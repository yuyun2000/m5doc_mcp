# m5doc_mcp
m5官方文档的mcp服务器

- 最新 Streamable HTTP：https://mcp.m5stack.com/mcp
- 兼容 SSE：https://mcp.m5stack.com/sse
通过modelscope连接：https://www.modelscope.cn/mcp/servers/yuyun2000/m5stack-doc-server

## 安装依赖

```bash
python3 -m pip install -r requirements.txt
```

服务固定使用官方 `mcp==2.0.0`，要求 Python 3.10+；由于生产使用的旧版
`volcengine==1.0.123` 依赖较旧，推荐部署在 Python 3.10-3.12。`start.sh` 会创建
`.venv-mcp2` 专用虚拟环境，并使用 `--system-site-packages` 复用服务器
现有的 `volcengine==1.0.123`。`mcp==2.0.0` 安装在虚拟环境中并优先于系统环境的
`mcp==1.27`，不会覆盖或破坏其他服务的 MCP 依赖。首次启动需要系统提供 `python3-venv`。

## 配置说明

### 1. 创建配置文件

首次使用前，需要配置知识库、AI 回答服务和云日志服务：

```bash
# 复制示例配置文件
cp config.example.json config.json
```

### 2. 填写密钥信息

编辑 `config.json` 文件，填入知识库、OpenAI 兼容接口和云日志配置。示例只保留字段名、安全占位符及公共服务地址：

```json
{
  "volcengine": {
    "ak": "your_access_key_here",
    "sk": "your_secret_key_here",
    "knowledge_base_domain": "api-knowledgebase.mlp.cn-beijing.volces.com",
    "request_timeout": 30,
    "connect_timeout": 5,
    "read_timeout": 30,
    "max_retries": 2,
    "pool_connections": 32,
    "pool_maxsize": 64,
    "internal_workers": 64,
    "knowledge_base_name": "m5stack",
    "project": "default",
    "region": "cn-north-1",
    "service": "air"
  },
  "openai_compatible": {
    "base_url": "https://chat.m5stack.com",
    "api_key": "your_openai_compatible_api_key_here",
    "model": "m5stack-fae",
    "chat_completions_path": "/v1/chat/completions",
    "connect_timeout": 10,
    "read_timeout": 240,
    "max_retries": 1,
    "pool_connections": 8,
    "pool_maxsize": 16
  },
  "cloud_logging": {
    "enabled": true,
    "endpoint": "tls-cn-beijing.volces.com",
    "region": "cn-beijing",
    "access_key": "your_tls_access_key_here",
    "secret_key": "your_tls_secret_key_here",
    "topic_id": "your_tls_topic_id_here",
    "source": "m5doc-mcp",
    "message_log_interval_seconds": 60,
    "message_log_max_keys": 20000
  },
  "rate_limit": {
    "enabled": true,
    "requests_per_minute": 120,
    "burst": 30,
    "whitelist_ips": ["47.113.125.164"],
    "max_clients": 10000,
    "client_ttl_seconds": 3600
  },
  "mcp_server": {
    "json_response": true,
    "stateless_http": true,
    "max_request_body_size": 4194304,
    "allowed_hosts": ["mcp.m5stack.com", "mcp.m5stack.com:*", "127.0.0.1:*", "localhost:*", "[::1]:*"],
    "allowed_origins": ["https://mcp.m5stack.com", "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
  }
}
```

**⚠️ 重要提示：**
- `config.json` 文件已被添加到 `.gitignore`，不会上传到 Git 仓库
- 请妥善保管你的 API 密钥，不要泄露给他人
- `config.example.json` 是示例模板，可以安全地提交到代码仓库
- AI 回答服务也可通过环境变量覆盖：`M5DOC_AI_BASE_URL`、`M5DOC_AI_API_KEY`、`M5DOC_AI_MODEL`
- TLS 默认复用 `volcengine.ak/sk`，也可通过部署环境变量覆盖；生产环境推荐使用仅有目标日志主题写权限的专用密钥
- 示例文件只保留云日志字段名和占位符；真实密钥、Topic 及采集参数仅配置在被 Git 忽略的 `config.json` 或部署环境变量中
- `trusted_proxies` 默认仅信任本机 Nginx。只有部署了其他反向代理时，才把其明确的 CIDR 加入列表，避免客户端伪造转发 IP
- `allowed_hosts` 和 `allowed_origins` 是 SDK v2 的传输安全白名单；增加域名或反向代理入口时必须同步更新，禁止在公网关闭校验

### MCP v2 与兼容端点

- `/mcp` 同时支持 MCP 2026-07-28 `server/discover` 和旧版 `initialize`；新协议请求使用官方规定的 `MCP-Protocol-Version`、`MCP-Method` 与请求 `_meta`。
- `/mcp` 默认使用 JSON 响应和无会话旧协议模式，普通工具请求可横向扩展，不依赖 `Mcp-Session-Id`。
- `/sse` 和 `/messages` 保留给旧客户端；SSE 会广播规范的 `/messages/` 地址，服务端也兼容无尾斜杠的 `/messages` 且不会返回 307。SSE 是长连接，多实例部署仍需要粘性路由或共享连接路由能力。
- 工具成功和失败结果都保留文本 `content`，并额外提供 `structuredContent`；旧客户端继续读取文本，新客户端可直接消费结构化字段。
- SDK 在 POST 层限制请求体为 4 MiB，并强制校验 Content-Type、Host 和 Origin。

### 云日志与用量统计

- 所有应用日志、HTTP 请求和 MCP 工具调用都通过有界内存队列异步批量上传到火山引擎 TLS，不再创建 `m5doc_mcp.log`。
- 日志包含请求耗时、HTTP 状态、工具结果、并发峰值、队列丢弃数、IP、User-Agent、MCP 会话/协议字段和加盐指纹。
- `knowledge_search` 的原始查询词、`knowledge_answer` 的原始问题会上传，默认最多保留 20,000 字符；常见 `Authorization: Bearer ...`、`api_key=...` 等凭据模式会先脱敏。知识库返回正文和 AI 回答正文不会上传。
- 原始查询可能含个人信息，生产环境必须限制 TLS Topic 的访问权限并设置合适的日志保留周期；可用 `M5DOC_LOG_INPUT_MAX_CHARS` 调整单条输入上限。
- 日志队列满、TLS 超时或云端故障时，业务请求继续执行；`/health` 的 `cloud_logging` 字段会显示队列、丢弃、上传失败和并发状态。
- 如当地隐私政策不允许保存原始 IP，可设置 `M5DOC_TLS_COLLECT_CLIENT_IP=false`；加盐指纹仍可用于近似去重统计。
- 未配置 OAuth 时，`/.well-known/oauth-protected-resource*` 的预期 404 探测不会上传，避免无效日志占量；其他异常请求仍正常记录。
- legacy SSE 的成功 `POST /messages` 只是 JSON-RPC 上行传输确认，不能直接作为工具用量。服务只提取顶层 `method`，按“客户端指纹 + method”每 60 秒最多上传一条 HTTP 样本；`params` 和请求体不会进入该 HTTP 日志。被抑制总数可在 `/health` 的 `cloud_logging.suppressed_transport_logs` 查看。
- 工具用量以 `mcp_tool_call` 为准，反馈以 `knowledge_feedback` 为准；所有非 2xx `/messages`、未处理异常、限流和工具错误仍逐条记录，不参与采样。
- 可用 `M5DOC_TLS_MESSAGE_LOG_INTERVAL_SECONDS` 调整采样窗口，设为 `0` 可恢复逐条记录；`M5DOC_TLS_MESSAGE_LOG_MAX_KEYS` 控制采样状态的内存上限。

### 按来源 IP 限流

- `/sse`、`/messages`、`/mcp` 按可信代理解析出的来源 IP 限流，每个 IP 使用完全独立的额度；指纹、查询内容、User-Agent 等字段不参与限流。
- 默认每个来源 IP 每分钟补充 120 个请求额度，最多突发 30 个请求。超过额度返回 HTTP `429` 和 `Retry-After`；`/health` 不限流。
- `47.113.125.164` 是服务器调用白名单，始终绕过限流且不消耗任何 IP bucket 的额度。
- 当前 limiter 位于单个 Python 进程内。生产环境保持单 Uvicorn worker；若未来启用多 worker 或多实例，必须先迁移到 Redis 等共享限流存储。
- 可用 `M5DOC_RATE_LIMIT_ENABLED`、`M5DOC_RATE_LIMIT_REQUESTS_PER_MINUTE`、`M5DOC_RATE_LIMIT_BURST`、`M5DOC_RATE_LIMIT_WHITELIST_IPS`、`M5DOC_RATE_LIMIT_MAX_CLIENTS`、`M5DOC_RATE_LIMIT_CLIENT_TTL_SECONDS` 覆盖配置。

## MCP 工具

- `knowledge_search`：快速检索 M5Stack 文档和芯片资料，通常 1 秒左右返回原始参考片段；保持原有名称、必填参数和返回行为，兼容老客户端。
- `knowledge_answer`：将用户问题直接发送到 M5Stack FAE AI，通过 OpenAI 兼容接口返回整理后的专业回复；复杂问题可能耗时 1 分钟以上，客户端需要等待，除非工具返回错误。
- `knowledge_feedback`：Agent 在资料缺失、内容错误、功能未覆盖、示例损坏或工具异常时主动提交反馈；反馈会以 `manual_review=true`、`priority=high` 写入云日志并返回 `feedback_id`，供 M5Stack 人工评估和补充。

## 运行服务

### Ubuntu/Linux 环境（推荐）

使用一键脚本管理服务：

```bash
# 赋予脚本执行权限（首次使用）
chmod +x *.sh

# 启动服务
./start.sh

# 查看状态
./status.sh

# 停止服务
./stop.sh

# 重启服务
./restart.sh

# 查看服务及云日志上传状态
curl http://127.0.0.1:5058/health
```

**脚本说明：**
- `start.sh` - 启动服务（后台运行，自动检查依赖和配置）
- `stop.sh` - 停止服务（优雅关闭，超时后强制终止）
- `restart.sh` - 重启服务
- `status.sh` - 查看服务、进程和云日志队列状态

### 手动运行（Windows/其他环境）

```bash
python server.py
```

服务将在 `http://0.0.0.0:5058` 启动。

协议回归和基础测试：

```bash
python3 -m py_compile server.py rag.py ai_answer.py cloud_logging.py rate_limit.py mcp_config.py
python3 -m unittest discover -s tests -v
```

## 安全说明

本项目使用配置文件管理敏感信息：
- ✅ `config.json` - 包含真实密钥，已加入 `.gitignore`，不会上传
- ✅ `config.example.json` - 配置模板，可以安全提交
- ✅ `.gitignore` - 确保敏感文件不会意外提交到 Git
- 服务不再写仓库本地日志文件；TLS 密钥只放在被忽略的 `config.json` 或部署环境变量中
