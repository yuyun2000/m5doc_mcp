"""Per-source-IP token-bucket rate limiting for MCP HTTP transports."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from starlette.responses import JSONResponse


DEFAULT_WHITELIST_IPS = ("47.113.125.164",)
MCP_PATHS = frozenset({"/sse", "/messages", "/mcp"})


def _load_config_section() -> dict[str, Any]:
    path = Path(__file__).parent / "config.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            root = json.load(file)
    except (OSError, ValueError):
        return {}
    section = root.get("rate_limit", {}) if isinstance(root, dict) else {}
    return section if isinstance(section, dict) else {}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool
    requests_per_minute: int
    burst: int
    whitelist_ips: tuple[str, ...]
    max_clients: int
    client_ttl_seconds: int


def load_rate_limit_config() -> RateLimitConfig:
    section = _load_config_section()
    whitelist_value = os.getenv("M5DOC_RATE_LIMIT_WHITELIST_IPS")
    if whitelist_value is not None:
        whitelist = tuple(item.strip() for item in whitelist_value.split(",") if item.strip())
    else:
        configured = section.get("whitelist_ips", DEFAULT_WHITELIST_IPS)
        if isinstance(configured, str):
            whitelist = tuple(item.strip() for item in configured.split(",") if item.strip())
        else:
            whitelist = tuple(str(item) for item in configured or DEFAULT_WHITELIST_IPS)

    return RateLimitConfig(
        enabled=_env_bool("M5DOC_RATE_LIMIT_ENABLED", bool(section.get("enabled", True))),
        requests_per_minute=_positive_int(
            os.getenv(
                "M5DOC_RATE_LIMIT_REQUESTS_PER_MINUTE",
                section.get("requests_per_minute", 120),
            ),
            120,
        ),
        burst=_positive_int(
            os.getenv("M5DOC_RATE_LIMIT_BURST", section.get("burst", 30)),
            30,
        ),
        whitelist_ips=whitelist,
        max_clients=_positive_int(
            os.getenv("M5DOC_RATE_LIMIT_MAX_CLIENTS", section.get("max_clients", 10000)),
            10000,
        ),
        client_ttl_seconds=_positive_int(
            os.getenv(
                "M5DOC_RATE_LIMIT_CLIENT_TTL_SECONDS",
                section.get("client_ttl_seconds", 3600),
            ),
            3600,
        ),
    )


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    last_seen_at: float


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    bypassed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class PerIpRateLimiter:
    def __init__(
        self,
        config: RateLimitConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or load_rate_limit_config()
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._checks = 0
        self._allowed = 0
        self._blocked = 0
        self._bypassed = 0
        self._whitelist = self._parse_whitelist(self.config.whitelist_ips)

    @staticmethod
    def _parse_whitelist(values: tuple[str, ...]) -> tuple[Any, ...]:
        networks = []
        for value in values:
            try:
                networks.append(ipaddress.ip_network(value, strict=False))
            except ValueError:
                continue
        return tuple(networks)

    def is_whitelisted(self, client_ip: str) -> bool:
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return any(address in network for network in self._whitelist)

    def check(self, client_ip: str) -> RateLimitDecision:
        if not self.config.enabled:
            return RateLimitDecision(True, True, self.config.requests_per_minute, self.config.burst, 0)
        if self.is_whitelisted(client_ip):
            with self._lock:
                self._bypassed += 1
            return RateLimitDecision(True, True, self.config.requests_per_minute, self.config.burst, 0)

        key = client_ip or "unknown"
        now = self._clock()
        refill_rate = self.config.requests_per_minute / 60.0
        with self._lock:
            self._checks += 1
            if self._checks % 256 == 0:
                self._cleanup_locked(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                self._ensure_capacity_locked(now)
                bucket = _Bucket(float(self.config.burst), now, now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(
                    float(self.config.burst),
                    bucket.tokens + elapsed * refill_rate,
                )
                bucket.updated_at = now
                bucket.last_seen_at = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                self._allowed += 1
                return RateLimitDecision(
                    True,
                    False,
                    self.config.requests_per_minute,
                    max(0, int(bucket.tokens)),
                    0,
                )

            self._blocked += 1
            retry_after = max(1, math.ceil((1.0 - bucket.tokens) / refill_rate))
            return RateLimitDecision(
                False,
                False,
                self.config.requests_per_minute,
                0,
                retry_after,
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "requests_per_minute": self.config.requests_per_minute,
                "burst": self.config.burst,
                "whitelist_ips": list(self.config.whitelist_ips),
                "tracked_ips": len(self._buckets),
                "allowed": self._allowed,
                "blocked": self._blocked,
                "bypassed": self._bypassed,
            }

    def _cleanup_locked(self, now: float) -> None:
        stale_before = now - self.config.client_ttl_seconds
        stale_keys = [
            key for key, bucket in self._buckets.items() if bucket.last_seen_at < stale_before
        ]
        for key in stale_keys:
            self._buckets.pop(key, None)

    def _ensure_capacity_locked(self, now: float) -> None:
        if len(self._buckets) < self.config.max_clients:
            return
        self._cleanup_locked(now)
        if len(self._buckets) < self.config.max_clients:
            return
        oldest_key = min(self._buckets, key=lambda key: self._buckets[key].last_seen_at)
        self._buckets.pop(oldest_key, None)


class PerIpRateLimitMiddleware:
    def __init__(
        self,
        app: Any,
        limiter: PerIpRateLimiter,
        client_ip_getter: Callable[[dict[str, Any]], str],
        on_blocked: Callable[..., Any] | None = None,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.client_ip_getter = client_ip_getter
        self.on_blocked = on_blocked

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") not in MCP_PATHS:
            await self.app(scope, receive, send)
            return

        client_ip = self.client_ip_getter(scope)
        decision = self.limiter.check(client_ip)
        if decision.allowed:
            await self.app(scope, receive, send)
            return

        if self.on_blocked:
            self.on_blocked(
                "rate_limit_blocked",
                client_ip=client_ip,
                http_method=str(scope.get("method", "")),
                http_path=str(scope.get("path", "")),
                limit=decision.limit,
                retry_after_seconds=decision.retry_after_seconds,
            )
        response = JSONResponse(
            {
                "error": "rate_limit_exceeded",
                "message": "Too many requests from this IP. Please retry later.",
                "retry_after_seconds": decision.retry_after_seconds,
            },
            status_code=429,
            headers={
                "Retry-After": str(decision.retry_after_seconds),
                "X-RateLimit-Limit": str(decision.limit),
                "X-RateLimit-Remaining": "0",
            },
        )
        await response(scope, receive, send)


rate_limiter = PerIpRateLimiter()
