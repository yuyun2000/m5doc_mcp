import asyncio
import logging
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from cloud_logging import (
    AsyncCloudLogger,
    CloudLogConfig,
    CloudLogHandler,
    RequestTelemetryMiddleware,
    _credential_setting,
    redact_log_message,
    request_metadata_from_scope,
    resolve_client_ip_from_scope,
)
from server import run_blocking_tool


def make_config(**overrides):
    values = {
        "enabled": True,
        "endpoint": "tls.example.test",
        "region": "cn-beijing",
        "access_key": "test-access-key",
        "secret_key": "test-secret-key",
        "topic_id": "test-topic-id",
        "source": "m5doc-mcp-test",
        "batch_size": 10,
        "flush_interval": 0.01,
        "queue_capacity": 100,
        "request_timeout": 1,
        "shutdown_timeout": 1,
        "max_backoff": 0.1,
        "trusted_proxies": ("127.0.0.1/32", "::1/128"),
        "fingerprint_salt": "unit-test-salt",
        "collect_client_ip": True,
    }
    values.update(overrides)
    return CloudLogConfig(**values)


class FakeTLSService:
    def __init__(self):
        self.requests = []

    def put_logs_v2(self, request):
        self.requests.append(request)


class LegacyPutLogsV2Logs:
    """Matches the volcengine 1.0.123 constructor used in production."""

    def __init__(self, source=None, filename=None):
        self.source = source
        self.filename = filename
        self.log_tags = {}
        self.logs = []

    def add_log(self, contents, log_time=0, time_ns=None):
        self.logs.append(
            type(
                "LegacyLog",
                (),
                {"log_dict": contents, "time": log_time, "time_ns": time_ns},
            )()
        )


class CloudLoggingTests(unittest.TestCase):
    def test_placeholder_tls_credential_falls_back_to_existing_volcengine_key(self):
        value = _credential_setting(
            {"access_key": "your_placeholder"},
            "access_key",
            "M5DOC_TEST_UNUSED_ACCESS_KEY",
            "existing-volcengine-key",
        )

        self.assertEqual(value, "existing-volcengine-key")

    def test_queue_is_non_blocking_and_bounded(self):
        logger = AsyncCloudLogger(make_config(queue_capacity=1))

        self.assertTrue(logger.emit("first"))
        self.assertFalse(logger.emit("second"))
        self.assertEqual(logger.status()["dropped"], 1)

    def test_background_worker_batches_and_uploads(self):
        service = FakeTLSService()
        logger = AsyncCloudLogger(
            make_config(), service_factory=lambda _config: service
        )

        with patch("cloud_logging.PutLogsV2Logs", LegacyPutLogsV2Logs):
            self.assertTrue(logger.start())
            logger.emit("mcp_tool_call", outcome="success", query_len=12)
            logger.stop()

        uploaded_events = []
        for request in service.requests:
            self.assertEqual(request.topic_id, "test-topic-id")
            for log in request.logs.logs:
                self.assertIsNone(log.time_ns)
                uploaded_events.append(log.log_dict["event"])
        self.assertIn("mcp_tool_call", uploaded_events)
        self.assertGreaterEqual(logger.status()["uploaded"], 1)

    def test_forwarded_ip_is_only_trusted_from_configured_proxy(self):
        config = make_config()
        headers = [
            (b"x-forwarded-for", b"192.0.2.99, 203.0.113.8, 127.0.0.1"),
            (b"user-agent", b"codex/1.0"),
            (b"accept-language", b"zh-CN"),
        ]
        trusted_scope = {
            "type": "http",
            "client": ("127.0.0.1", 50123),
            "headers": headers,
            "method": "POST",
            "path": "/mcp",
            "scheme": "https",
            "http_version": "1.1",
        }
        direct_scope = {**trusted_scope, "client": ("198.51.100.7", 50123)}

        trusted = request_metadata_from_scope(trusted_scope, config)
        direct = request_metadata_from_scope(direct_scope, config)

        self.assertEqual(trusted["client_ip"], "203.0.113.8")
        self.assertEqual(direct["client_ip"], "198.51.100.7")
        self.assertEqual(len(trusted["client_fingerprint"]), 32)
        self.assertEqual(
            trusted["client_fingerprint"],
            request_metadata_from_scope(trusted_scope, config)["client_fingerprint"],
        )

        trusted_scope["headers"].append((b"x-real-ip", b"198.51.100.23"))
        with_real_ip = request_metadata_from_scope(trusted_scope, config)
        self.assertEqual(with_real_ip["client_ip"], "198.51.100.23")

    def test_internal_ip_resolution_does_not_depend_on_log_collection(self):
        config = make_config(collect_client_ip=False)
        scope = {
            "type": "http",
            "client": ("127.0.0.1", 50123),
            "headers": [(b"x-real-ip", b"198.51.100.44")],
            "method": "POST",
            "path": "/mcp",
        }

        metadata = request_metadata_from_scope(scope, config)
        resolved_ip, peer_ip = resolve_client_ip_from_scope(scope, config)

        self.assertEqual(metadata["client_ip"], "")
        self.assertEqual(metadata["peer_ip"], "")
        self.assertEqual(resolved_ip, "198.51.100.44")
        self.assertEqual(peer_ip, "127.0.0.1")

    def test_log_redaction_masks_credentials(self):
        message = "Authorization: Bearer token-123 api_key=secret-456"
        redacted = redact_log_message(message)

        self.assertNotIn("token-123", redacted)
        self.assertNotIn("secret-456", redacted)
        self.assertIn("<redacted>", redacted)

    def test_cloud_handler_enqueues_without_exception_text(self):
        logger = AsyncCloudLogger(make_config(queue_capacity=2))
        handler = CloudLogHandler(logger)
        record = logging.LogRecord(
            "m5doc_test",
            logging.ERROR,
            __file__,
            1,
            "failed with api_key=secret-value",
            (),
            None,
        )

        handler.emit(record)

        payload = logger._queue.get_nowait()
        self.assertEqual(payload["event"], "application_log")
        self.assertNotIn("secret-value", payload["message"])


class RequestTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expected_oauth_discovery_404_is_not_uploaded(self):
        logger = AsyncCloudLogger(make_config(enabled=False))
        emitted = []
        logger.emit = lambda event, **fields: emitted.append((event, fields)) or True

        async def downstream(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"Not Found"})

        middleware = RequestTelemetryMiddleware(downstream, logger)
        scope = {
            "type": "http",
            "client": ("198.51.100.10", 50000),
            "headers": [(b"host", b"mcp.m5stack.com")],
            "method": "GET",
            "path": "/.well-known/oauth-protected-resource/sse",
            "scheme": "https",
            "http_version": "1.1",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        await middleware(scope, receive, send)

        self.assertEqual(emitted, [])


class ToolConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_worker_keeps_admission_slot_until_it_exits(self):
        release_worker = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        semaphore = asyncio.Semaphore(1)

        try:
            with self.assertRaises(asyncio.TimeoutError):
                await run_blocking_tool(
                    semaphore,
                    executor,
                    lambda: release_worker.wait(1),
                    timeout=0.01,
                    queue_timeout=0.01,
                )
            self.assertTrue(semaphore.locked())

            release_worker.set()
            deadline = time.monotonic() + 1
            while semaphore.locked() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertFalse(semaphore.locked())
        finally:
            release_worker.set()
            executor.shutdown(wait=True)

    async def test_cancelled_worker_keeps_admission_slot_until_it_exits(self):
        release_worker = threading.Event()
        worker_started = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        semaphore = asyncio.Semaphore(1)

        def blocking_call():
            worker_started.set()
            release_worker.wait(1)

        task = asyncio.create_task(
            run_blocking_tool(
                semaphore,
                executor,
                blocking_call,
                timeout=1,
                queue_timeout=0.1,
            )
        )
        try:
            while not worker_started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(semaphore.locked())

            release_worker.set()
            deadline = time.monotonic() + 1
            while semaphore.locked() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertFalse(semaphore.locked())
        finally:
            release_worker.set()
            executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
