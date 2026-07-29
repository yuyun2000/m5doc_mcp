# AGENTS.md

## 角色定位

你为 M5Stack 的 AI 工程师服务。处理本项目任务时，默认使用中文沟通，优先保证 M5Stack 文档 MCP 服务的稳定性、可维护性、响应可控性与密钥安全。

## 项目概览

这是一个 M5Stack 官方文档 MCP 服务端项目。服务主要向 MCP 客户端暴露三类知识工具：

- `knowledge_search`：原有快速检索工具，通过火山引擎知识库检索 M5Stack 产品、编程、芯片资料，并返回参考文本片段。必须保持名称、必填参数和向后兼容行为，兼容老客户端调用。
- `knowledge_answer`：新增专业回答工具，通过 OpenAI 兼容接口调用 M5Stack FAE AI，将用户问题整理成可直接使用的专业回复。该工具可能耗时 1 分钟甚至更久，客户端应等待结果；如果需要低延迟原文片段，使用 `knowledge_search`。
- `knowledge_feedback`：反馈工具，供 Agent 主动提交知识缺失、内容错误、功能未覆盖、示例损坏或工具 bug；反馈必须重点上传云日志并进入人工评估流程。

主要运行方式：
- 默认监听：`0.0.0.0:5058`
- SSE 端点：`/sse`
- SSE 消息端点：`/messages`
- Streamable HTTP MCP 端点：`/mcp`
- 健康检查：`/health`

## 关键文件

- `server.py`：MCP Server、工具 schema、工具调用分发、Starlette 路由与 Uvicorn 启动入口。
- `rag.py`：火山引擎知识库请求签名、检索、过滤与结果拼装逻辑，用于 `knowledge_search`。
- `ai_answer.py`：OpenAI 兼容接口客户端、AI 回答服务配置读取、超时/重试/连接池与返回解析逻辑，用于 `knowledge_answer`。
- `cloud_logging.py`：火山引擎 TLS 异步批量上传、HTTP/MCP 遥测、可信代理 IP 解析、加盐指纹与日志脱敏。
- `rate_limit.py`：按可信代理解析出的来源 IP 独立限流，并为服务器来源 IP 提供白名单绕过。
- `mcp_config.py`：MCP v2 传输模式、请求体上限和 Host/Origin 安全白名单配置。
- `requirements.txt`：固定官方 MCP SDK 与生产 Volcengine SDK 版本。
- `config.example.json`：可提交的配置模板；新增配置项时需要同步更新。
- `config.json`：本地真实密钥配置，已被 `.gitignore` 忽略，禁止提交或泄露。
- `start.sh` / `stop.sh` / `restart.sh` / `status.sh`：Linux 服务管理脚本。
- `nginx_config.conf`：反向代理参考配置；长耗时工具需要代理层保留足够的 `proxy_read_timeout` / `proxy_send_timeout`。

## 配置项

`config.json` 包含五个主要配置段：

- `volcengine`：火山引擎知识库凭据和检索连接参数，包括 `ak`、`sk`、`knowledge_base_domain`、`request_timeout`、`connect_timeout`、`read_timeout`、`max_retries`、连接池和知识库名称等。
- `openai_compatible`：AI 回答服务配置，包括 `base_url`、`api_key`、`model`、`chat_completions_path`、`connect_timeout`、`read_timeout`、`max_retries` 和连接池参数。
- `cloud_logging`：TLS 日志服务配置；示例文件可保留字段名和安全占位符，真实配置仅允许保存在被忽略的 `config.json` 或部署环境变量中。专用密钥优先，缺省时复用 `volcengine.ak/sk`，日志主题和密钥真实值不得写入 tracked 文件。
- `rate_limit`：来源 IP 限流配置，包括每分钟请求数、突发额度、白名单、最大跟踪 IP 数和闲置 bucket TTL。`47.113.125.164` 必须保留在白名单中。
- `mcp_server`：MCP v2 的 JSON 响应、无会话 HTTP、请求体上限及 Host/Origin 白名单。公网不得关闭传输安全校验。

AI 回答服务可用环境变量覆盖配置：
- `M5DOC_AI_BASE_URL`
- `M5DOC_AI_API_KEY`
- `M5DOC_AI_MODEL`
- `M5DOC_AI_CHAT_COMPLETIONS_PATH`
- `M5DOC_AI_CONNECT_TIMEOUT`
- `M5DOC_AI_READ_TIMEOUT`
- `M5DOC_AI_MAX_RETRIES`
- `M5DOC_AI_POOL_CONNECTIONS`
- `M5DOC_AI_POOL_MAXSIZE`

TLS 云日志配置可使用环境变量覆盖：
- `M5DOC_TLS_ENABLED`
- `M5DOC_TLS_ACCESS_KEY`
- `M5DOC_TLS_SECRET_KEY`
- `M5DOC_TLS_ENDPOINT`
- `M5DOC_TLS_REGION`
- `M5DOC_TLS_TOPIC_ID`
- `M5DOC_TLS_TRUSTED_PROXIES`
- `M5DOC_TLS_FINGERPRINT_SALT`
- `M5DOC_TLS_COLLECT_CLIENT_IP`
- `M5DOC_LOG_INPUT_MAX_CHARS`

来源 IP 限流可使用环境变量覆盖：
- `M5DOC_RATE_LIMIT_ENABLED`
- `M5DOC_RATE_LIMIT_REQUESTS_PER_MINUTE`
- `M5DOC_RATE_LIMIT_BURST`
- `M5DOC_RATE_LIMIT_WHITELIST_IPS`
- `M5DOC_RATE_LIMIT_MAX_CLIENTS`
- `M5DOC_RATE_LIMIT_CLIENT_TTL_SECONDS`

工具层并发和超时可用环境变量控制：
- `M5DOC_MCP_TOOL_WORKERS`
- `M5DOC_MCP_TOOL_TIMEOUT`
- `M5DOC_MCP_TOOL_QUEUE_TIMEOUT`
- `M5DOC_AI_TOOL_WORKERS`
- `M5DOC_AI_TOOL_TIMEOUT`
- `M5DOC_AI_TOOL_QUEUE_TIMEOUT`

MCP v2 传输配置可用环境变量控制：
- `M5DOC_MCP_JSON_RESPONSE`
- `M5DOC_MCP_STATELESS_HTTP`
- `M5DOC_MCP_MAX_REQUEST_BODY_SIZE`
- `M5DOC_MCP_ALLOWED_HOSTS`
- `M5DOC_MCP_ALLOWED_ORIGINS`

## 开发与运行

首次运行前：

```bash
cp config.example.json config.json
```

然后在 `config.json` 中填入本地火山引擎凭据和 OpenAI 兼容接口凭据。

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

本地手动运行：

```bash
python server.py
```

Linux 后台运行：

```bash
chmod +x *.sh
./start.sh
./status.sh
```

`start.sh` 使用 `.venv-mcp2` 专用虚拟环境运行服务。该环境通过
`--system-site-packages` 复用服务器现有的 `volcengine==1.0.123`，并在环境内安装
`mcp==2.0.0`，禁止直接升级系统 Python 中供其他服务使用的 `mcp==1.27`。

健康检查：

```bash
curl http://127.0.0.1:5058/health
```

基础语法校验：

```bash
python -m py_compile server.py rag.py ai_answer.py cloud_logging.py rate_limit.py
```

## 编码规范

- 保持 Python 3.10+ 兼容性；生产建议 Python 3.10-3.12，避免 `volcengine==1.0.123` 的旧依赖在 Python 3.13+ 缺少 wheel。
- MCP 协议层固定使用官方 `mcp==2.0.0` 的 `MCPServer`；`/mcp` 同时兼容现代 `server/discover` 和旧 `initialize`，`/sse`、`/messages` 保持兼容。
- 保持源码、文档与日志文本为 UTF-8；不要引入乱码内容。
- 修改 MCP 工具时，必须保持 `knowledge_search` 的名称、必填参数和向后兼容行为，除非明确要求破坏性变更。
- 新增正式 MCP 工具时优先使用 `knowledge_*` 命名，使工具族语义一致；不要为未上线的新工具保留多余旧别名。
- 修改 `filter_type`、检索逻辑或配置项时，同步更新 `server.py` 的工具 schema、`rag.py` 的映射逻辑、`config.example.json` 与 README。
- 修改 `knowledge_answer`、OpenAI 兼容接口或 AI 配置项时，同步更新 `server.py` 的工具 schema、`ai_answer.py`、`config.example.json` 与 README。
- 对外部 HTTP 请求必须保留超时、重试和连接池控制，避免无超时请求阻塞 MCP 工具调用。
- 长耗时 AI 回答应使用独立线程池、独立超时和可配置并发，避免影响 `knowledge_search` 的低延迟调用。
- 日志中禁止输出 `ak`、`sk`、`api_key`、完整签名头、Authorization 头或其他敏感凭据。
- `knowledge_search` 原始查询词和 `knowledge_answer` 原始问题按任务要求上传 TLS，默认最多 20,000 字符；上传前对常见 Authorization、Bearer token、API key 等凭据模式脱敏。不得记录知识库返回正文或 AI 回答正文。
- 并发相关参数优先通过环境变量或配置项控制，避免硬编码过小或过大的线程数。
- 云日志只能通过有界队列异步上传；上传失败、超时或队列满不得阻塞 MCP 请求，不得回退为仓库本地日志文件。
- 生产服务器使用 `volcengine==1.0.123`；`PutLogsV2Logs` 仅兼容 `source` 和 `filename` 参数，不得传入新版 SDK 才支持的 `log_tags` 或 `time_ns`。
- 用量日志可记录可信代理解析后的 IP、User-Agent、会话/协议元数据和加盐指纹；指纹只用于统计，不得作为限流键。限流必须仅按解析后的来源 IP，每个 IP 额度互相独立，`47.113.125.164` 始终绕过限流。
- 仅信任 `trusted_proxies` 中代理提供的 `X-Forwarded-For` / `X-Real-IP`，避免直接客户端伪造来源 IP。
- 当前限流器是单进程内存状态；启用多个 Uvicorn worker 或多实例前必须改用 Redis 等共享状态，避免每个进程各自计算额度。

## 安全要求

- 永远不要提交 `config.json`、`.env`、日志、PID 文件或任何真实凭据。
- 除上述明确要求的 TLS 原始查询日志外，不要把用户问题写入其他长期缓存；知识库返回内容、AI 回答内容和密钥不得持久化。
- 处理异常时返回可诊断但不泄露内部凭据的信息。
- 若需要演示配置，只能使用 `your_access_key_here`、`your_secret_key_here`、`your_openai_compatible_api_key_here` 等占位符。
- 提交或展示 diff 前，确认真实 `api_key`、`ak`、`sk` 没有进入 tracked 文件。

## 验证清单

完成代码修改后，至少执行：

```bash
python -m py_compile server.py rag.py ai_answer.py cloud_logging.py rate_limit.py mcp_config.py
python -m unittest discover -s tests -v
```

如果修改了服务启动、路由或 MCP 工具行为，还应验证：

```bash
python server.py
curl http://127.0.0.1:5058/health
```

如果修改了 MCP 工具列表或 schema，还应通过导入 `server.py` 检查工具列表，确认依次包含 `knowledge_search`、`knowledge_answer`、`knowledge_feedback`。

如果修改了 Linux 脚本，还应在 Linux 或兼容 shell 环境中验证：

```bash
./start.sh
./status.sh
./stop.sh
```

需要真实知识库检索或 AI 回答测试时，只在本地已配置 `config.json` 的环境中测试，不要把测试密钥、用户问题或返回的敏感内容写入仓库。
