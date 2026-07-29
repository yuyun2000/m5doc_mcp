#!/bin/bash

# M5Doc MCP 服务启动脚本

# 脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 配置
SERVICE_NAME="m5doc_mcp"
PID_FILE="$SCRIPT_DIR/${SERVICE_NAME}.pid"
PORT=5058

# 颜色输出
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "  M5Doc MCP 服务启动"
echo "=========================================="

# 检查配置文件
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    echo -e "${RED}错误: 配置文件 config.json 不存在${NC}"
    echo -e "${YELLOW}请复制 config.example.json 为 config.json 并填入正确的密钥信息${NC}"
    echo ""
    echo "执行命令: cp config.example.json config.json"
    echo "然后编辑 config.json 填入你的 API 密钥"
    exit 1
fi

# 检查是否已经在运行
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo -e "${YELLOW}服务已经在运行中 (PID: $OLD_PID)${NC}"
        echo "如需重启，请先执行: ./stop.sh"
        exit 1
    else
        echo -e "${YELLOW}清理过期的 PID 文件${NC}"
        rm -f "$PID_FILE"
    fi
fi

# 检查端口是否被占用
if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo -e "${RED}错误: 端口 $PORT 已被占用${NC}"
    echo "占用端口的进程:"
    lsof -i :$PORT
    exit 1
fi

# 检查 Python 环境
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}错误: 未找到 Python3${NC}"
    exit 1
fi

echo "检查 Python 依赖包..."
VENV_DIR="$SCRIPT_DIR/.venv-mcp2"
PYTHON_BIN="$VENV_DIR/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "创建 MCP 2.0 专用虚拟环境..."
    # 复用服务器现有的 volcengine==1.0.123，避免重新构建其旧版依赖；
    # 虚拟环境中安装的 mcp==2.0.0 会优先于系统环境的 mcp==1.27。
    if ! python3 -m venv --system-site-packages "$VENV_DIR"; then
        echo -e "${RED}虚拟环境创建失败，请先安装 python3-venv${NC}"
        exit 1
    fi
fi

if ! "$PYTHON_BIN" -c "import importlib.metadata, mcp, starlette, uvicorn, volcengine, requests; raise SystemExit(0 if importlib.metadata.version('mcp') == '2.0.0' and importlib.metadata.version('volcengine') == '1.0.123' else 1)" 2>/dev/null; then
    echo "安装或更新项目依赖..."
    if ! "$PYTHON_BIN" -m pip install -r requirements.txt; then
        echo -e "${RED}依赖包安装失败${NC}"
        exit 1
    fi
fi

# 启动服务
echo "启动服务..."

# 运行日志由应用异步上传到火山引擎 TLS，不再创建仓库本地日志文件。
nohup "$PYTHON_BIN" server.py > /dev/null 2>&1 &
PID=$!

# 保存 PID
echo $PID > "$PID_FILE"

# 等待服务启动
sleep 2

# 检查服务是否成功启动
if ps -p $PID > /dev/null 2>&1; then
    echo -e "${GREEN}[OK] 服务启动成功!${NC}"
    echo ""
    echo "服务信息:"
    echo "  PID:  $PID"
    echo "  端口: $PORT"
    echo ""
    echo "云日志状态: curl http://127.0.0.1:$PORT/health"
    echo "停止服务: ./stop.sh"
    echo "查看状态: ./status.sh"
else
    echo -e "${RED}[FAIL] 服务启动失败${NC}"
    echo "请前台运行 .venv-mcp2/bin/python server.py 检查启动错误"
    rm -f "$PID_FILE"
    exit 1
fi
