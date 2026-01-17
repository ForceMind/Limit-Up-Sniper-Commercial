#!/bin/bash

# update.sh - 自动更新代码并重启服务
# 用法: sudo ./update.sh

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== Limit-Up Sniper 更新脚本 ===${NC}"

# 1. 检查 Root 权限
if [ "$EUID" -ne 0 ]; then 
  echo -e "${YELLOW}提示: 建议使用 sudo 运行以确保服务重启成功${NC}"
fi

# 1.1 系统检测 (新增)
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$NAME
    echo -e "Detected OS: ${GREEN}$OS${NC}"
fi

# 2. 拉取最新代码
echo -e "${YELLOW}[1/3] 准备更新代码...${NC}"

# 2.1 备份本地数据
BACKUP_DIR="._data_backup"
# Data is in root, we are in Server-Version
DATA_DIR="../backend/data"

if [ -d "$DATA_DIR" ]; then
    echo -e "${YELLOW}正在备份 data 目录...${NC}"
    rm -rf "$BACKUP_DIR"
    cp -r "$DATA_DIR" "$BACKUP_DIR"
fi

git config --global --add safe.directory "*"

# 2.2 尝试拉取
pushd .. > /dev/null
if ! git pull; then
    popd > /dev/null
    echo -e "${RED}------------------------------------------------${NC}"
    echo -e "${RED}❌ Git pull 失败！请查看上方的错误信息。${NC}"
    echo -e "${RED}------------------------------------------------${NC}"
    
    # 交互询问
    echo -e "${YELLOW}常见原因说明：${NC}"
    echo -e "1. ${YELLOW}网络超时/GnuTLS error${NC} -> 请选择 ${GREEN}n (取消)${NC}，稍后重试。"
    echo -e "2. ${YELLOW}本地文件冲突${NC} -> 如确需覆盖本地修改，可选择 ${RED}y (强制重置)${NC}。"
    echo -e "   ${GREEN}(提示: 我们在此前已将数据备份至子目录，强制更新后会自动恢复你的配置文件)${NC}"
    echo ""
    
    read -p "⚠️  是否丢弃本地修改并强制更新? (y/n): " CONFIRM
    
    if [ "$CONFIRM" == "y" ]; then
        echo -e "${YELLOW}正在执行强制重置 (git reset --hard)...${NC}"
        pushd .. > /dev/null
        CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
        git fetch --all
        git reset --hard "origin/$CURRENT_BRANCH"
        popd > /dev/null
    else
        echo -e "${RED}更新已取消。正在恢复数据...${NC}"
        if [ -d "$BACKUP_DIR" ]; then
            rm -rf "$DATA_DIR"
            cp -r "$BACKUP_DIR" "$DATA_DIR"
            rm -rf "$BACKUP_DIR"
            echo -e "${GREEN}数据已恢复，服务保持原样。${NC}"
        fi
        exit 1
    fi
else
    popd > /dev/null
    echo -e "${GREEN}✅ Git pull 成功。${NC}"
fi

# 2.3 正常流程的数据恢复
if [ -d "$BACKUP_DIR" ]; then
    echo -e "${YELLOW}正在恢复数据...${NC}"
    mkdir -p "$DATA_DIR"
    
    PROTECTED_FILES=("config.json" "lhb_config.json" "watchlist.json" "seat_mappings.json" "vip_seats.json")
    
    for file in "${PROTECTED_FILES[@]}"; do
        if [ -f "$BACKUP_DIR/$file" ]; then
            cp "$BACKUP_DIR/$file" "$DATA_DIR/$file"
        fi
    done

    cp -rn "$BACKUP_DIR"/* "$DATA_DIR/" 2>/dev/null || true
    
    rm -rf "$BACKUP_DIR"
    echo -e "${GREEN}✅ 数据恢复完成。${NC}"
fi

# 3. 更新依赖
echo -e "${YELLOW}[2/3] 更新 Python 依赖...${NC}"
if [ -d "venv" ]; then
    source venv/bin/activate
    pip install -r ../backend/requirements.txt -q --timeout 100
else
    echo "未找到虚拟环境，跳过依赖更新。"
fi

# 3.1 检查并修复 Service 文件路径
SERVICE_FILE="/etc/systemd/system/limit-up-sniper.service"
if [ -f "$SERVICE_FILE" ]; then
    if grep -q "uvicorn main:app" "$SERVICE_FILE"; then
        echo -e "${YELLOW}[Fix] 检测到旧版服务配置，正在更新为 app.main:app...${NC}"
        sed -i 's/uvicorn main:app/uvicorn app.main:app/g' "$SERVICE_FILE"
        systemctl daemon-reload
    fi
fi

# 4. 重启服务
echo -e "${YELLOW}[3/3] 重启服务...${NC}"
if systemctl is-active --quiet limit-up-sniper; then
    sudo systemctl restart limit-up-sniper
    echo -e "${GREEN}服务已重启!${NC}"
else
    echo -e "${YELLOW}服务未运行，尝试启动...${NC}"
    sudo systemctl start limit-up-sniper
fi

# 5. 检查状态
echo -e "${GREEN}更新完成! 当前状态:${NC}"
sudo systemctl status limit-up-sniper --no-pager | head -n 10