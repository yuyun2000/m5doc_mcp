# ADR 0002: MCP SDK v2 双协议与双传输架构

- 状态：已采用
- 日期：2026-07-29

## 背景

服务必须兼容既有 `/sse`、`/messages` 客户端，同时支持 MCP 2026-07-28 的
`server/discover`、无会话 Streamable HTTP 和结构化工具结果。业务侧的知识检索与
AI 回答具有不同耗时和并发特征，不能共享一个容易被长请求占满的执行池。

## 决策

1. 使用官方 `mcp==2.0.0` 和 `MCPServer` 注册类型化工具。
2. `/mcp` 使用 `streamable_http_app(json_response=True, stateless_http=True)`；SDK 自动在
   2026-07-28 新协议与旧 `initialize` 协议之间路由。
3. `/sse`、`/messages` 继续使用 SDK 的 SSE 兼容应用。
4. 顶层 Starlette lifespan 显式运行 `mcp.session_manager.run()`，并管理云日志与线程池。
5. 工具同时返回旧客户端可读的文本内容和新客户端可读的 `structuredContent`。
6. 检索和 AI 回答继续使用独立、有界的线程池与准入超时。
7. MCP 传输强制校验 Host、Origin、Content-Type 和 4 MiB 请求体上限。

## 影响

- 旧客户端无需修改 URL 或工具参数。
- 新 `/mcp` 请求不依赖粘性会话，可横向扩展业务实例。
- 旧 SSE 长连接在多实例部署时仍需要负载均衡器保持连接及消息路由一致。
- 当前来源 IP 限流仍是单进程状态；扩展到多 worker/多实例前必须迁移到 Redis 等共享存储。

