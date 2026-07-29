"""Configuration for MCP transports and protocol security."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ALLOWED_HOSTS = (
    "mcp.m5stack.com",
    "mcp.m5stack.com:*",
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
)
DEFAULT_ALLOWED_ORIGINS = (
    "https://mcp.m5stack.com",
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
)


def _load_config_section() -> dict[str, Any]:
    path = Path(__file__).parent / "config.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            root = json.load(file)
    except (OSError, ValueError):
        return {}
    section = root.get("mcp_server", {}) if isinstance(root, dict) else {}
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


def _string_list(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return default


@dataclass(frozen=True)
class MCPServerConfig:
    json_response: bool
    stateless_http: bool
    max_request_body_size: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]


def load_mcp_server_config() -> MCPServerConfig:
    section = _load_config_section()
    return MCPServerConfig(
        json_response=_env_bool(
            "M5DOC_MCP_JSON_RESPONSE",
            bool(section.get("json_response", True)),
        ),
        stateless_http=_env_bool(
            "M5DOC_MCP_STATELESS_HTTP",
            bool(section.get("stateless_http", True)),
        ),
        max_request_body_size=_positive_int(
            os.getenv(
                "M5DOC_MCP_MAX_REQUEST_BODY_SIZE",
                section.get("max_request_body_size", 4 * 1024 * 1024),
            ),
            4 * 1024 * 1024,
        ),
        allowed_hosts=_string_list(
            os.getenv("M5DOC_MCP_ALLOWED_HOSTS", section.get("allowed_hosts")),
            DEFAULT_ALLOWED_HOSTS,
        ),
        allowed_origins=_string_list(
            os.getenv("M5DOC_MCP_ALLOWED_ORIGINS", section.get("allowed_origins")),
            DEFAULT_ALLOWED_ORIGINS,
        ),
    )
