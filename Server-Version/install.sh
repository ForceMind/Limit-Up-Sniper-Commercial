#!/bin/bash

# Limit-Up Sniper 一键部署脚本
# 适用系统: Ubuntu 20.04/22.04 LTS
# 用法: sudo ./install.sh

set -e

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}   Limit-Up Sniper 一键部署脚本          ${NC}"
echo -e "${GREEN}=========================================${NC}"

# 1. 检查 Root 权限
if [ "$EUID" -ne 0 ]; then 
  echo -e "${RED}[Error] 请使用 sudo 运行此脚本: sudo ./install.sh${NC}"
  exit 1
fi

# 2. 安装系统依赖
echo -e "${YELLOW}[1/6] 正在安装系统依赖...${NC}"

if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$NAME
fi

if [[ "$OS" == *"Alibaba"* ]] || [[ "$OS" == *"CentOS"* ]] || [[ "$OS" == *"Red Hat"* ]]; then
    echo "Detected RedHat/CentOS/Alibaba Cloud Linux..."
    # 尝试安装更高版本的 Python (Akshare 需要 3.10+)
    if command -v dnf > /dev/null; then
        dnf install -y git nginx python3.11 python3.11-pip python3.11-devel || yum install -y git python3 python3-pip nginx
    else
        yum install -y git python3 python3-pip nginx
    fi
else
    # Assume Debian/Ubuntu
    apt update -qq
    apt install -y python3 python3-pip python3-venv git nginx -qq
fi

# 3. 设置 Python 环境
echo -e "${YELLOW}[2/6] 配置 Python 虚拟环境...${NC}"
APP_DIR=$(pwd)
PROJECT_ROOT=$(dirname "$APP_DIR")

# 寻找合适的 Python 版本 (优先 3.11 > 3.10 > 3.9 > 3)
PYTHON_EXE="python3"
if command -v python3.12 > /dev/null; then PYTHON_EXE="python3.12"
elif command -v python3.11 > /dev/null; then PYTHON_EXE="python3.11"
elif command -v python3.10 > /dev/null; then PYTHON_EXE="python3.10"
fi

# 检查最终选定的 Python 版本是否满足要求
PY_VERSION=$($PYTHON_EXE -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Selected Python: $PYTHON_EXE (Version $PY_VERSION)"

if [ $(echo "$PY_VERSION < 3.10" | bc -l) -eq 1 ]; then
    echo -e "${RED}[Error] Akshare 需要 Python 3.10+, 但当前系统中最高版本为 $PY_VERSION${NC}"
    echo -e "${YELLOW}建议手动安装: sudo dnf install python3.11 (Alibaba/CentOS) 或 sudo apt install python3.11 (Ubuntu)${NC}"
    exit 1
fi

if [ ! -d "venv" ]; then
    $PYTHON_EXE -m venv venv || {
        echo -e "${YELLOW}venv 模块缺失，尝试安装...${NC}"
        if [[ "$OS" == *"Alibaba"* ]] || [[ "$OS" == *"CentOS"* ]]; then
             # RedHat 系通常不需要单独安装 venv
             exit 1
        else
             apt install -y python3-venv -qq
             $PYTHON_EXE -m venv venv
        fi
    }
fi
source venv/bin/activate
echo "正在安装 Python 依赖 (这可能需要几分钟)..."
pip install --upgrade pip -q
pip install -r "$PROJECT_ROOT/requirements.txt" -q

# 4. 配置 API Key
echo -e "${YELLOW}[3/6] 配置 Deepseek API...${NC}"

# 尝试从现有服务文件中读取 API Key
DEFAULT_KEY=""
SERVICE_FILE="/etc/systemd/system/limit-up-sniper.service"
if [ -f "$SERVICE_FILE" ]; then
    # 提取 Key (假设格式为 Environment="DEEPSEEK_API_KEY=sk-...")
    EXISTING_KEY=$(grep "DEEPSEEK_API_KEY" $SERVICE_FILE | cut -d'=' -f3- | tr -d '"')
    if [ ! -z "$EXISTING_KEY" ]; then
        DEFAULT_KEY=$EXISTING_KEY
        echo "检测到现有 API Key: ${DEFAULT_KEY:0:5}******${DEFAULT_KEY: -4}"
    fi
fi

read -p "请输入您的 Deepseek API Key (回车使用现有 Key): " INPUT_KEY
API_KEY=${INPUT_KEY:-$DEFAULT_KEY}

if [ -z "$API_KEY" ]; then
    echo -e "${RED}[Error] API Key 不能为空。${NC}"
    exit 1
fi

# 5. 配置 Systemd 服务
echo -e "${YELLOW}[4/6] 配置后台服务 (Systemd)...${NC}"

# 确定运行用户
RUN_USER=$SUDO_USER
if [ -z "$RUN_USER" ]; then
    RUN_USER="root"
fi

SERVICE_FILE="/etc/systemd/system/limit-up-sniper.service"
cat > $SERVICE_FILE <<EOF
[Unit]
Description=Limit-Up Sniper FastAPI Service
After=network.target

[Service]
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$PROJECT_ROOT
Environment="PATH=$APP_DIR/venv/bin"
Environment="DEEPSEEK_API_KEY=$API_KEY"
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable limit-up-sniper
systemctl restart limit-up-sniper

# 6. 配置 Nginx
echo -e "${YELLOW}[5/6] 配置 Nginx 反向代理...${NC}"

# 尝试获取公网 IP
SERVER_IP=$(curl -s ifconfig.me || echo "your_server_ip")
read -p "请输入服务器 IP 或域名 (默认: $SERVER_IP): " USER_IP
USER_IP=${USER_IP:-$SERVER_IP}

# 根据系统确定 Nginx 配置文件路径
if [[ "$OS" == *"Alibaba"* ]] || [[ "$OS" == *"CentOS"* ]] || [[ "$OS" == *"Red Hat"* ]]; then
    # RedHat/Alibaba 系路径
    NGINX_CONF="/etc/nginx/conf.d/limit-up-sniper.conf"
    NGINX_TYPE="redhat"
else
    # Debian/Ubuntu 系路径
    NGINX_CONF="/etc/nginx/sites-available/limit-up-sniper"
    NGINX_TYPE="debian"
fi

cat > $NGINX_CONF <<EOF
server {
    listen 80;
    server_name $USER_IP;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

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

if [ "$NGINX_TYPE" == "debian" ]; then
    mkdir -p /etc/nginx/sites-enabled/
    ln -sf $NGINX_CONF /etc/nginx/sites-enabled/
    # 移除默认配置以避免冲突
    rm -f /etc/nginx/sites-enabled/default
fi

nginx -t
if [ $? -eq 0 ]; then
    systemctl restart nginx
    echo -e "${GREEN}Nginx 配置成功！${NC}"
else
    echo -e "${RED}Nginx 配置测试失败，请检查配置文件。${NC}"
fi
    systemctl restart nginx
else
    echo -e "${RED}[Error] Nginx 配置有误，请检查。${NC}"
    exit 1
fi

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}   ✅ 部署成功! (Deployment Success)     ${NC}"
echo -e "${GREEN}=========================================${NC}"
echo -e "访问地址: http://$USER_IP"
echo -e "查看日志: sudo journalctl -u limit-up-sniper -f"
