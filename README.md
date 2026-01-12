# m5doc_mcp
m5官方文档的mcp服务器
地址：https://mcp.m5stack.com/sse
通过modelscope连接：https://www.modelscope.cn/mcp/servers/yuyun2000/m5stack-doc-server

## 安装依赖

```bash
pip install mcp fastapi uvicorn volcengine requests
```

## 配置说明

### 1. 创建配置文件

首次使用前，需要配置火山引擎 API 密钥：

```bash
# 复制示例配置文件
cp config.example.json config.json
```

### 2. 填写密钥信息

编辑 `config.json` 文件，填入你的火山引擎 API 密钥：

```json
{
  "volcengine": {
    "ak": "your_access_key_here",
    "sk": "your_secret_key_here",
    "knowledge_base_domain": "api-knowledgebase.mlp.cn-beijing.volces.com",
    "request_timeout": 30,
    "knowledge_base_name": "m5stack",
    "project": "default",
    "region": "cn-north-1",
    "service": "air"
  }
}
```

**⚠️ 重要提示：**
- `config.json` 文件已被添加到 `.gitignore`，不会上传到 Git 仓库
- 请妥善保管你的 API 密钥，不要泄露给他人
- `config.example.json` 是示例模板，可以安全地提交到代码仓库

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

# 查看实时日志
tail -f m5doc_mcp.log
```

**脚本说明：**
- `start.sh` - 启动服务（后台运行，自动检查依赖和配置）
- `stop.sh` - 停止服务（优雅关闭，超时后强制终止）
- `restart.sh` - 重启服务
- `status.sh` - 查看服务状态、进程信息和最近日志

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
- ✅ `*.pid` / `*.log` - 运行时文件，不会上传到 Git
