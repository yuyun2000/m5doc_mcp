import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger("m5doc_ai_answer")

DEFAULT_BASE_URL = "https://chat.m5stack.com"
DEFAULT_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
DEFAULT_MODEL = "m5stack-fae"


class AIAnswerError(Exception):
    """Base error for the OpenAI-compatible answer service."""


class AIAnswerConfigError(AIAnswerError):
    """Raised when the answer service configuration is missing or invalid."""


class AIAnswerResponseError(AIAnswerError):
    """Raised when the answer service returns an unusable response."""


@dataclass(frozen=True)
class AIAnswerConfig:
    base_url: str
    api_key: str
    model: str
    chat_completions_path: str
    connect_timeout: float
    read_timeout: float
    max_retries: int
    pool_connections: int
    pool_maxsize: int


thread_local = threading.local()


def _load_config_root() -> dict:
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        raise AIAnswerConfigError(
            f"config file not found: {config_path}. Copy config.example.json to config.json first."
        )

    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_config_value(section: dict, key: str, env_name: str, default=None):
    value = os.getenv(env_name)
    if value is not None and value != "":
        return value
    return section.get(key, default)


def _as_float(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AIAnswerConfigError(f"invalid numeric config for {name}") from exc


def _as_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise AIAnswerConfigError(f"invalid integer config for {name}") from exc


def load_ai_answer_config() -> AIAnswerConfig:
    root = _load_config_root()
    section = root.get("openai_compatible", {})

    base_url = _get_config_value(section, "base_url", "M5DOC_AI_BASE_URL", DEFAULT_BASE_URL)
    api_key = _get_config_value(section, "api_key", "M5DOC_AI_API_KEY", "")
    model = _get_config_value(section, "model", "M5DOC_AI_MODEL", DEFAULT_MODEL)
    chat_completions_path = _get_config_value(
        section,
        "chat_completions_path",
        "M5DOC_AI_CHAT_COMPLETIONS_PATH",
        DEFAULT_CHAT_COMPLETIONS_PATH,
    )

    if not api_key or str(api_key).startswith("your_"):
        raise AIAnswerConfigError(
            "missing openai_compatible.api_key or M5DOC_AI_API_KEY for AI answer service"
        )

    return AIAnswerConfig(
        base_url=str(base_url).rstrip("/"),
        api_key=str(api_key),
        model=str(model),
        chat_completions_path=str(chat_completions_path),
        connect_timeout=_as_float(
            _get_config_value(section, "connect_timeout", "M5DOC_AI_CONNECT_TIMEOUT", 10),
            "openai_compatible.connect_timeout",
        ),
        read_timeout=_as_float(
            _get_config_value(section, "read_timeout", "M5DOC_AI_READ_TIMEOUT", 240),
            "openai_compatible.read_timeout",
        ),
        max_retries=_as_int(
            _get_config_value(section, "max_retries", "M5DOC_AI_MAX_RETRIES", 1),
            "openai_compatible.max_retries",
        ),
        pool_connections=_as_int(
            _get_config_value(section, "pool_connections", "M5DOC_AI_POOL_CONNECTIONS", 8),
            "openai_compatible.pool_connections",
        ),
        pool_maxsize=_as_int(
            _get_config_value(section, "pool_maxsize", "M5DOC_AI_POOL_MAXSIZE", 16),
            "openai_compatible.pool_maxsize",
        ),
    )


def _create_http_session(config: AIAnswerConfig) -> requests.Session:
    retry = Retry(
        total=config.max_retries,
        connect=config.max_retries,
        read=0,
        status=config.max_retries,
        backoff_factor=0.5,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(
        pool_connections=config.pool_connections,
        pool_maxsize=config.pool_maxsize,
        max_retries=retry,
        pool_block=True,
    )
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _get_http_session(config: AIAnswerConfig) -> requests.Session:
    session_key = (
        config.max_retries,
        config.pool_connections,
        config.pool_maxsize,
    )
    session = getattr(thread_local, "http_session", None)
    existing_key = getattr(thread_local, "http_session_key", None)
    if session is None or existing_key != session_key:
        session = _create_http_session(config)
        thread_local.http_session = session
        thread_local.http_session_key = session_key
    return session


def _chat_completions_url(config: AIAnswerConfig) -> str:
    base_url = config.base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if (
        config.chat_completions_path == DEFAULT_CHAT_COMPLETIONS_PATH
        and base_url.endswith("/v1")
    ):
        return f"{base_url}/chat/completions"
    return f"{base_url}/{config.chat_completions_path.lstrip('/')}"


def _extract_error_message(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:500]

    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("type") or error.get("code")
        if message:
            return str(message)[:500]
    if isinstance(error, str):
        return error[:500]
    return response.text[:500]


def _stringify_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_answer(data: dict) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise AIAnswerResponseError("AI answer response missing choices")

    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if isinstance(message, dict):
        content = _stringify_content(message.get("content"))
    else:
        content = _stringify_content(choice.get("text") if isinstance(choice, dict) else None)

    content = content.strip()
    if not content:
        raise AIAnswerResponseError("AI answer response content is empty")
    return content


def answer_question(question: str) -> str:
    question = (question or "").strip()
    if not question:
        raise AIAnswerConfigError("question is required")

    config = load_ai_answer_config()
    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": question}],
        "stream": False,
    }
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    start_time = time.monotonic()
    try:
        response = _get_http_session(config).post(
            _chat_completions_url(config),
            headers=headers,
            json=payload,
            timeout=(config.connect_timeout, config.read_timeout),
        )
    except requests.Timeout as exc:
        raise AIAnswerError(
            f"AI answer service timed out after {config.read_timeout:.0f}s"
        ) from exc
    except requests.RequestException as exc:
        raise AIAnswerError(f"AI answer request failed: {exc}") from exc

    elapsed = time.monotonic() - start_time
    logger.info(
        "AI answer request finished: question_len=%s status=%s elapsed=%.2fs",
        len(question),
        response.status_code,
        elapsed,
    )

    if response.status_code >= 400:
        error_message = _extract_error_message(response)
        raise AIAnswerResponseError(
            f"AI answer service returned HTTP {response.status_code}: {error_message}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise AIAnswerResponseError("AI answer response is not valid JSON") from exc

    return _extract_answer(data)
