# MCP Python SDK v2 升级实施计划

## 目标

在保持旧 SSE 地址、工具名称、参数和文本结果兼容的前提下，升级到官方 MCP Python SDK
2.0，并支持 2026-07-28 协议、结构化输出、无会话 HTTP 和传输安全校验。

## 实施项

1. 固定 `mcp==2.0.0` 与生产使用的 `volcengine==1.0.123`。
2. 将低层 `Server` 手工 schema 改为 `MCPServer` 类型化工具。
3. 组合 `/mcp`、`/sse`、`/messages` 和 `/health`，统一生命周期、限流和遥测。
4. 保持检索/AI 独立并发池，超时后直到工作线程结束才释放准入额度。
5. 增加现代 discover、旧 initialize、schema、路由、安全、请求体和日志噪声测试。
6. 更新配置模板、README、Nginx 示例和服务启动依赖检查。

## 验收

- `python -m py_compile server.py rag.py ai_answer.py cloud_logging.py rate_limit.py mcp_config.py`
- `python -m unittest discover -s tests -v`
- 隔离环境中确认 `mcp==2.0.0`，并对 `/mcp` 执行新旧协议请求。
- UTF-8 无 BOM 校验与 tracked 文件密钥扫描通过。

