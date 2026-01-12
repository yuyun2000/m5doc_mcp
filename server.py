import asyncio
from mcp.server import Server
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server.fastapi import FastApiServerTransport
from fastapi import FastAPI
import uvicorn

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
3. 编程开发（API、UIFlow、Arduino、MicroPython、固件、库函数、代码示例）
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
                    }
                }
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    if name == "knowledge_search":
        # 获取 AI 传入的参数
        query = arguments.get("query")
        num = arguments.get("num", 1)
        is_chip = arguments.get("is_chip", False)
        # 调用你的函数
        result = retrieve_knowledge_text(query, num=num, is_chip=is_chip)
        
        # 返回结果给 AI
        return [types.TextContent(type="text", text=str(result))]
    raise ValueError(f"Unknown tool: {name}")

# ---------------------------------------------------------
# 4. 设置 FastAPI 和 SSE 传输
# ---------------------------------------------------------
app = FastAPI(title=app_name)
transport = FastApiServerTransport(server)

@app.get("/sse")
async def sse():
    async with transport.connect_sse() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=app_name,
                server_version="0.1.0",
                capabilities=server.get_capabilities()
            )
        )

@app.post("/messages")
async def messages():
    await transport.handle_post_message()

if __name__ == "__main__":
    # 在 5058 端口启动
    uvicorn.run(app, host="0.0.0.0", port=5058)