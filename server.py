import asyncio
import logging
import traceback
import json
from mcp.server import Server
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
import uvicorn

# 配置日志系统，确保在所有平台上输出UTF-8编码（与 rag.py 保持一致）
def setup_logging():
    # 移除已有的handler，避免重复设置
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # 创建自定义的StreamHandler子类，确保输出UTF-8编码
    class UTF8StreamHandler(logging.StreamHandler):
        def emit(self, record):
            try:
                msg = self.format(record)
                # 确保消息以UTF-8编码输出
                if isinstance(msg, str):
                    msg = msg.encode('utf-8').decode('utf-8')
                self.stream.write(msg + self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)

    handler = UTF8StreamHandler()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[handler]
    )

setup_logging()

# 创建专门的日志记录器
logger = logging.getLogger('m5doc_server')

# 1. 初始化 MCP Server
app_name = "m5-doc-server"
server = Server(app_name)

# ---------------------------------------------------------
# 2. 定义你的函数 
# ---------------------------------------------------------
from rag import retrieve_knowledge_text

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
- num: 根据问题涉及的实体数量设置（默认1）
  * 询问单个产品/功能 → 1
  * 对比2个产品 → 2
  * 询问"有哪些"/"多少种"/"所有" → 3
  * 多步骤操作或复杂问题 → 对应步骤数（最多5）
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
                    "num": {
                        "type": "integer", 
                        "description": "问题涉及的实体数量，影响返回结果的丰富度。单个产品/功能=1，对比2个=2，询问'有哪些/多少/所有'=3，多步骤问题=步骤数（1-5）。默认值: 1",
                        "default": 1,
                        "minimum": 1,
                        "maximum": 3
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
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """处理工具调用"""
    logger.info(f"=== 工具调用请求 ===")
    logger.info(f"工具名称: {name}")
    logger.info(f"调用参数: {json.dumps(arguments, ensure_ascii=False, default=str)}")

    if name == "knowledge_search":
        query = arguments.get("query") if arguments else None
        num = arguments.get("num", 1) if arguments else 1
        is_chip = arguments.get("is_chip", False) if arguments else False
        filter_type = arguments.get("filter_type", None) if arguments else None

        if not query:
            logger.warning("查询参数缺失")
            return [types.TextContent(type="text", text="错误：缺少查询参数")]

        try:
            logger.debug(f"开始知识库检索: query='{query}', num={num}, is_chip={is_chip}, filter_type='{filter_type}'")
            result = retrieve_knowledge_text(query, num=num, is_chip=is_chip, filter_type=filter_type)
            logger.info(f"知识库检索成功，返回结果长度: {len(str(result))} 字符")
            return [types.TextContent(type="text", text=str(result))]
        except Exception as e:
            logger.error(f"知识库检索失败: {str(e)}")
            logger.error(f"异常堆栈跟踪: {traceback.format_exc()}")
            return [types.TextContent(type="text", text=f"查询错误: {str(e)}")]

    logger.error(f"未知工具调用: {name}")
    raise ValueError(f"Unknown tool: {name}")

# ---------------------------------------------------------
# 4. 设置 Starlette 和 SSE 传输
# ---------------------------------------------------------
sse = SseServerTransport("/messages")

async def handle_sse(request):
    """SSE 端点"""
    async with sse.connect_sse(
        request.scope,
        request.receive,
        request._send
    ) as streams:
        init_options = InitializationOptions(
            server_name=app_name,
            server_version="0.1.1",
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability()
            )
        )
        await server.run(streams[0], streams[1], init_options)

# ✅ 创建 ASGI 应用而不是普通函数
class MessageHandler:
    """消息处理器 - 实现 ASGI 接口"""
    async def __call__(self, scope, receive, send):
        await sse.handle_post_message(scope, receive, send)

async def health(request):
    """健康检查"""
    return JSONResponse({"status": "ok", "server": app_name})

# 创建 Starlette 应用
app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Route("/messages", endpoint=MessageHandler(), methods=["POST"]),  # ✅ 使用 ASGI 应用
        Route("/health", endpoint=health, methods=["GET"]),
    ]
)

if __name__ == "__main__":
    print(f"🚀 Starting {app_name} on http://0.0.0.0:5058")
    print(f"📡 SSE endpoint: http://0.0.0.0:5058/sse")
    print(f"💬 Messages endpoint: http://0.0.0.0:5058/messages")
    uvicorn.run(app, host="0.0.0.0", port=5058)