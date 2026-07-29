#!/bin/bash

# M5Doc MCP 服务状态检查脚本

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
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo "=========================================="
echo "  M5Doc MCP 服务状态"
echo "=========================================="

# 检查 PID 文件
if [ ! -f "$PID_FILE" ]; then
    echo -e "服务状态: ${RED}未运行${NC} (PID 文件不存在)"
    exit 0
fi

# 读取 PID
PID=$(cat "$PID_FILE")

# 检查进程是否存在
if ! ps -p "$PID" > /dev/null 2>&1; then
    echo -e "服务状态: ${RED}未运行${NC} (进程已退出)"
    echo -e "${YELLOW}提示: 发现残留的 PID 文件，建议执行 ./stop.sh 清理${NC}"
    exit 0
fi

# 服务正在运行
echo -e "服务状态: ${GREEN}运行中${NC}"
echo ""
echo "进程信息:"
echo "  PID:     $PID"
echo "  端口:    $PORT"
echo ""

# 显示进程详细信息
echo "进程详情:"
ps -p "$PID" -o pid,ppid,user,%cpu,%mem,vsz,rss,etime,cmd --no-headers | awk '{
    printf "  CPU:     %s%%\n", $4
    printf "  内存:    %s%% (RSS: %s KB)\n", $5, $7
    printf "  运行时长: %s\n", $8
}'

echo ""

# 检查端口
if command -v lsof &> /dev/null; then
    if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo -e "端口状态: ${GREEN}正在监听 $PORT${NC}"
    else
        echo -e "端口状态: ${YELLOW}端口 $PORT 未监听${NC}"
    fi
fi

# 显示云日志队列和上传状态，不读取本地日志文件。
if command -v curl &> /dev/null; then
    echo ""
    echo "服务健康与云日志状态:"
    curl --silent --show-error --max-time 3 "http://127.0.0.1:$PORT/health" || true
    echo ""
fi

echo ""
echo "管理命令:"
echo "  启动: ./start.sh"
echo "  停止: ./stop.sh"
echo "  重启: ./restart.sh"
