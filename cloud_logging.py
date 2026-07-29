"""Non-blocking Volcengine TLS logging and request telemetry."""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from volcengine.tls.TLSService import TLSService
from volcengine.tls.tls_requests import PutLogsV2Logs, PutLogsV2Request


DEFAULT_TLS_ENDPOINT = "tls-cn-beijing.volces.com"
DEFAULT_TLS_REGION = "cn-beijing"
DEFAULT_TLS_TOPIC_ID = ""
DEFAULT_TRUSTED_PROXIES = ("127.0.0.1/32", "::1/128")
DEFAULT_MESSAGE_LOG_INTERVAL_SECONDS = 60.0
DEFAULT_MESSAGE_LOG_MAX_KEYS = 20000
MAX_JSONRPC_METHOD_BODY_BYTES = 64 * 1024
PLACEHOLDER_PREFIXES = ("your_", "change_")
JSONRPC_METHOD_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")

REQUEST_METADATA: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "m5doc_request_metadata",
    default={},
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return max(minimum, int(value))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return max(minimum, float(value))
    except ValueError:
        return default


def _load_config_root() -> dict[str, Any]:
    path = Path(__file__).parent / "config.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            root = json.load(file)
    except (OSError, ValueError):
        return {}
    return root if isinstance(root, dict) else {}


def _setting(section: dict[str, Any], key: str, env_name: str, default: Any) -> Any:
    value = os.getenv(env_name)
    if value not in (None, ""):
        return value
    return section.get(key, default)


def _credential_setting(
    section: dict[str, Any],
    key: str,
    env_name: str,
    fallback: str,
) -> str:
    env_value = os.getenv(env_name)
    if env_value not in (None, ""):
        return env_value
    configured_value = str(section.get(key, ""))
    if configured_value and not configured_value.lower().startswith(PLACEHOLDER_PREFIXES):
        return configured_value
    return str(fallback or "")


@dataclass(frozen=True)
class CloudLogConfig:
    enabled: bool
    endpoint: str
    region: str
    access_key: str
    secret_key: str
    topic_id: str
    source: str
    batch_size: int
    flush_interval: float
    queue_capacity: int
    request_timeout: float
    shutdown_timeout: float
    max_backoff: float
    trusted_proxies: tuple[str, ...]
    fingerprint_salt: str
    collect_client_ip: bool
    message_log_interval_seconds: float = DEFAULT_MESSAGE_LOG_INTERVAL_SECONDS
    message_log_max_keys: int = DEFAULT_MESSAGE_LOG_MAX_KEYS

    @property
    def configured(self) -> bool:
        required = (self.access_key, self.secret_key, self.topic_id)
        return self.enabled and all(
            value and not value.lower().startswith(PLACEHOLDER_PREFIXES)
            for value in required
        )


def load_cloud_log_config() -> CloudLogConfig:
    root = _load_config_root()
    section = root.get("cloud_logging", {})
    if not isinstance(section, dict):
        section = {}
    volcengine_section = root.get("volcengine", {})
    if not isinstance(volcengine_section, dict):
        volcengine_section = {}
    trusted = _setting(
        section,
        "trusted_proxies",
        "M5DOC_TLS_TRUSTED_PROXIES",
        DEFAULT_TRUSTED_PROXIES,
    )
    if isinstance(trusted, str):
        trusted_proxies = tuple(item.strip() for item in trusted.split(",") if item.strip())
    else:
        trusted_proxies = tuple(str(item) for item in trusted or DEFAULT_TRUSTED_PROXIES)

    configured_enabled = section.get("enabled", True)
    enabled = _env_bool("M5DOC_TLS_ENABLED", bool(configured_enabled))
    return CloudLogConfig(
        enabled=enabled,
        endpoint=str(_setting(section, "endpoint", "M5DOC_TLS_ENDPOINT", DEFAULT_TLS_ENDPOINT)),
        region=str(_setting(section, "region", "M5DOC_TLS_REGION", DEFAULT_TLS_REGION)),
        access_key=_credential_setting(
            section,
            "access_key",
            "M5DOC_TLS_ACCESS_KEY",
            str(volcengine_section.get("ak", "")),
        ),
        secret_key=_credential_setting(
            section,
            "secret_key",
            "M5DOC_TLS_SECRET_KEY",
            str(volcengine_section.get("sk", "")),
        ),
        topic_id=str(_setting(section, "topic_id", "M5DOC_TLS_TOPIC_ID", DEFAULT_TLS_TOPIC_ID)),
        source=str(_setting(section, "source", "M5DOC_TLS_SOURCE", "m5doc-mcp")),
        batch_size=_env_int(
            "M5DOC_TLS_BATCH_SIZE",
            int(section.get("batch_size", 50)),
        ),
        flush_interval=_env_float(
            "M5DOC_TLS_FLUSH_INTERVAL",
            float(section.get("flush_interval", 1.0)),
        ),
        queue_capacity=_env_int(
            "M5DOC_TLS_QUEUE_CAPACITY",
            int(section.get("queue_capacity", 10000)),
        ),
        request_timeout=_env_float(
            "M5DOC_TLS_REQUEST_TIMEOUT",
            float(section.get("request_timeout", 5.0)),
        ),
        shutdown_timeout=_env_float(
            "M5DOC_TLS_SHUTDOWN_TIMEOUT",
            float(section.get("shutdown_timeout", 5.0)),
        ),
        max_backoff=_env_float(
            "M5DOC_TLS_MAX_BACKOFF",
            float(section.get("max_backoff", 30.0)),
        ),
        trusted_proxies=trusted_proxies,
        fingerprint_salt=str(
            _setting(section, "fingerprint_salt", "M5DOC_TLS_FINGERPRINT_SALT", "")
        ),
        collect_client_ip=_env_bool(
            "M5DOC_TLS_COLLECT_CLIENT_IP",
            bool(section.get("collect_client_ip", True)),
        ),
        message_log_interval_seconds=_env_float(
            "M5DOC_TLS_MESSAGE_LOG_INTERVAL_SECONDS",
            float(
                section.get(
                    "message_log_interval_seconds",
                    DEFAULT_MESSAGE_LOG_INTERVAL_SECONDS,
                )
            ),
            minimum=0.0,
        ),
        message_log_max_keys=_env_int(
            "M5DOC_TLS_MESSAGE_LOG_MAX_KEYS",
            int(section.get("message_log_max_keys", DEFAULT_MESSAGE_LOG_MAX_KEYS)),
        ),
    )


def _safe_stderr(message: str) -> None:
    try:
        sys.stderr.write(f"{message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return str(value)


class AsyncCloudLogger:
    """Bounded, best-effort log uploader that never blocks request threads."""

    def __init__(
        self,
        config: CloudLogConfig | None = None,
        service_factory: Callable[[CloudLogConfig], Any] | None = None,
    ) -> None:
        self.config = config or load_cloud_log_config()
        self._service_factory = service_factory or self._default_service_factory
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(self.config.queue_capacity)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._service: Any = None
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._instance_id = uuid.uuid4().hex
        self._host = socket.gethostname()
        self._dropped = 0
        self._uploaded = 0
        self._upload_failures = 0
        self._batches_uploaded = 0
        self._suppressed_transport_logs = 0
        self._last_success_at = 0.0
        self._last_error_type = ""
        self._active = {"http": 0, "tool": 0}
        self._peak = {"http": 0, "tool": 0}

    @staticmethod
    def _default_service_factory(config: CloudLogConfig) -> TLSService:
        return TLSService(
            config.endpoint,
            config.access_key,
            config.secret_key,
            config.region,
            timeout=max(1, int(round(config.request_timeout))),
        )

    def start(self) -> bool:
        if not self.config.configured:
            if self.config.enabled:
                _safe_stderr("[WARN] Cloud logging disabled: TLS credentials are not configured")
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            try:
                self._service = self._service_factory(self.config)
            except Exception as exc:
                self._last_error_type = type(exc).__name__
                _safe_stderr(f"[WARN] Cloud logging initialization failed: {type(exc).__name__}")
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="m5doc-cloud-log",
                daemon=True,
            )
            self._thread.start()
        self.emit("service_lifecycle", state="started")
        return True

    def stop(self) -> None:
        thread = self._thread
        if not thread:
            return
        self.emit("service_lifecycle", state="stopping")
        self._stop_event.set()
        thread.join(timeout=self.config.shutdown_timeout)
        if thread.is_alive():
            _safe_stderr("[WARN] Cloud logging shutdown timed out; remaining logs were abandoned")
        self._thread = None

    def emit(self, event: str, **fields: Any) -> bool:
        if not self.config.configured:
            return False
        now = time.time()
        payload = {
            "event": event,
            "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "epoch_ms": int(now * 1000),
            "service": "m5doc-mcp",
            "instance_id": self._instance_id,
            "host": self._host,
            "pid": os.getpid(),
            **fields,
        }
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False

    def activity_enter(self, kind: str) -> tuple[int, int]:
        with self._lock:
            self._active[kind] = self._active.get(kind, 0) + 1
            self._peak[kind] = max(self._peak.get(kind, 0), self._active[kind])
            return self._active[kind], self._peak[kind]

    def activity_exit(self, kind: str) -> int:
        with self._lock:
            self._active[kind] = max(0, self._active.get(kind, 0) - 1)
            return self._active[kind]

    def record_suppressed_transport_log(self) -> None:
        with self._lock:
            self._suppressed_transport_logs += 1

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "configured": self.config.configured,
                "worker_alive": bool(self._thread and self._thread.is_alive()),
                "queue_size": self._queue.qsize(),
                "queue_capacity": self.config.queue_capacity,
                "dropped": self._dropped,
                "uploaded": self._uploaded,
                "upload_failures": self._upload_failures,
                "batches_uploaded": self._batches_uploaded,
                "suppressed_transport_logs": self._suppressed_transport_logs,
                "last_success_at": int(self._last_success_at) if self._last_success_at else None,
                "last_error_type": self._last_error_type or None,
                "active_http": self._active["http"],
                "peak_http": self._peak["http"],
                "active_tools": self._active["tool"],
                "peak_tools": self._peak["tool"],
                "uptime_seconds": int(time.time() - self._started_at),
            }

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop_event.is_set() or not self._queue.empty():
            batch = self._take_batch()
            if not batch:
                continue
            try:
                self._upload(batch)
            except Exception as exc:
                consecutive_failures += 1
                with self._lock:
                    self._upload_failures += len(batch)
                    self._last_error_type = type(exc).__name__
                _safe_stderr(f"[WARN] Cloud log upload failed: {type(exc).__name__}")
                backoff = min(self.config.max_backoff, float(2 ** min(consecutive_failures - 1, 8)))
                self._stop_event.wait(backoff)
            else:
                consecutive_failures = 0
                with self._lock:
                    self._uploaded += len(batch)
                    self._batches_uploaded += 1
                    self._last_success_at = time.time()
                    self._last_error_type = ""

    def _take_batch(self) -> list[dict[str, Any]]:
        try:
            first = self._queue.get(timeout=self.config.flush_interval)
        except queue.Empty:
            return []
        batch = [first]
        deadline = time.monotonic() + self.config.flush_interval
        while len(batch) < self.config.batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(self._queue.get(timeout=remaining))
            except queue.Empty:
                break
        return batch

    def _upload(self, batch: list[dict[str, Any]]) -> None:
        logs = PutLogsV2Logs(
            source=self.config.source,
            filename="mcp",
        )
        for event in batch:
            epoch_ms = int(event.get("epoch_ms", int(time.time() * 1000)))
            logs.add_log(
                contents={key: _stringify(value) for key, value in event.items()},
                log_time=epoch_ms // 1000,
            )
        request = PutLogsV2Request(self.config.topic_id, logs, compression="zlib")
        self._service.put_logs_v2(request)


_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+")
_SECRET_RE = re.compile(r"(?i)((?:api[_-]?key|access[_-]?key|secret[_-]?key|\bak|\bsk)\s*[:=]\s*)[^\s,;]+")


def redact_sensitive_text(message: str) -> str:
    redacted = _AUTH_RE.sub(r"\1<redacted>", str(message))
    return _SECRET_RE.sub(r"\1<redacted>", redacted)


def redact_log_message(message: str, limit: int = 2000) -> str:
    return redact_sensitive_text(message)[:limit]


class CloudLogHandler(logging.Handler):
    def __init__(self, uploader: AsyncCloudLogger) -> None:
        super().__init__()
        self.uploader = uploader

    def emit(self, record: logging.LogRecord) -> None:
        if record.threadName == "m5doc-cloud-log" or record.name.startswith("volcengine"):
            return
        try:
            self.uploader.emit(
                "application_log",
                level=record.levelname,
                logger=record.name,
                message=redact_log_message(record.getMessage()),
            )
        except Exception:
            self.handleError(record)


def configure_cloud_logging(uploader: AsyncCloudLogger, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(CloudLogHandler(uploader))


def _headers_from_scope(scope: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        headers[name] = f"{headers[name]},{value}" if name in headers else value
    return headers


def _valid_ip(value: str) -> str:
    candidate = value.strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def _is_trusted_proxy(peer_ip: str, networks: tuple[str, ...]) -> bool:
    try:
        address = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    for network in networks:
        try:
            if address in ipaddress.ip_network(network, strict=False):
                return True
        except ValueError:
            continue
    return False


def resolve_client_ip_from_scope(
    scope: dict[str, Any],
    config: CloudLogConfig,
) -> tuple[str, str]:
    headers = _headers_from_scope(scope)
    client = scope.get("client") or ("", 0)
    peer_ip = _valid_ip(str(client[0])) if client else ""
    client_ip = peer_ip
    if peer_ip and _is_trusted_proxy(peer_ip, config.trusted_proxies):
        real_ip = _valid_ip(headers.get("x-real-ip", ""))
        forwarded_ips = [
            valid
            for item in headers.get("x-forwarded-for", "").split(",")
            if (valid := _valid_ip(item))
        ]
        forwarded_ip = next(
            (
                address
                for address in reversed(forwarded_ips)
                if not _is_trusted_proxy(address, config.trusted_proxies)
            ),
            forwarded_ips[-1] if forwarded_ips else "",
        )
        client_ip = real_ip or forwarded_ip or peer_ip
    return client_ip, peer_ip


def request_metadata_from_scope(scope: dict[str, Any], config: CloudLogConfig) -> dict[str, str]:
    headers = _headers_from_scope(scope)
    client = scope.get("client") or ("", 0)
    client_ip, peer_ip = resolve_client_ip_from_scope(scope, config)

    user_agent = headers.get("user-agent", "")[:1000]
    accept_language = headers.get("accept-language", "")[:250]
    fingerprint_key = (config.fingerprint_salt or config.secret_key).encode("utf-8")
    fingerprint_input = "\n".join((client_ip, user_agent, accept_language)).encode("utf-8")
    fingerprint = hmac.new(fingerprint_key, fingerprint_input, hashlib.sha256).hexdigest()[:32]

    metadata = {
        "request_id": headers.get("x-request-id", "")[:128] or uuid.uuid4().hex,
        "client_ip": client_ip if config.collect_client_ip else "",
        "peer_ip": peer_ip if config.collect_client_ip else "",
        "client_port": str(client[1]) if client and len(client) > 1 else "",
        "client_fingerprint": fingerprint,
        "fingerprint_version": "v1",
        "user_agent": user_agent,
        "accept_language": accept_language,
        "referer": headers.get("referer", "")[:1000],
        "origin": headers.get("origin", "")[:500],
        "host_header": headers.get("host", "")[:500],
        "mcp_protocol_version": headers.get("mcp-protocol-version", "")[:100],
        "mcp_session_id": headers.get("mcp-session-id", "")[:250],
        "http_method": str(scope.get("method", "")),
        "http_path": str(scope.get("path", "")),
        "http_scheme": str(scope.get("scheme", "")),
        "http_version": str(scope.get("http_version", "")),
    }
    return metadata


def _jsonrpc_method_from_body(body: bytes, complete: bool, truncated: bool) -> str:
    """Return only the top-level JSON-RPC method, never params or body text."""
    if not complete or truncated or not body:
        return ""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return "batch" if isinstance(payload, list) else ""
    method = payload.get("method")
    if not isinstance(method, str):
        return ""
    return method if JSONRPC_METHOD_RE.fullmatch(method) else "invalid"


@dataclass
class _MessageLogSample:
    window_started: float
    suppressed: int = 0


class _SuccessfulMessageLogSampler:
    """Bound successful legacy transport logs without affecting MCP traffic."""

    def __init__(self, interval_seconds: float, max_keys: int) -> None:
        self.interval_seconds = max(0.0, interval_seconds)
        self.max_keys = max(1, max_keys)
        self._samples: dict[tuple[str, str], _MessageLogSample] = {}
        self._lock = threading.Lock()

    def select(self, fingerprint: str, method: str) -> tuple[bool, int]:
        if self.interval_seconds <= 0:
            return True, 0

        now = time.monotonic()
        key = (fingerprint, method)
        with self._lock:
            sample = self._samples.get(key)
            if sample and now - sample.window_started < self.interval_seconds:
                sample.suppressed += 1
                return False, 0

            previously_suppressed = sample.suppressed if sample else 0
            if sample:
                self._samples.pop(key, None)
            elif len(self._samples) >= self.max_keys:
                self._samples.pop(next(iter(self._samples)))
            self._samples[key] = _MessageLogSample(window_started=now)
            return True, previously_suppressed


class RequestTelemetryMiddleware:
    def __init__(self, app: Any, uploader: AsyncCloudLogger) -> None:
        self.app = app
        self.uploader = uploader
        self._message_sampler = _SuccessfulMessageLogSampler(
            uploader.config.message_log_interval_seconds,
            uploader.config.message_log_max_keys,
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        metadata = request_metadata_from_scope(scope, self.uploader.config)
        token = REQUEST_METADATA.set(metadata)
        started = time.monotonic()
        active, peak = self.uploader.activity_enter("http")
        status_code = 500
        response_bytes = 0
        path = str(scope.get("path", ""))
        observe_jsonrpc_method = (
            str(scope.get("method", "")).upper() == "POST"
            and path in {"/messages", "/messages/"}
        )
        request_body = bytearray()
        request_body_complete = False
        request_body_truncated = False

        async def receive_wrapper() -> dict[str, Any]:
            nonlocal request_body_complete, request_body_truncated
            message = await receive()
            if observe_jsonrpc_method and message.get("type") == "http.request":
                body = message.get("body", b"")
                remaining = MAX_JSONRPC_METHOD_BODY_BYTES - len(request_body)
                if remaining > 0:
                    request_body.extend(body[:remaining])
                if len(body) > remaining:
                    request_body_truncated = True
                if not message.get("more_body", False):
                    request_body_complete = True
                    method = _jsonrpc_method_from_body(
                        bytes(request_body),
                        request_body_complete,
                        request_body_truncated,
                    )
                    if method:
                        metadata["mcp_jsonrpc_method"] = method
            return message

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code, response_bytes
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            elif message.get("type") == "http.response.body":
                response_bytes += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(
                scope,
                receive_wrapper if observe_jsonrpc_method else receive,
                send_wrapper,
            )
        except Exception:
            self.uploader.emit(
                "http_request_error",
                **metadata,
                error_type="unhandled_exception",
            )
            raise
        finally:
            remaining = self.uploader.activity_exit("http")
            # Generic MCP/Node clients commonly probe OAuth discovery variants.
            # Without OAuth configured these 404s are expected and have no usage value.
            ignore_oauth_probe = (
                status_code == 404
                and path.startswith("/.well-known/oauth-protected-resource")
            )
            should_emit = not ignore_oauth_probe
            sampling_fields: dict[str, Any] = {}
            successful_message_post = (
                observe_jsonrpc_method and 200 <= status_code < 300
            )
            if should_emit and successful_message_post:
                jsonrpc_method = metadata.get("mcp_jsonrpc_method", "unknown")
                should_emit, previously_suppressed = self._message_sampler.select(
                    metadata.get("client_fingerprint", ""),
                    jsonrpc_method,
                )
                if should_emit:
                    sampling_fields = {
                        "transport_log_sampled": True,
                        "sample_interval_seconds": self._message_sampler.interval_seconds,
                        "suppressed_since_previous_sample": previously_suppressed,
                    }
                else:
                    self.uploader.record_suppressed_transport_log()
            if should_emit:
                self.uploader.emit(
                    "http_request",
                    **metadata,
                    status_code=status_code,
                    response_bytes=response_bytes,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                    active_http_at_start=active,
                    peak_http=peak,
                    active_http_after=remaining,
                    **sampling_fields,
                )
            REQUEST_METADATA.reset(token)


cloud_logger = AsyncCloudLogger()
