import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import inspect
import logging
import os
import time
import uuid
from mcp.server import Server
import mcp.types as types
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
import uvicorn

from cloud_logging import (
    REQUEST_METADATA,
    RequestTelemetryMiddleware,
    cloud_logger,
    configure_cloud_logging,
    redact_sensitive_text,
    resolve_client_ip_from_scope,
)
from rate_limit import PerIpRateLimitMiddleware, rate_limiter

configure_cloud_logging(cloud_logger)

# 创建专门的日志记录器
logger = logging.getLogger('m5doc_server')

# 1. 初始化 MCP Server
app_name = "m5-doc-server"
server = Server(app_name, version="0.2.0")

MCP_TOOL_WORKERS = int(os.getenv("M5DOC_MCP_TOOL_WORKERS", "32"))
MCP_TOOL_TIMEOUT = float(os.getenv("M5DOC_MCP_TOOL_TIMEOUT", "90"))
MCP_TOOL_QUEUE_TIMEOUT = float(os.getenv("M5DOC_MCP_TOOL_QUEUE_TIMEOUT", "5"))

tool_executor = ThreadPoolExecutor(
    max_workers=MCP_TOOL_WORKERS,
    thread_name_prefix="m5doc-tool",
)
tool_semaphore = asyncio.Semaphore(MCP_TOOL_WORKERS)

# ---------------------------------------------------------
# 2. 定义你的函数 
# ---------------------------------------------------------
from ai_answer import AIAnswerError, answer_question
from rag import retrieve_knowledge_text

AI_TOOL_WORKERS = int(os.getenv("M5DOC_AI_TOOL_WORKERS", "8"))
AI_TOOL_TIMEOUT = float(os.getenv("M5DOC_AI_TOOL_TIMEOUT", "260"))
AI_TOOL_QUEUE_TIMEOUT = float(os.getenv("M5DOC_AI_TOOL_QUEUE_TIMEOUT", "5"))

ai_tool_executor = ThreadPoolExecutor(
    max_workers=AI_TOOL_WORKERS,
    thread_name_prefix="m5doc-ai-tool",
)
ai_tool_semaphore = asyncio.Semaphore(AI_TOOL_WORKERS)

AI_ANSWER_TOOL_NAME = "knowledge_answer"
FEEDBACK_TOOL_NAME = "knowledge_feedback"
MAX_LOG_INPUT_CHARS = int(os.getenv("M5DOC_LOG_INPUT_MAX_CHARS", "20000"))


class ToolQueueTimeout(Exception):
    """Raised when all workers stay occupied past the admission timeout."""


async def run_blocking_tool(semaphore, executor, call, timeout, queue_timeout):
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=queue_timeout)
    except asyncio.TimeoutError as exc:
        raise ToolQueueTimeout from exc

    future = asyncio.get_running_loop().run_in_executor(executor, call)
    release_on_return = True
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except asyncio.TimeoutError:
        # The worker thread cannot be cancelled safely. Keep its admission slot
        # until the underlying call really exits so overload cannot grow silently.
        future.add_done_callback(lambda _future: semaphore.release())
        release_on_return = False
        raise
    finally:
        if release_on_return:
            semaphore.release()


def current_mcp_metadata() -> dict[str, str]:
    try:
        context = server.request_context
    except LookupError:
        return {}
    params = context.session.client_params
    client_info = params.clientInfo if params else None
    return {
        "mcp_request_id": str(context.request_id),
        "mcp_protocol_version": str(params.protocolVersion) if params else "",
        "mcp_client_name": str(client_info.name) if client_info else "",
        "mcp_client_version": str(client_info.version) if client_info else "",
    }


def bounded_log_text(value, limit: int = MAX_LOG_INPUT_CHARS) -> tuple[str, bool]:
    text = redact_sensitive_text(str(value or ""))
    return text[:limit], len(text) > limit


def feedback_text(arguments: dict | None, key: str, limit: int) -> tuple[str, bool]:
    value = arguments.get(key) if arguments else None
    return bounded_log_text(str(value or "").strip(), limit)

# 3. 注册为 MCP 工具
@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """列出可用工具。"""
    return [
        types.Tool(
            name="knowledge_search",
            description='''从M5Stack产品知识库中检索相关信息。这是一个专业的M5Stack产品、硬件、编程和芯片数据库查询工具。
【核心功能】
- 查询M5Stack产品的技术规格、参数、功能特性
- 检索产品兼容性、连接方式、引脚定义
- 获取编程API、代码示例、固件配置信息
- 查找芯片数据手册和技术细节
【必须触发此工具的场景】
当用户询问涉及以下任何内容时，务必调用此工具：
1. M5Stack品牌及产品（Core、Atom、StickC、Paper、Dial、Capsule等系列）
2. 硬件技术（模块、传感器、执行器、连接器、引脚、GPIO、接口、通讯协议如I2C/SPI/UART）
3. 编程开发（API、UIFlow、Arduino、MicroPython、ESP-IDF、固件、库函数、代码示例）
4. 技术参数（电气特性、尺寸、重量、SKU、兼容性、供电、性能指标）
5. 芯片相关（ESP32、芯片型号、数据手册、寄存器、技术规格）
6. 产品对比、选型建议、功能差异
7. 常见嵌入式问题解答（FAQ）、故障排除
【参数使用指南】
- query: 用清晰的关键词描述查询内容，必要时结合上下文重构查询语句
- is_chip: 判断是否需要查询芯片数据手册
  * 明确提到芯片型号、数据手册、寄存器 → true
  * 询问底层技术原理、电气特性 → true
  * 仅询问产品使用、编程API → false
- filter_type: 指定查询的知识库类型
  * "product": 查询所有产品文档（包括在售和EOL产品）
  * "product_no_eol": 查询在售产品文档
  * "program": 查询全品类编程相关文档（包括Arduino、UIFlow、ESP-IDF）
  * "arduino": 专门查询Arduino开发相关文档
  * "uiflow": 专门查询UIFlow开发相关文档
  * "esp-idf": 专门查询ESP-IDF开发相关文档
  * "esphome": 查询ESPHome官方文档
            ''',
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "知识库查询文本。使用清晰的关键词，包含产品名称、技术术语或功能描述。如果用户问题模糊，需结合对话上下文优化查询语句。"
                    },
                    "is_chip": {
                        "type": "boolean",
                        "description": "是否需要查询芯片数据手册。当问题涉及芯片型号、数据手册、寄存器、底层电气特性时设为true；仅询问产品使用或API时设为false。默认值: false",
                        "default": False
                    },
                    "filter_type": {
                        "type": "string",
                        "description": "过滤类型，用于指定查询特定类型的知识库文档。可选值包括：'product'（产品文档）、'product_no_eol'（在售产品文档）、'program'（全品类编程文档）、'arduino'（Arduino开发文档）、'uiflow'（UIFlow开发文档）、'esp-idf'（ESP-IDF开发文档）、'esphome'（ESPHome官方文档）。默认值: None",
                        "enum": ["product", "product_no_eol", "program", "arduino", "uiflow", "esp-idf", "esphome"],
                        "default": None
                    }
                }
            }
        ),
        types.Tool(
            name=AI_ANSWER_TOOL_NAME,
            description='''调用 M5Stack FAE AI 生成专业回复，而不是直接返回杂乱的知识库片段。
【核心功能】
- 将用户原始问题直接发送给 M5Stack FAE 模型，由模型整理并输出完整答案
- 适合需要最终结论、故障排查步骤、选型建议、代码/API解释、产品使用说明的场景
- 返回内容是模型生成的专业回复，不包含原始检索片段
【使用建议】
- 当用户明确需要“直接回答”“帮我整理”“给出方案”“解释原因”时优先使用此工具
- 如果用户只需要快速检索原文资料、引用片段或低延迟查询，使用 knowledge_search
- 此工具可能耗时 1 分钟甚至更久；只要没有返回错误，客户端应继续等待
【参数】
- question: 用户发来的原始问题。除非必须补全上下文，否则不要把大量检索片段拼进去。
            ''',
            inputSchema={
                "type": "object",
                "required": ["question"],
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "用户原始问题，会直接转发给 M5Stack FAE AI 生成专业回复。"
                    }
                }
            }
        ),
        types.Tool(
            name=FEEDBACK_TOOL_NAME,
            description='''向 M5Stack 提交知识库缺失、错误、功能未覆盖或文档问题，供人工评估和后续补充。
【强烈建议反馈的场景】
- knowledge_search 没有找到用户需要的产品功能、API、接线、示例或说明
- knowledge_answer 无法回答，或现有资料疑似过期、错误、互相矛盾
- 发现 M5Stack 产品、SDK、UIFlow2、Arduino、ESP-IDF 文档中的 bug 或缺失步骤
- Agent 判断某个高频问题值得 M5Stack FAE 人工补充
【反馈原则】
- 鼓励 Agent 主动、大胆反馈真实问题，不必等用户明确要求
- 清楚写明缺少什么、哪里可能错误、期望补充什么
- 反馈将上传到 M5Stack 云日志并进入人工评估流程
            ''',
            inputSchema={
                "type": "object",
                "required": ["category", "feedback"],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "missing_documentation",
                            "incorrect_information",
                            "unsupported_feature",
                            "broken_example",
                            "tool_error",
                            "other"
                        ],
                        "description": "反馈分类。"
                    },
                    "feedback": {
                        "type": "string",
                        "minLength": 10,
                        "maxLength": 8000,
                        "description": "具体问题、缺失内容或疑似 bug，请提供足够信息供人工复现和评估。"
                    },
                    "original_question": {
                        "type": "string",
                        "maxLength": 8000,
                        "description": "触发本次反馈的用户原始问题，可选。"
                    },
                    "product": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "相关产品、芯片、Unit、Module 或 SDK 名称，可选。"
                    },
                    "expected_information": {
                        "type": "string",
                        "maxLength": 4000,
                        "description": "期望 M5Stack 后续补充或修正的资料，可选。"
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "default": "medium",
                        "description": "问题影响程度。"
                    },
                    "source_tool": {
                        "type": "string",
                        "enum": ["knowledge_search", "knowledge_answer", "other"],
                        "default": "knowledge_search",
                        "description": "发现问题时使用的工具。"
                    }
                }
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """处理工具调用"""
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
                return [types.TextContent(type="text", text="错误：缺少查询参数")]

            call = partial(
                retrieve_knowledge_text,
                query,
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
                return [types.TextContent(type="text", text="服务当前繁忙，请稍后重试")]
            except asyncio.TimeoutError:
                outcome = "timeout"
                error_type = "tool_timeout"
                return [types.TextContent(type="text", text=f"Query timed out after {MCP_TOOL_TIMEOUT:.0f}s")]
            except Exception as exc:
                error_type = type(exc).__name__
                logger.error("knowledge_search failed: error_type=%s", error_type)
                return [types.TextContent(type="text", text=f"查询错误: {str(exc)}")]

            outcome = "success"
            result_len = len(str(result))
            return [types.TextContent(type="text", text=str(result))]

        if name == AI_ANSWER_TOOL_NAME:
            question = arguments.get("question") if arguments else None
            query_len = len(str(question or ""))
            input_text, input_truncated = bounded_log_text(question)

            if not question:
                outcome = "invalid_arguments"
                error_type = "missing_question"
                return [types.TextContent(type="text", text="错误：缺少 question 参数")]

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
                return [types.TextContent(type="text", text="AI回答服务当前繁忙，请稍后重试")]
            except asyncio.TimeoutError:
                outcome = "timeout"
                error_type = "tool_timeout"
                return [types.TextContent(type="text", text=f"AI回答超时：已等待 {AI_TOOL_TIMEOUT:.0f}s，建议客户端延长等待时间或改用 knowledge_search 快速检索。")]
            except AIAnswerError as exc:
                error_type = type(exc).__name__
                logger.error("knowledge_answer failed: error_type=%s", error_type)
                return [types.TextContent(type="text", text=f"AI回答错误: {str(exc)}")]
            except Exception as exc:
                error_type = type(exc).__name__
                logger.error("knowledge_answer failed: error_type=%s", error_type)
                return [types.TextContent(type="text", text=f"AI回答错误: {str(exc)}")]

            outcome = "success"
            result_len = len(str(result))
            return [types.TextContent(type="text", text=str(result))]

        if name == FEEDBACK_TOOL_NAME:
            category = str(arguments.get("category") or "") if arguments else ""
            severity = str(arguments.get("severity") or "medium") if arguments else "medium"
            source_tool = (
                str(arguments.get("source_tool") or "knowledge_search")
                if arguments
                else "knowledge_search"
            )
            input_text, input_truncated = feedback_text(arguments, "feedback", 8000)
            original_question, original_question_truncated = feedback_text(
                arguments,
                "original_question",
                8000,
            )
            product, product_truncated = feedback_text(arguments, "product", 200)
            expected_information, expected_information_truncated = feedback_text(
                arguments,
                "expected_information",
                4000,
            )
            query_len = len(input_text)

            allowed_categories = {
                "missing_documentation",
                "incorrect_information",
                "unsupported_feature",
                "broken_example",
                "tool_error",
                "other",
            }
            if category not in allowed_categories:
                outcome = "invalid_arguments"
                error_type = "invalid_feedback_category"
                return [types.TextContent(type="text", text="反馈失败：category 不合法")]
            if len(input_text) < 10:
                outcome = "invalid_arguments"
                error_type = "feedback_too_short"
                return [types.TextContent(type="text", text="反馈失败：feedback 至少需要 10 个字符")]
            if severity not in {"low", "medium", "high"}:
                outcome = "invalid_arguments"
                error_type = "invalid_feedback_severity"
                return [types.TextContent(type="text", text="反馈失败：severity 不合法")]
            if source_tool not in {"knowledge_search", "knowledge_answer", "other"}:
                outcome = "invalid_arguments"
                error_type = "invalid_feedback_source_tool"
                return [types.TextContent(type="text", text="反馈失败：source_tool 不合法")]

            feedback_id = uuid.uuid4().hex
            mcp_metadata = current_mcp_metadata()
            accepted = cloud_logger.emit(
                "knowledge_feedback",
                **{**request_metadata, **mcp_metadata},
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
                return [
                    types.TextContent(
                        type="text",
                        text="反馈暂未保存：云日志队列不可用，请稍后重试。",
                    )
                ]

            outcome = "success"
            response_text = (
                f"反馈已接收并排队进入 M5Stack 人工评估流程。feedback_id={feedback_id}"
            )
            result_len = len(response_text)
            return [types.TextContent(type="text", text=response_text)]

        error_type = "unknown_tool"
        raise ValueError(f"Unknown tool: {name}")
    finally:
        active_after = cloud_logger.activity_exit("tool")
        mcp_metadata = current_mcp_metadata()
        event_metadata = {**request_metadata, **mcp_metadata}
        cloud_logger.emit(
            "mcp_tool_call",
            **event_metadata,
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

# ---------------------------------------------------------
# 4. 设置 Starlette 和 SSE 传输
# ---------------------------------------------------------
sse = SseServerTransport("/messages")

def create_streamable_http_manager():
    manager_options = {
        "app": server,
        "json_response": True,
        "stateless": False,
        "session_idle_timeout": 1800,
    }
    supported_options = inspect.signature(StreamableHTTPSessionManager).parameters
    return StreamableHTTPSessionManager(
        **{
            key: value
            for key, value in manager_options.items()
            if key in supported_options
        }
    )

streamable_http = create_streamable_http_manager()

class SSEHandler:
    """SSE 端点 - 实现 ASGI 接口，避免 Starlette 用 request_response 包装导致连接关闭时报错"""
    async def __call__(self, scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

class MessageHandler:
    """消息处理器 - 实现 ASGI 接口"""
    async def __call__(self, scope, receive, send):
        await sse.handle_post_message(scope, receive, send)

class StreamableHTTPHandler:
    """Streamable HTTP MCP endpoint for clients that do not need SSE responses."""
    async def __call__(self, scope, receive, send):
        await streamable_http.handle_request(scope, receive, send)

async def health(request):
    """健康检查"""
    return JSONResponse({
        "status": "ok",
        "server": app_name,
        "cloud_logging": cloud_logger.status(),
        "rate_limit": rate_limiter.status(),
    })

@contextlib.asynccontextmanager
async def lifespan(app):
    cloud_logger.start()
    try:
        if hasattr(streamable_http, "run"):
            async with streamable_http.run():
                yield
        else:
            yield
    finally:
        tool_executor.shutdown(wait=False, cancel_futures=True)
        ai_tool_executor.shutdown(wait=False, cancel_futures=True)
        cloud_logger.stop()

# 创建 Starlette 应用
starlette_app = Starlette(
    routes=[
        Route("/sse", endpoint=SSEHandler(), methods=["GET"]),
        Route("/messages", endpoint=MessageHandler(), methods=["POST"]),
        Route("/mcp", endpoint=StreamableHTTPHandler(), methods=["GET", "POST", "DELETE"]),
        Route("/health", endpoint=health, methods=["GET"]),
    ],
    lifespan=lifespan,
)
rate_limited_app = PerIpRateLimitMiddleware(
    starlette_app,
    rate_limiter,
    client_ip_getter=lambda scope: resolve_client_ip_from_scope(
        scope,
        cloud_logger.config,
    )[0],
    on_blocked=lambda event, **fields: cloud_logger.emit(
        event,
        **{
            **fields,
            "client_ip": (
                fields.get("client_ip", "")
                if cloud_logger.config.collect_client_ip
                else ""
            ),
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
