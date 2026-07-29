# Legacy `/messages` 日志降噪设计

## 目标

降低 Cursor 等 legacy SSE 客户端频繁发送 `POST /messages/` 造成的 TLS 日志量，同时保留故障诊断和真实工具用量统计。

## 设计

- ASGI 中间件旁路观察最多 64 KiB 请求体，不预读、不重放、不修改下游消息。
- 仅解析完整 JSON 对象的顶层 `method`，不记录 `params`、原始请求体或知识内容。
- 成功 `/messages` 按“客户端指纹 + method”采样，默认每 60 秒最多一条。
- 采样状态有最大 key 数，满额时淘汰最早状态，避免无界内存增长。
- 非 2xx、未处理异常、限流、`mcp_tool_call` 和 `knowledge_feedback` 保持逐条上传。
- `/health` 暴露累计抑制数，工具用量分析只使用 `mcp_tool_call`。

## 验收

- 分片请求体传给 MCP SDK 的字节完全不变。
- 同一客户端和方法在窗口内只有一条成功 HTTP 样本。
- 请求参数和原始问题不进入 HTTP 遥测字段。
- 相同错误请求不会被采样抑制。
- 完整单元测试、语法检查、UTF-8/no-BOM 和敏感信息检查通过。
