import unittest

from rate_limit import (
    PerIpRateLimitMiddleware,
    PerIpRateLimiter,
    RateLimitConfig,
)


def make_config(**overrides):
    values = {
        "enabled": True,
        "requests_per_minute": 60,
        "burst": 2,
        "whitelist_ips": ("47.113.125.164",),
        "max_clients": 100,
        "client_ttl_seconds": 3600,
    }
    values.update(overrides)
    return RateLimitConfig(**values)


class PerIpRateLimiterTests(unittest.TestCase):
    def test_each_source_ip_has_an_independent_bucket(self):
        now = [100.0]
        limiter = PerIpRateLimiter(make_config(), clock=lambda: now[0])

        self.assertTrue(limiter.check("198.51.100.10").allowed)
        self.assertTrue(limiter.check("198.51.100.10").allowed)
        self.assertFalse(limiter.check("198.51.100.10").allowed)
        self.assertTrue(limiter.check("198.51.100.11").allowed)

        now[0] += 1.0
        self.assertTrue(limiter.check("198.51.100.10").allowed)

    def test_server_ip_is_always_whitelisted(self):
        limiter = PerIpRateLimiter(make_config())

        for _ in range(100):
            decision = limiter.check("47.113.125.164")
            self.assertTrue(decision.allowed)
            self.assertTrue(decision.bypassed)

        self.assertEqual(limiter.status()["blocked"], 0)
        self.assertEqual(limiter.status()["bypassed"], 100)


class RateLimitMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_request_returns_429_after_ip_exhausts_burst(self):
        limiter = PerIpRateLimiter(make_config(burst=1))
        blocked_events = []

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = PerIpRateLimitMiddleware(
            downstream,
            limiter,
            client_ip_getter=lambda _scope: "198.51.100.20",
            on_blocked=lambda event, **fields: blocked_events.append((event, fields)),
        )
        scope = {"type": "http", "path": "/mcp", "method": "POST"}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def invoke():
            messages = []

            async def send(message):
                messages.append(message)

            await middleware(scope, receive, send)
            return messages

        first = await invoke()
        second = await invoke()

        self.assertEqual(first[0]["status"], 204)
        self.assertEqual(second[0]["status"], 429)
        self.assertEqual(blocked_events[0][0], "rate_limit_blocked")
        self.assertEqual(blocked_events[0][1]["client_ip"], "198.51.100.20")

    async def test_health_endpoint_is_not_rate_limited(self):
        limiter = PerIpRateLimiter(make_config(burst=1))

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = PerIpRateLimitMiddleware(
            downstream,
            limiter,
            client_ip_getter=lambda _scope: "198.51.100.30",
        )
        scope = {"type": "http", "path": "/health", "method": "GET"}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        for _ in range(5):
            messages = []

            async def send(message):
                messages.append(message)

            await middleware(scope, receive, send)
            self.assertEqual(messages[0]["status"], 200)


if __name__ == "__main__":
    unittest.main()
