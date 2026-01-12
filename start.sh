#!/bin/bash

# M5Doc MCP 服务启动脚本

# 脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 配置
SERVICE_NAME="m5doc_mcp"
PID_FILE="$SCRIPT_DIR/${SERVICE_NAME}.pid"
LOG_FILE="$SCRIPT_DIR/${SERVICE_NAME}.log"
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

# 检查依赖包
echo "检查 Python 依赖包..."
MISSING_DEPS=()
for pkg in mcp fastapi uvicorn volcengine requests; do
    if ! python3 -c "import $pkg" 2>/dev/null; then
        MISSING_DEPS+=("$pkg")
    fi
done

if [ ${#MISSING_DEPS[@]} -ne 0 ]; then
    echo -e "${YELLOW}警告: 缺少以下依赖包: ${MISSING_DEPS[*]}${NC}"
    echo "正在安装依赖包..."
    pip3 install mcp fastapi uvicorn volcengine requests
    if [ $? -ne 0 ]; then
        echo -e "${RED}依赖包安装失败${NC}"
        exit 1
    fi
fi

# 启动服务
echo "启动服务..."
echo "日志文件: $LOG_FILE"
echo ""

nohup python3 server.py > "$LOG_FILE" 2>&1 &
PID=$!

# 保存 PID
echo $PID > "$PID_FILE"

# 等待服务启动
sleep 2

# 检查服务是否成功启动
if ps -p $PID > /dev/null 2>&1; then
    echo -e "${GREEN}✓ 服务启动成功!${NC}"
    echo ""
    echo "服务信息:"
    echo "  PID:  $PID"
    echo "  端口: $PORT"
    echo "  日志: $LOG_FILE"
    echo ""
    echo "查看日志: tail -f $LOG_FILE"
    echo "停止服务: ./stop.sh"
    echo "查看状态: ./status.sh"
else
    echo -e "${RED}✗ 服务启动失败${NC}"
    echo "请查看日志文件: $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
