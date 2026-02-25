#!/bin/bash

# Limit-Up Sniper 商业版部署脚本
# 支持系统: Ubuntu/Debian, CentOS/RHEL/Alibaba Cloud Linux/TencentOS
# 用法: sudo ./install.sh

set -e

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}   Limit-Up Sniper 商业版一键部署        ${NC}"
echo -e "${GREEN}=========================================${NC}"

# 1. 检查 Root 权限
if [ "$EUID" -ne 0 ]; then 
  echo -e "${RED}[错误] 请使用 sudo 或 root 权限运行此脚本 (sudo ./install.sh)${NC}"
  exit 1
fi

# 2. 检测操作系统
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$NAME
    VERSION_ID=$VERSION_ID
fi

echo -e "${YELLOW}[1/5] 正在为 $OS 安装系统依赖...${NC}"

APP_DIR="/opt/limit-up-sniper"
# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
# 假设脚本位于 Server-Version/ 目录下，向上寻找项目根目录
SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"

echo "检测到源码根目录: $SOURCE_ROOT"

# 安装依赖函数
install_deps() {
    if [[ "$OS" == *"Ubuntu"* ]] || [[ "$OS" == *"Debian"* ]]; then
        apt-get update -qq
        apt-get install -y python3 python3-pip python3-venv python3-dev build-essential git nginx curl bc
    elif [[ "$OS" == *"CentOS"* ]] || [[ "$OS" == *"Red Hat"* ]] || [[ "$OS" == *"Alibaba"* ]] || [[ "$OS" == *"Tencent"* ]] || [[ "$OS" == *"Fedora"* ]]; then
        # CentOS/RHEL 系
        if command -v dnf > /dev/null; then
            PKG_MGR="dnf"
        else
            PKG_MGR="yum"
        fi
        
        $PKG_MGR install -y epel-release || true
        $PKG_MGR install -y python3 python3-pip python3-devel gcc git nginx curl bc
    else
        echo -e "${RED}[错误] 不支持的操作系统: $OS${NC}"
        echo "尝试通用安装..."
        if command -v apt > /dev/null; then apt update && apt install -y python3-pip python3-venv git nginx; fi
        if command -v yum > /dev/null; then yum install -y python3-pip python3-devel git nginx; fi
    fi
}

install_deps

# 3. 设置应用目录
echo -e "${YELLOW}[2/5] 正在配置应用目录 $APP_DIR...${NC}"

# 停止可能存在的旧服务
systemctl stop limit-up-sniper || true

# 验证源码结构
if [ ! -d "$SOURCE_ROOT/backend" ] || [ ! -d "$SOURCE_ROOT/frontend" ]; then
    echo -e "${RED}[错误] 在 $SOURCE_ROOT 下未找到 backend 或 frontend 目录${NC}"
    echo "请确保您在项目根目录下运行此脚本。"
    exit 1
fi

mkdir -p "$APP_DIR"
# 复制文件
echo "正在复制文件..."
cp -r "$SOURCE_ROOT/backend" "$APP_DIR/"
cp -r "$SOURCE_ROOT/frontend" "$APP_DIR/"
# 复制维护脚本
mkdir -p "$APP_DIR/scripts"
cp "$SOURCE_ROOT/Server-Version/"*.sh "$APP_DIR/scripts/"
chmod +x "$APP_DIR/scripts/"*.sh

cp "$SOURCE_ROOT/backend/requirements.txt" "$APP_DIR/backend/"

# 创建数据目录并设置权限
mkdir -p "$APP_DIR/backend/data"
touch "$APP_DIR/backend/app.log"
# 确保数据目录可写
chmod -R 777 "$APP_DIR/backend/data"
chmod 666 "$APP_DIR/backend/app.log"

# 4. 配置 Python 环境
echo -e "${YELLOW}[3/5] 正在配置 Python 虚拟环境...${NC}"

cd "$APP_DIR/backend"
if [ ! -d "../venv" ]; then
    python3 -m venv ../venv
fi

source ../venv/bin/activate
pip install --upgrade pip -q
echo "正在安装 Python 依赖库 (可能需要几分钟)..."
# 使用清华源加速 (可选，视网络情况而定，这里使用默认源以保证兼容)
pip install -r requirements.txt --no-cache-dir

# 5. 配置 Systemd 服务
echo -e "${YELLOW}[4/5] 配置系统后台服务...${NC}"

SERVICE_FILE="/etc/systemd/system/limit-up-sniper.service"

cat > $SERVICE_FILE <<EOF
[Unit]
Description=Limit-Up Sniper Commercial Backend
After=network.target

[Service]
# 为简单起见使用 root 运行，如需更安全请修改为专用用户
User=root
Group=root
WorkingDirectory=$APP_DIR/backend
Environment="PATH=$APP_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable limit-up-sniper
systemctl restart limit-up-sniper

# 6. 配置 Nginx
echo -e "${YELLOW}[5/5] 配置 Nginx 反向代理...${NC}"

# 获取公网 IP
SERVER_IP=$(curl -s ifconfig.me || echo "您的服务器IP")
read -p "请输入服务器域名或IP (默认为: $SERVER_IP): " USER_IP
USER_IP=${USER_IP:-$SERVER_IP}

# Nginx 配置文件路径
if [ -d "/etc/nginx/sites-available" ]; then
    # Debian/Ubuntu
    NGINX_CONF="/etc/nginx/sites-available/limit-up-sniper"
    NGINX_LINK="/etc/nginx/sites-enabled/limit-up-sniper"
    mkdir -p /etc/nginx/sites-enabled
else
    # CentOS/RHEL
    NGINX_CONF="/etc/nginx/conf.d/limit-up-sniper.conf"
    NGINX_LINK=""
fi

# 写入 Nginx 配置
cat > $NGINX_CONF <<EOF
server {
    listen 80;
    server_name $USER_IP;

    client_max_body_size 10M;

    # 代理所有请求到后端 (由 FastAPI 处理静态文件和 API)
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # WebSocket 支持
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 86400;
    }
}
EOF

if [ ! -z "$NGINX_LINK" ]; then
    ln -sf $NGINX_CONF $NGINX_LINK
    rm -f /etc/nginx/sites-enabled/default
fi

# 测试 Nginx
nginx -t
if [ $? -eq 0 ]; then
    systemctl restart nginx
    systemctl enable nginx
else
    echo -e "${RED}[错误] Nginx 配置测试失败，请检查日志。${NC}"
fi

# 最终输出 - 等待后端生成 Token
sleep 5
ADMIN_TOKEN_FILE="$APP_DIR/backend/data/admin_token.txt"
ADMIN_TOKEN="请查看文件 $ADMIN_TOKEN_FILE"

if [ -f "$ADMIN_TOKEN_FILE" ]; then
    ADMIN_TOKEN=$(cat "$ADMIN_TOKEN_FILE")
fi

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}   ✅ 部署成功!                          ${NC}"
echo -e "${GREEN}=========================================${NC}"
echo -e "前台访问地址: http://$USER_IP/"
echo -e "后台管理地址: http://$USER_IP/admin/index.html"
echo -e "管理员 Token: ${YELLOW}$ADMIN_TOKEN${NC} (用于登录后台)"
echo -e "查看日志命令: journalctl -u limit-up-sniper -f"
