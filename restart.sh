#!/bin/bash

# M5Doc MCP 服务重启脚本

# 脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "  M5Doc MCP 服务重启"
echo "=========================================="

# 停止服务
echo -e "${YELLOW}步骤 1/2: 停止服务${NC}"
./stop.sh

echo ""

# 等待一下
sleep 1

# 启动服务
echo -e "${YELLOW}步骤 2/2: 启动服务${NC}"
./start.sh

if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}✓ 服务重启完成${NC}"
else
    exit 1
fi
