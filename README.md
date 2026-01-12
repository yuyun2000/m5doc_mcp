# m5doc_mcp
m5官方文档的mcp服务器

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

```bash
python server.py
```

## 安全说明

本项目使用配置文件管理敏感信息：
- ✅ `config.json` - 包含真实密钥，已加入 `.gitignore`，不会上传
- ✅ `config.example.json` - 配置模板，可以安全提交
- ✅ `.gitignore` - 确保敏感文件不会意外提交到 Git
