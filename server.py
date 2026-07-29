"""M5Stack documentation MCP service built on the official MCP SDK v2."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Annotated, Literal

import uvicorn
from mcp import types
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from ai_answer import AIAnswerError, answer_question
from cloud_logging import (
    REQUEST_METADATA,
    RequestTelemetryMiddleware,
    cloud_logger,
    configure_cloud_logging,
    redact_sensitive_text,
    resolve_client_ip_from_scope,
)
from mcp_config import load_mcp_server_config
from rag import retrieve_knowledge_text
from rate_limit import PerIpRateLimitMiddleware, rate_limiter

configure_cloud_logging(cloud_logger)
logger = logging.getLogger("m5doc_server")

APP_NAME = "m5-doc-server"
APP_VERSION = "1.0.0"
AI_ANSWER_TOOL_NAME = "knowledge_answer"
FEEDBACK_TOOL_NAME = "knowledge_feedback"
MAX_LOG_INPUT_CHARS = int(os.getenv("M5DOC_LOG_INPUT_MAX_CHARS", "20000"))

MCP_TOOL_WORKERS = int(os.getenv("M5DOC_MCP_TOOL_WORKERS", "32"))
MCP_TOOL_TIMEOUT = float(os.getenv("M5DOC_MCP_TOOL_TIMEOUT", "90"))
MCP_TOOL_QUEUE_TIMEOUT = float(os.getenv("M5DOC_MCP_TOOL_QUEUE_TIMEOUT", "5"))
AI_TOOL_WORKERS = int(os.getenv("M5DOC_AI_TOOL_WORKERS", "8"))
AI_TOOL_TIMEOUT = float(os.getenv("M5DOC_AI_TOOL_TIMEOUT", "260"))
AI_TOOL_QUEUE_TIMEOUT = float(os.getenv("M5DOC_AI_TOOL_QUEUE_TIMEOUT", "5"))

tool_executor = ThreadPoolExecutor(
    max_workers=MCP_TOOL_WORKERS, thread_name_prefix="m5doc-tool"
)
tool_semaphore = asyncio.Semaphore(MCP_TOOL_WORKERS)
ai_tool_executor = ThreadPoolExecutor(
    max_workers=AI_TOOL_WORKERS, thread_name_prefix="m5doc-ai-tool"
)
ai_tool_semaphore = asyncio.Semaphore(AI_TOOL_WORKERS)


class ToolQueueTimeout(Exception):
    """Raised when all workers stay occupied past the admission timeout."""


class BasicToolOutput(BaseModel):
    text: str
    outcome: str


class FeedbackToolOutput(BasicToolOutput):
    accepted: bool
    feedback_id: str | None = None


FilterType = Literal[
    "product",
    "product_no_eol",
    "program",
    "arduino",
    "uiflow",
    "esp-idf",
    "esphome",
]
FeedbackCategory = Literal[
    "missing_documentation",
    "incorrect_information",
    "unsupported_feature",
    "broken_example",
    "tool_error",
    "other",
]
FeedbackSeverity = Literal["low", "medium", "high"]
FeedbackSourceTool = Literal["knowledge_search", "knowledge_answer", "other"]


async def run_blocking_tool(semaphore, executor, call, timeout, queue_timeout):
    """Run blocking work with bounded admission and a non-leaking timeout."""
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=queue_timeout)
    except asyncio.TimeoutError as exc:
        raise ToolQueueTimeout from exc

    future = asyncio.get_running_loop().run_in_executor(executor, call)
    release_on_return = True
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except asyncio.TimeoutError:
        # Python cannot safely cancel a running thread. Retain its admission slot
        # until it exits so repeated timeouts cannot grow unbounded work.
        future.add_done_callback(lambda _future: semaphore.release())
        release_on_return = False
        raise
    except asyncio.CancelledError:
        # A disconnected client cancels only the coroutine, not the worker thread.
        # Keep accounting for that worker until the blocking call actually exits.
        future.add_done_callback(lambda _future: semaphore.release())
        release_on_return = False
        raise
    finally:
        if release_on_return:
            semaphore.release()


def current_mcp_metadata(ctx: Context | None = None) -> dict[str, str]:
    if ctx is None:
        return {}
    try:
        params = ctx.session.client_params
        client_info = getattr(params, "client_info", None) or getattr(
            params, "clientInfo", None
        )
        return {
            "mcp_request_id": ctx.request_id,
            "mcp_protocol_version": str(ctx.protocol_version or ""),
            "mcp_client_name": str(getattr(client_info, "name", "") or ""),
            "mcp_client_version": str(getattr(client_info, "version", "") or ""),
        }
    except (AttributeError, LookupError, ValueError):
        return {}


def bounded_log_text(value, limit: int = MAX_LOG_INPUT_CHARS) -> tuple[str, bool]:
    text = redact_sensitive_text(str(value or ""))
    return text[:limit], len(text) > limit


def feedback_text(arguments: dict | None, key: str, limit: int) -> tuple[str, bool]:
    value = arguments.get(key) if arguments else None
    return bounded_log_text(str(value or "").strip(), limit)


def make_result(
    text: str,
    outcome: str,
    *,
    is_error: bool = False,
    **structured_fields,
) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content={"text": text, "outcome": outcome, **structured_fields},
        is_error=is_error,
    )


mcp = MCPServer(
    name=APP_NAME,
    title="M5Stack Official Documentation",
    description="M5Stack 官方产品、硬件、编程和芯片资料服务。",
    instructions=(
        "优先使用 knowledge_search 获取低延迟原始资料；需要整理后的完整答复时使用 "
        "knowledge_answer。资料缺失、错误、示例损坏或工具异常时，应主动调用 "
        "knowledge_feedback 提交可复现反馈。"
    ),
    version=APP_VERSION,
)
# Preserve the old import surface for code that imports server.server.
server = mcp


SEARCH_DESCRIPTION = """从 M5Stack 官方知识库检索产品、硬件、编程和芯片资料。

适用于产品规格、SKU、接口引脚、GPIO、供电、电气特性、兼容性、选型、Arduino、
UIFlow/UIFlow2、MicroPython、ESP-IDF、ESPHome、代码示例、芯片手册和故障排除。
如果检索不到需要的功能、说明或示例，请继续调用 knowledge_feedback 主动反馈。"""

ANSWER_DESCRIPTION = """调用 M5Stack FAE AI，将原始问题整理成可直接使用的专业答复。

复杂问题可能耗时一分钟以上，客户端应等待工具完成。只需要低延迟原文片段时使用
knowledge_search；回答缺失、错误或无法复现时请调用 knowledge_feedback。"""

FEEDBACK_DESCRIPTION = """向 M5Stack 提交知识缺失、内容错误、未支持功能、损坏示例或工具 bug。

反馈会重点写入云日志并进入人工评估。鼓励 Agent 在检索或回答不完整时大胆反馈，说明
原始问题、相关产品、缺失内容、复现方式和期望资料，不必等待用户明确要求。"""


async def _execute_tool(
    name: str,
    arguments: dict | None,
    ctx: Context | None = None,
) -> types.CallToolResult:
    started = time.monotonic()
    active_tools, peak_tools = cloud_logger.activity_enter("tool")
    request_metadata = dict(REQUEST_METADATA.get())
    outcome = "error"
    error_type = ""
    result_len = 0
    query_len = 0
    filter_type = ""
    is_chip = False
    input_text = ""
    input_truncated = False
    feedback_id = ""

    try:
        if name == "knowledge_search":
            query = arguments.get("query") if arguments else None
            is_chip = bool(arguments.get("is_chip", False)) if arguments else False
            filter_type = str(arguments.get("filter_type") or "") if arguments else ""
            query_len = len(str(query or ""))
            input_text, input_truncated = bounded_log_text(query)
            if not query:
                outcome = "invalid_arguments"
                error_type = "missing_query"
                return make_result("错误：缺少查询参数", outcome, is_error=True)

            call = partial(
                retrieve_knowledge_text,
                str(query),
                is_chip=is_chip,
                filter_type=filter_type or None,
            )
            try:
                result = await run_blocking_tool(
                    tool_semaphore,
                    tool_executor,
                    call,
                    MCP_TOOL_TIMEOUT,
                    MCP_TOOL_QUEUE_TIMEOUT,
                )
            except ToolQueueTimeout:
                outcome = "overloaded"
                error_type = "queue_timeout"
                return make_result("服务当前繁忙，请稍后重试", outcome, is_error=True)
            except asyncio.TimeoutError:
                outcome = "timeout"
                error_type = "tool_timeout"
                return make_result(
                    f"查询超时：已等待 {MCP_TOOL_TIMEOUT:.0f} 秒，请稍后重试",
                    outcome,
                    is_error=True,
                )
            except Exception as exc:  # noqa: BLE001 - external SDK boundary
                error_type = type(exc).__name__
                logger.error("knowledge_search failed: error_type=%s", error_type)
                return make_result(
                    "查询服务暂时不可用，请稍后重试", outcome, is_error=True
                )

            outcome = "success"
            result_text = str(result)
            result_len = len(result_text)
            return make_result(result_text, outcome)

        if name == AI_ANSWER_TOOL_NAME:
            question = arguments.get("question") if arguments else None
            query_len = len(str(question or ""))
            input_text, input_truncated = bounded_log_text(question)
            if not question:
                outcome = "invalid_arguments"
                error_type = "missing_question"
                return make_result("错误：缺少 question 参数", outcome, is_error=True)

            call = partial(answer_question, str(question))
            try:
                result = await run_blocking_tool(
                    ai_tool_semaphore,
                    ai_tool_executor,
                    call,
                    AI_TOOL_TIMEOUT,
                    AI_TOOL_QUEUE_TIMEOUT,
                )
            except ToolQueueTimeout:
                outcome = "overloaded"
                error_type = "queue_timeout"
                return make_result(
                    "AI 回答服务当前繁忙，请稍后重试", outcome, is_error=True
                )
            except asyncio.TimeoutError:
                outcome = "timeout"
                error_type = "tool_timeout"
                return make_result(
                    f"AI 回答超时：已等待 {AI_TOOL_TIMEOUT:.0f} 秒；可改用 knowledge_search 快速检索",
                    outcome,
                    is_error=True,
                )
            except AIAnswerError as exc:
                error_type = type(exc).__name__
                logger.error("knowledge_answer failed: error_type=%s", error_type)
                return make_result(
                    "AI 回答服务暂时不可用，请稍后重试", outcome, is_error=True
                )
            except Exception as exc:  # noqa: BLE001 - external AI provider boundary
                error_type = type(exc).__name__
                logger.error("knowledge_answer failed: error_type=%s", error_type)
                return make_result(
                    "AI 回答服务暂时不可用，请稍后重试", outcome, is_error=True
                )

            outcome = "success"
            result_text = str(result)
            result_len = len(result_text)
            return make_result(result_text, outcome)

        if name == FEEDBACK_TOOL_NAME:
            category = str(arguments.get("category") or "") if arguments else ""
            severity = (
                str(arguments.get("severity") or "medium") if arguments else "medium"
            )
            source_tool = (
                str(arguments.get("source_tool") or "knowledge_search")
                if arguments
                else "knowledge_search"
            )
            input_text, input_truncated = feedback_text(arguments, "feedback", 8000)
            original_question, original_question_truncated = feedback_text(
                arguments, "original_question", 8000
            )
            product, product_truncated = feedback_text(arguments, "product", 200)
            expected_information, expected_information_truncated = feedback_text(
                arguments,
                "expected_information",
                4000,
            )
            query_len = len(input_text)

            if category not in {
                "missing_documentation",
                "incorrect_information",
                "unsupported_feature",
                "broken_example",
                "tool_error",
                "other",
            }:
                outcome = "invalid_arguments"
                error_type = "invalid_feedback_category"
                return make_result(
                    "反馈失败：category 不合法", outcome, is_error=True, accepted=False
                )
            if len(input_text) < 10:
                outcome = "invalid_arguments"
                error_type = "feedback_too_short"
                return make_result(
                    "反馈失败：feedback 至少需要 10 个字符",
                    outcome,
                    is_error=True,
                    accepted=False,
                )
            if severity not in {"low", "medium", "high"}:
                outcome = "invalid_arguments"
                error_type = "invalid_feedback_severity"
                return make_result(
                    "反馈失败：severity 不合法", outcome, is_error=True, accepted=False
                )
            if source_tool not in {"knowledge_search", "knowledge_answer", "other"}:
                outcome = "invalid_arguments"
                error_type = "invalid_feedback_source_tool"
                return make_result(
                    "反馈失败：source_tool 不合法",
                    outcome,
                    is_error=True,
                    accepted=False,
                )

            feedback_id = uuid.uuid4().hex
            accepted = cloud_logger.emit(
                "knowledge_feedback",
                **{**request_metadata, **current_mcp_metadata(ctx)},
                feedback_id=feedback_id,
                category=category,
                severity=severity,
                source_tool=source_tool,
                feedback=input_text,
                feedback_truncated=input_truncated,
                original_question=original_question,
                original_question_truncated=original_question_truncated,
                product=product,
                product_truncated=product_truncated,
                expected_information=expected_information,
                expected_information_truncated=expected_information_truncated,
                manual_review=True,
                priority="high",
                review_status="pending",
            )
            if not accepted:
                outcome = "storage_unavailable"
                error_type = "cloud_log_queue_unavailable"
                return make_result(
                    "反馈暂未保存：云日志队列不可用，请稍后重试。",
                    outcome,
                    is_error=True,
                    accepted=False,
                    feedback_id=None,
                )

            outcome = "success"
            response_text = (
                f"反馈已接收并排队进入 M5Stack 人工评估流程。feedback_id={feedback_id}"
            )
            result_len = len(response_text)
            return make_result(
                response_text,
                outcome,
                accepted=True,
                feedback_id=feedback_id,
            )

        error_type = "unknown_tool"
        raise ValueError(f"Unknown tool: {name}")
    finally:
        active_after = cloud_logger.activity_exit("tool")
        cloud_logger.emit(
            "mcp_tool_call",
            **{**request_metadata, **current_mcp_metadata(ctx)},
            tool_name=name,
            outcome=outcome,
            error_type=error_type,
            query_len=query_len,
            input_text=input_text,
            input_truncated=input_truncated,
            result_len=result_len,
            filter_type=filter_type,
            is_chip=is_chip,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            active_tools_at_start=active_tools,
            peak_tools=peak_tools,
            active_tools_after=active_after,
            feedback_id=feedback_id,
        )


@mcp.tool(
    name="knowledge_search",
    title="M5Stack 知识检索",
    description=SEARCH_DESCRIPTION,
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
    structured_output=True,
)
async def knowledge_search_tool(
    query: Annotated[
        str,
        Field(min_length=1, max_length=20000, description="知识库查询词或用户原始问题"),
    ],
    is_chip: Annotated[
        bool, Field(description="是否需要检索芯片手册和底层电气资料")
    ] = False,
    filter_type: Annotated[
        FilterType | None, Field(description="限定检索的知识库类型")
    ] = None,
    ctx: Context | None = None,
) -> Annotated[types.CallToolResult, BasicToolOutput]:
    return await _execute_tool(
        "knowledge_search",
        {"query": query, "is_chip": is_chip, "filter_type": filter_type},
        ctx,
    )


@mcp.tool(
    name=AI_ANSWER_TOOL_NAME,
    title="M5Stack FAE 专业回答",
    description=ANSWER_DESCRIPTION,
    annotations=types.ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
    structured_output=True,
)
async def knowledge_answer_tool(
    question: Annotated[
        str, Field(min_length=1, max_length=20000, description="用户原始问题")
    ],
    ctx: Context | None = None,
) -> Annotated[types.CallToolResult, BasicToolOutput]:
    return await _execute_tool(AI_ANSWER_TOOL_NAME, {"question": question}, ctx)


@mcp.tool(
    name=FEEDBACK_TOOL_NAME,
    title="反馈给 M5Stack FAE",
    description=FEEDBACK_DESCRIPTION,
    annotations=types.ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
    structured_output=True,
)
async def knowledge_feedback_tool(
    category: Annotated[FeedbackCategory, Field(description="反馈分类")],
    feedback: Annotated[
        str, Field(min_length=10, max_length=8000, description="可复现的问题或缺失资料")
    ],
    original_question: Annotated[
        str | None, Field(max_length=8000, description="触发反馈的原始问题")
    ] = None,
    product: Annotated[
        str | None,
        Field(max_length=200, description="相关产品、芯片、Unit、Module 或 SDK"),
    ] = None,
    expected_information: Annotated[
        str | None, Field(max_length=4000, description="期望补充或修正的资料")
    ] = None,
    severity: Annotated[FeedbackSeverity, Field(description="影响程度")] = "medium",
    source_tool: Annotated[
        FeedbackSourceTool, Field(description="发现问题时使用的工具")
    ] = "knowledge_search",
    ctx: Context | None = None,
) -> Annotated[types.CallToolResult, FeedbackToolOutput]:
    return await _execute_tool(
        FEEDBACK_TOOL_NAME,
        {
            "category": category,
            "feedback": feedback,
            "original_question": original_question,
            "product": product,
            "expected_information": expected_information,
            "severity": severity,
            "source_tool": source_tool,
        },
        ctx,
    )


async def list_tools():
    """Compatibility helper retained for existing tests and integrations."""
    return await mcp.list_tools()


async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent]:
    """Compatibility helper returning the legacy text-content list."""
    result = await _execute_tool(name, arguments)
    return [item for item in result.content if isinstance(item, types.TextContent)]


mcp_server_config = load_mcp_server_config()
transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=list(mcp_server_config.allowed_hosts),
    allowed_origins=list(mcp_server_config.allowed_origins),
)

sse_transport_app = mcp.sse_app(
    sse_path="/sse",
    message_path="/messages/",
    host="0.0.0.0",
    transport_security=transport_security,
)
streamable_transport_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=mcp_server_config.json_response,
    stateless_http=mcp_server_config.stateless_http,
    max_request_body_size=mcp_server_config.max_request_body_size,
    host="0.0.0.0",
    transport_security=transport_security,
)


class LegacyMessagePathMiddleware:
    """Serve the legacy no-slash message URL without a 307 redirect."""

    def __init__(self, wrapped_app):
        self.wrapped_app = wrapped_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/messages":
            scope = dict(scope)
            scope["path"] = "/messages/"
            scope["raw_path"] = b"/messages/"
        await self.wrapped_app(scope, receive, send)


async def health(_request):
    return JSONResponse(
        {
            "status": "ok",
            "server": APP_NAME,
            "version": APP_VERSION,
            "mcp_sdk": "2.0.0",
            "transports": {
                "streamable_http": "/mcp",
                "legacy_sse": "/sse",
                "legacy_messages": "/messages",
                "stateless_http": mcp_server_config.stateless_http,
                "json_response": mcp_server_config.json_response,
            },
            "cloud_logging": cloud_logger.status(),
            "rate_limit": rate_limiter.status(),
        }
    )


@contextlib.asynccontextmanager
async def lifespan(_app):
    cloud_logger.start()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        tool_executor.shutdown(wait=False, cancel_futures=True)
        ai_tool_executor.shutdown(wait=False, cancel_futures=True)
        cloud_logger.stop()


starlette_app = Starlette(
    routes=[
        *sse_transport_app.routes,
        *streamable_transport_app.routes,
        Route("/health", endpoint=health, methods=["GET"]),
    ],
    lifespan=lifespan,
)
message_compatible_app = LegacyMessagePathMiddleware(starlette_app)
rate_limited_app = PerIpRateLimitMiddleware(
    message_compatible_app,
    rate_limiter,
    client_ip_getter=lambda scope: resolve_client_ip_from_scope(
        scope, cloud_logger.config
    )[0],
    on_blocked=lambda event, **fields: cloud_logger.emit(
        event,
        **{
            **fields,
            "client_ip": fields.get("client_ip", "")
            if cloud_logger.config.collect_client_ip
            else "",
        },
    ),
)
app = RequestTelemetryMiddleware(rate_limited_app, cloud_logger)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=5058,
        access_log=False,
        log_config=None,
        backlog=int(os.getenv("M5DOC_UVICORN_BACKLOG", "2048")),
        timeout_keep_alive=int(os.getenv("M5DOC_UVICORN_KEEP_ALIVE", "10")),
    )
