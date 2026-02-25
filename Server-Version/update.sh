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
GIT_REPO_DIR=""

# 场景A: 脚本在已安装目录内运行 (/opt/limit-up-sniper/scripts/update.sh)
if [[ "$SCRIPT_DIR/.." -ef "$APP_DIR" ]]; then
    # 切换到 APP_DIR 看看是不是 git 仓库
    if [ -d "$APP_DIR/.git" ]; then
        GIT_REPO_DIR="$APP_DIR"
    fi
# 场景B: 脚本在源码目录运行 (/root/Limit-Up-Sniper-Commercial/Server-Version/update.sh)
else
    # 检查源码根目录是不是 git 仓库
    SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"
    if [ -d "$SOURCE_ROOT/.git" ]; then
        GIT_REPO_DIR="$SOURCE_ROOT"
    fi
fi

# 如果找到了 Git 仓库，执行自动拉取
if [ ! -z "$GIT_REPO_DIR" ]; then
    echo -e "${YELLOW}检测到 Git 仓库 ($GIT_REPO_DIR)，正在拉取最新代码...${NC}"
    cd "$GIT_REPO_DIR"
    
    # 尝试拉取
    if git pull; then
        echo "Git 拉取成功。"
    else
        echo -e "${RED}[警告] Git 拉取失败 (可能是本地修改冲突)。${NC}"
        read -p "是否强制覆盖本地修改? (y/n) " FORCE_RESET
        if [[ "$FORCE_RESET" == "y" ]]; then
            git fetch --all
            git reset --hard origin/main
            git pull
        else
            echo "已取消自动更新，将使用当前文件进行部署。"
        fi
    fi
    
    # 无论是在 /opt 还是在 /rootPull 完之后，我们都需要确保 SOURCE_ROOT 正确
    # 如果是在 /opt 运行且它是git仓库，那么 SOURCE_ROOT 就是 APP_DIR (自更新)
    # 如果是在 /root 运行，SOURCE_ROOT 已经是 /root/Limit-Up-Sniper-Commercial
    
    if [[ "$GIT_REPO_DIR" -ef "$APP_DIR" ]]; then
         # 已经是安装目录自更新，不需要 copy，直接跳过 copy 步骤
         SOURCE_ROOT="$APP_DIR"
         # 设置一个标志，跳过后续的 cp 操作
         SKIP_COPY=true
    else
         # 源码目录更新，继续走下面的 copy 流程
         SOURCE_ROOT="$GIT_REPO_DIR"
         SKIP_COPY=false
    fi
else
    # 非 Git 环境，需要用户提供新源码路径 (或者是当前源码目录)
    if [[ ! "$SCRIPT_DIR/.." -ef "$APP_DIR" ]]; then
        SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"
        echo "检测到本地源码目录: $SOURCE_ROOT (非 Git 仓库)"
    else
        # 在 /opt 运行但没 git，也没给参数
        SOURCE_ROOT="$1"
        if [ -z "$SOURCE_ROOT" ]; then
            echo -e "${RED}[错误] 请提供新源码的路径。${NC}"
            exit 1
        fi
    fi
fi

if [ ! -d "$SOURCE_ROOT/backend" ]; then
    echo -e "${RED}[错误] 源码目录无效，未找到 backend 文件夹。${NC}"
    exit 1
fi

echo -e "${YELLOW}[1/3] 停止服务...${NC}"
systemctl stop limit-up-sniper || true

if [ "$SKIP_COPY" = true ]; then
    echo "跳过文件复制 (原地更新)..."
else
    echo -e "${YELLOW}[2/3] 更新文件...${NC}"
    # 备份配置文件 (如果在 backend/data)
    if [ -d "$APP_DIR/backend/data" ]; then
        # 仅备份，cp命令下方会自动处理
        # 但为了安全，我们把现有的 data 移开，防止被错误的源码覆盖（如果源码里 data 是空的）
        mv "$APP_DIR/backend/data" "$APP_DIR/backend_data_tempmove"
    fi

    # 覆盖后端
    # 注意：如果 SOURCE_ROOT/backend/data 存在且为空，cp -r 会创建空目录
    yes | cp -rf "$SOURCE_ROOT/backend" "$APP_DIR/"

    # 还原 data
    if [ -d "$APP_DIR/backend_data_tempmove" ]; then
        # 移除可能被源码覆盖生成的 data 目录
        rm -rf "$APP_DIR/backend/data" 
        mv "$APP_DIR/backend_data_tempmove" "$APP_DIR/backend/data"
    else
        # 首次或意外情况
        mkdir -p "$APP_DIR/backend/data"
    fi

    # 覆盖前端
    yes | cp -rf "$SOURCE_ROOT/frontend" "$APP_DIR/"
    
    # 覆盖脚本
    mkdir -p "$APP_DIR/scripts"
    yes | cp -rf "$SOURCE_ROOT/Server-Version/"*.sh "$APP_DIR/scripts/"
    chmod +x "$APP_DIR/scripts/"*.sh
fi

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
