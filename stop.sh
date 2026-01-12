#!/bin/bash

# M5Doc MCP 服务停止脚本

# 脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 配置
SERVICE_NAME="m5doc_mcp"
PID_FILE="$SCRIPT_DIR/${SERVICE_NAME}.pid"

# 颜色输出
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "  M5Doc MCP 服务停止"
echo "=========================================="

# 检查 PID 文件是否存在
if [ ! -f "$PID_FILE" ]; then
    echo -e "${YELLOW}服务未运行 (PID 文件不存在)${NC}"
    exit 0
fi

# 读取 PID
PID=$(cat "$PID_FILE")

# 检查进程是否存在
if ! ps -p "$PID" > /dev/null 2>&1; then
    echo -e "${YELLOW}服务未运行 (进程不存在)${NC}"
    rm -f "$PID_FILE"
    exit 0
fi

# 停止服务
echo "正在停止服务 (PID: $PID)..."

# 尝试优雅关闭
kill "$PID" 2>/dev/null

# 等待进程结束
WAIT_TIME=0
MAX_WAIT=10

while ps -p "$PID" > /dev/null 2>&1; do
    sleep 1
    WAIT_TIME=$((WAIT_TIME + 1))
    
    if [ $WAIT_TIME -ge $MAX_WAIT ]; then
        echo -e "${YELLOW}优雅关闭超时，强制终止进程...${NC}"
        kill -9 "$PID" 2>/dev/null
        sleep 1
        break
    fi
done

# 检查是否成功停止
if ps -p "$PID" > /dev/null 2>&1; then
    echo -e "${RED}✗ 服务停止失败${NC}"
    echo "请手动执行: kill -9 $PID"
    exit 1
else
    echo -e "${GREEN}✓ 服务已停止${NC}"
    rm -f "$PID_FILE"
fi
