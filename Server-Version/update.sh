#!/bin/bash

# update.sh - Limit-Up Sniper 商业版 更新脚本
# 用法: sudo ./update.sh [源码目录]
# 注意: 如果文件已经在 /opt/limit-up-sniper 内，则默认只是重启或拉取(如使用git)
# 但此处设计为从上传的源码覆盖更新

set -e

# 默认安装目录
APP_DIR="/opt/limit-up-sniper"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== Limit-Up Sniper 商业版 更新程序 ===${NC}"

# 1. 检查操作环境
# 判断脚本是否是在已安装目录内运行 (/opt/limit-up-sniper/scripts/update.sh)
if [[ "$SCRIPT_DIR/.." -ef "$APP_DIR" ]]; then
    # 已安装模式
    cd "$APP_DIR"
    if [ -d ".git" ]; then
         echo -e "${YELLOW}检测到 Git 仓库，正在拉取最新代码...${NC}"
         git fetch --all
         git reset --hard origin/main
         git pull
         
         # 已经是最新代码，不需要再复制文件
         # 直接跳到更新依赖和重启服务
         echo -e "${YELLOW}[1/3] 停止服务...${NC}"
         systemctl stop limit-up-sniper || true
         
         echo -e "${YELLOW}[2/3] 更新 Python 依赖...${NC}"
         if [ -f "$APP_DIR/venv/bin/activate" ]; then
            source "$APP_DIR/venv/bin/activate"
            pip install -r "$APP_DIR/backend/requirements.txt" --no-cache-dir -q
         fi
         
         # 重新赋予权限
         chmod +x "$APP_DIR/scripts/"*.sh
         chmod -R 777 "$APP_DIR/backend/data"
         touch "$APP_DIR/backend/app.log"
         chmod 666 "$APP_DIR/backend/app.log"
         
         echo "正在重启服务..."
         systemctl restart limit-up-sniper
         systemctl restart nginx
         
         echo -e "${GREEN}=========================================${NC}"
         echo -e "${GREEN}   ✅ Git 自动更新完成!                   ${NC}"
         echo -e "${GREEN}=========================================${NC}"
         systemctl status limit-up-sniper --no-pager | head -n 5
         exit 0
    fi

    # 非 Git 环境，需要用户提供新源码路径
    SOURCE_ROOT="$1"
    if [ -z "$SOURCE_ROOT" ]; then
        echo -e "${RED}[错误] 当前不是 Git 仓库，请提供新源码的路径。${NC}"
        echo "用法: sudo $0 /root/New-Code-Folder"
        exit 1
    fi
else
    # 源码模式 (直接在新上传的文件夹里运行 Server-Version/update.sh)
    SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"
    echo "检测到源码目录: $SOURCE_ROOT"
fi

if [ ! -d "$SOURCE_ROOT/backend" ]; then
    echo -e "${RED}[错误] 源码目录无效，未找到 backend 文件夹。${NC}"
    exit 1
fi

echo -e "${YELLOW}[1/3] 停止服务...${NC}"
systemctl stop limit-up-sniper || true

echo -e "${YELLOW}[2/3] 更新文件...${NC}"
# 备份配置文件 (如果在 backend/data)
# data 目录通常保留，不覆盖，除非有 schema 变更。但 cp -r 默认覆盖同名文件。
# data 里的 config.json, admin_token.txt 等应该保留。
# 为了安全，我们只复制代码文件，不覆盖 data 目录（除非有新结构需手动处理）

# 复制 backend 代码 (排除 data 文件夹)
# rsync 是更好的选择，但为了兼容用 cp
# 先临时移出 data
if [ -d "$APP_DIR/backend/data" ]; then
    mv "$APP_DIR/backend/data" "$APP_DIR/backend_data_bak"
fi

# 覆盖后端
yes | cp -rf "$SOURCE_ROOT/backend" "$APP_DIR/"

# 还原 data
if [ -d "$APP_DIR/backend_data_bak" ]; then
    rm -rf "$APP_DIR/backend/data" # 删除源码自带的空data或其他
    mv "$APP_DIR/backend_data_bak" "$APP_DIR/backend/data"
else
    # 首次或意外情况，确保目录存在
    mkdir -p "$APP_DIR/backend/data"
fi

# 覆盖前端
yes | cp -rf "$SOURCE_ROOT/frontend" "$APP_DIR/"

# 覆盖脚本
mkdir -p "$APP_DIR/scripts"
yes | cp -rf "$SOURCE_ROOT/Server-Version/"*.sh "$APP_DIR/scripts/"
chmod +x "$APP_DIR/scripts/"*.sh

# 确保权限
chmod -R 777 "$APP_DIR/backend/data"
touch "$APP_DIR/backend/app.log"
chmod 666 "$APP_DIR/backend/app.log"

echo -e "${YELLOW}[3/3] 更新依赖环境...${NC}"
if [ -f "$APP_DIR/venv/bin/activate" ]; then
    source "$APP_DIR/venv/bin/activate"
    pip install -r "$APP_DIR/backend/requirements.txt" --no-cache-dir -q
fi

# 重启服务
echo "正在重启服务..."
systemctl restart limit-up-sniper
systemctl restart nginx

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}   ✅ 更新完成! (Update Complete)        ${NC}"
echo -e "${GREEN}=========================================${NC}"
systemctl status limit-up-sniper --no-pager | head -n 5
