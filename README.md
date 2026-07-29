# m5doc_mcp
m5官方文档的mcp服务器
地址：https://mcp.m5stack.com/sse
通过modelscope连接：https://www.modelscope.cn/mcp/servers/yuyun2000/m5stack-doc-server

## 安装依赖

```bash
pip install mcp fastapi starlette uvicorn volcengine requests
```

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
    "source": "m5doc-mcp"
  },
  "rate_limit": {
    "enabled": true,
    "requests_per_minute": 120,
    "burst": 30,
    "whitelist_ips": ["47.113.125.164"],
    "max_clients": 10000,
    "client_ttl_seconds": 3600
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

### 云日志与用量统计

- 所有应用日志、HTTP 请求和 MCP 工具调用都通过有界内存队列异步批量上传到火山引擎 TLS，不再创建 `m5doc_mcp.log`。
- 日志包含请求耗时、HTTP 状态、工具结果、并发峰值、队列丢弃数、IP、User-Agent、MCP 会话/协议字段和加盐指纹。
- `knowledge_search` 的原始查询词、`knowledge_answer` 的原始问题会上传，默认最多保留 20,000 字符；常见 `Authorization: Bearer ...`、`api_key=...` 等凭据模式会先脱敏。知识库返回正文和 AI 回答正文不会上传。
- 原始查询可能含个人信息，生产环境必须限制 TLS Topic 的访问权限并设置合适的日志保留周期；可用 `M5DOC_LOG_INPUT_MAX_CHARS` 调整单条输入上限。
- 日志队列满、TLS 超时或云端故障时，业务请求继续执行；`/health` 的 `cloud_logging` 字段会显示队列、丢弃、上传失败和并发状态。
- 如当地隐私政策不允许保存原始 IP，可设置 `M5DOC_TLS_COLLECT_CLIENT_IP=false`；加盐指纹仍可用于近似去重统计。

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

## 安全说明

本项目使用配置文件管理敏感信息：
- ✅ `config.json` - 包含真实密钥，已加入 `.gitignore`，不会上传
- ✅ `config.example.json` - 配置模板，可以安全提交
- ✅ `.gitignore` - 确保敏感文件不会意外提交到 Git
- 服务不再写仓库本地日志文件；TLS 密钥只放在被忽略的 `config.json` 或部署环境变量中
