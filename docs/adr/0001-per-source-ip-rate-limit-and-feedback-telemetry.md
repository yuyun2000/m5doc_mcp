# ADR-0001: 按来源 IP 限流并上传反馈遥测

## Status

Accepted

## Context

M5Stack 文档 MCP 服务需要在不影响正常并发的前提下，阻止单一调用方持续刷接口，同时收集原始查询和 Agent 反馈，便于统计用量、定位资料缺失并安排人工补充。

服务位于 Nginx 后方，必须区分 TCP peer 和真实来源 IP。服务器 `47.113.125.164` 也会主动调用 MCP，不应受限。现有部署是单个 Python/Uvicorn 进程，云日志采用有界内存队列异步上传。

## Decision

- 仅使用可信代理解析出的 `client_ip` 作为限流键。指纹、User-Agent、MCP 会话、查询内容均不参与限流。
- `/sse`、`/messages`、`/mcp` 共用同一组按 IP 独立的 token bucket；`/health` 不限流。
- 默认每个 IP 每分钟补充 120 个 token，bucket 容量为 30。额度耗尽时返回 HTTP 429 和 `Retry-After`。
- `47.113.125.164` 作为来源 IP 白名单直接放行，不创建或消耗 bucket。
- 只在直连 peer 属于 `trusted_proxies` 时读取 `X-Real-IP` 或 `X-Forwarded-For`，防止客户端伪造来源 IP 绕过限流。
- 当前使用线程安全的单进程内存 bucket，并限制最大跟踪 IP 数和闲置 TTL。启用多个 worker 或多个服务实例前，必须迁移到 Redis 等共享原子计数存储。
- `knowledge_search` 查询和 `knowledge_answer` 问题在常见凭据脱敏及长度截断后上传 TLS。知识库返回正文和 AI 回答正文不上传。
- `knowledge_feedback` 反馈以 `manual_review=true`、`priority=high`、`review_status=pending` 上传 TLS；云日志队列拒绝时工具明确返回未保存，避免向 Agent 假报成功。
- 普通遥测上传失败不得阻塞 MCP 业务；反馈工具因其持久化语义采用 fail-closed 行为。

## Consequences

### Positive

- 每个来源 IP 额度互不影响，单一调用方无法耗尽其他用户的配额。
- 服务器自调用不会被误限流。
- 可信代理边界使公网客户端无法通过伪造转发头随意更换限流身份。
- 原始问题和高优先级反馈可用于用量分析、内容维护和人工评估。
- 有界状态和异步日志上传避免无限内存增长或 TLS 抖动拖慢请求。

### Negative

- NAT 后多个真实用户会共享一个公网 IP 配额。
- 单进程 bucket 不支持多 worker/多实例的一致限流。
- 原始查询可能包含个人信息，需要严格控制 TLS Topic 权限和保留周期。
- 服务重启会清空当前限流状态。

### Neutral

- 加盐指纹继续用于统计近似去重，但与限流决策完全解耦。
- 长连接只在建立 `/sse` 请求时消耗一次额度，后续 `/messages` 请求分别计数。

## Alternatives Considered

**使用 IP 加指纹作为限流键**

- 拒绝：同一 IP 会因 User-Agent 或语言变化拆成多个身份，容易绕过，也不符合每个来源 IP 单独限流的要求。

**立即引入 Redis**

- 暂不采用：当前是单进程部署，引入外部共享存储会增加故障面和运维成本。多 worker 或多实例上线前再迁移。

**仅在 Nginx 层限流**

- 暂不采用：应用层需要统一覆盖 SSE 与 Streamable HTTP 路由，并在 `/health` 和云日志中暴露限流状态；Nginx 仍负责规范化真实来源 IP。

## References

- `rate_limit.py`
- `cloud_logging.py`
- `server.py`
- `nginx_config.conf`
