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
    elif [[ "$OS" == *"CentOS"* ]] || [[ "$OS" == *"Red Hat"* ]] || [[ "$OS" == *"Alibaba"* ]] || [[ "$OS" == *"Tencent"* ]] || [[ "$OS" == *"Fedora"* ]] || [[ "$OS" == *"OpenCloudOS"* ]] || [[ "$OS" == *"Rocky"* ]] || [[ "$OS" == *"Alma"* ]]; then
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

# 4.1. 配置 API Key (使用环境变量)
echo -e "${YELLOW}[3.5/5] 配置 API Key...${NC}"

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

# 创建数据目录(如果不存在)
DATA_DIR="$APP_DIR/backend/data"
mkdir -p "$DATA_DIR"
CONFIG_FILE="$DATA_DIR/config.json"

# 如果配置文件不存在，创建一个默认文件
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<EOF
{
    "api_keys": {
        "deepseek": "",
        "aliyun": "",
        "other": ""
    },
    "auto_analysis_enabled": false,
    "use_smart_schedule": true,
    "fixed_interval_minutes": 60,
    "lhb_enabled": true,
    "lhb_days": 3,
    "lhb_min_amount": 20000000,
    "data_provider_config": {
        "biying_enabled": false,
        "biying_license_key": "",
        "biying_endpoint": "",
        "biying_cert_path": "",
        "biying_daily_limit": 200
    }
}
EOF
fi

# 读取已有必盈配置，作为默认值
BIYING_ENABLED_DEFAULT="false"
BIYING_KEY_DEFAULT=""
BIYING_ENDPOINT_DEFAULT="https://api.biyingapi.com"
BIYING_CERT_DEFAULT=""
BIYING_DAILY_LIMIT_DEFAULT="200"
eval "$(python3 - "$CONFIG_FILE" <<'PY'
import json
import shlex
import sys

path = sys.argv[1]
cfg = {}
try:
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg = loaded
except Exception:
    cfg = {}

provider = cfg.get("data_provider_config") or {}

def quote(v):
    return shlex.quote(str(v if v is not None else ""))

print("BIYING_ENABLED_DEFAULT=" + quote("true" if provider.get("biying_enabled") else "false"))
print("BIYING_KEY_DEFAULT=" + quote(provider.get("biying_license_key", "")))
existing_endpoint = str(provider.get("biying_endpoint", "") or "").strip()
if not existing_endpoint:
    existing_endpoint = "https://api.biyingapi.com"
print("BIYING_ENDPOINT_DEFAULT=" + quote(existing_endpoint))
print("BIYING_CERT_DEFAULT=" + quote(provider.get("biying_cert_path", "")))
try:
    daily_limit = int(provider.get("biying_daily_limit", 200) or 200)
except Exception:
    daily_limit = 200
if daily_limit < 1:
    daily_limit = 200
print("BIYING_DAILY_LIMIT_DEFAULT=" + quote(daily_limit))
PY
)"

if [ "$BIYING_ENABLED_DEFAULT" = "true" ]; then
    BIYING_ENABLED_HINT="Y"
else
    BIYING_ENABLED_HINT="N"
fi

read -p "是否启用必盈数据源? [y/N] (当前: $BIYING_ENABLED_HINT): " INPUT_BIYING_ENABLED
case "$INPUT_BIYING_ENABLED" in
    [Yy]|[Yy][Ee][Ss]) BIYING_ENABLED="true" ;;
    [Nn]|[Nn][Oo]) BIYING_ENABLED="false" ;;
    "") BIYING_ENABLED="$BIYING_ENABLED_DEFAULT" ;;
    *) BIYING_ENABLED="$BIYING_ENABLED_DEFAULT" ;;
esac

if [ "$BIYING_ENABLED" = "true" ]; then
    read -p "请输入必盈 License Key (回车使用当前值): " INPUT_BIYING_KEY
    BIYING_LICENSE_KEY="${INPUT_BIYING_KEY:-$BIYING_KEY_DEFAULT}"
    BIYING_ENDPOINT="$BIYING_ENDPOINT_DEFAULT"
    BIYING_CERT_PATH="$BIYING_CERT_DEFAULT"
    BIYING_DAILY_LIMIT="$BIYING_DAILY_LIMIT_DEFAULT"
else
    BIYING_LICENSE_KEY="$BIYING_KEY_DEFAULT"
    BIYING_ENDPOINT="$BIYING_ENDPOINT_DEFAULT"
    BIYING_CERT_PATH="$BIYING_CERT_DEFAULT"
    BIYING_DAILY_LIMIT="$BIYING_DAILY_LIMIT_DEFAULT"
fi

if ! [[ "$BIYING_DAILY_LIMIT" =~ ^[0-9]+$ ]] || [ "$BIYING_DAILY_LIMIT" -lt 1 ]; then
    BIYING_DAILY_LIMIT=200
fi

# 合并写回配置，确保旧配置不会丢失
python3 - "$CONFIG_FILE" "$API_KEY" "$BIYING_ENABLED" "$BIYING_LICENSE_KEY" "$BIYING_ENDPOINT" "$BIYING_CERT_PATH" "$BIYING_DAILY_LIMIT" <<'PY'
import json
import sys

config_path = sys.argv[1]
api_key = sys.argv[2]
biying_enabled = sys.argv[3].lower() == "true"
biying_license_key = sys.argv[4]
biying_endpoint = sys.argv[5]
biying_cert_path = sys.argv[6]
try:
    biying_daily_limit = int(sys.argv[7] or 200)
except Exception:
    biying_daily_limit = 200
if biying_daily_limit < 1:
    biying_daily_limit = 200

config = {}
try:
    with open(config_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
        if isinstance(loaded, dict):
            config = loaded
except Exception:
    config = {}

api_keys = config.get("api_keys")
if not isinstance(api_keys, dict):
    api_keys = {}
if api_key:
    api_keys["deepseek"] = api_key
else:
    api_keys.setdefault("deepseek", "")
api_keys.setdefault("aliyun", "")
api_keys.setdefault("other", "")
config["api_keys"] = api_keys

provider = config.get("data_provider_config")
if not isinstance(provider, dict):
    provider = {}
provider["biying_enabled"] = biying_enabled
provider["biying_license_key"] = biying_license_key
provider["biying_endpoint"] = biying_endpoint
provider["biying_cert_path"] = biying_cert_path
provider["biying_daily_limit"] = biying_daily_limit
config["data_provider_config"] = provider

config.setdefault("auto_analysis_enabled", False)
config.setdefault("use_smart_schedule", True)
config.setdefault("fixed_interval_minutes", 60)
config.setdefault("lhb_enabled", True)
config.setdefault("lhb_days", 3)
config.setdefault("lhb_min_amount", 20000000)

with open(config_path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
PY

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
Environment="DEEPSEEK_API_KEY=$API_KEY"
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
echo -e "${YELLOW}正在等待后端初始化并生成管理员 Token (最多等待 30 秒)...${NC}"
ADMIN_TOKEN_FILE="$APP_DIR/backend/data/admin_token.txt"
ADMIN_TOKEN="获取失败，请稍后手动查看: cat $ADMIN_TOKEN_FILE"

# 循环检查 Token 文件 (最多 30 秒)
for i in {1..15}; do
    if [ -f "$ADMIN_TOKEN_FILE" ]; then
        #给予写入完成的一点缓冲时间
        sleep 1
        ADMIN_TOKEN=$(cat "$ADMIN_TOKEN_FILE")
        if [ ! -z "$ADMIN_TOKEN" ]; then
            break
        fi
    fi
    sleep 2
done

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}   ✅ 部署成功!                          ${NC}"
echo -e "${GREEN}=========================================${NC}"
echo -e "前台访问地址: http://$USER_IP/"
echo -e "后台管理地址: http://$USER_IP/admin/index.html"
echo -e "管理员 Token: ${YELLOW}$ADMIN_TOKEN${NC} (用于登录后台)"
echo -e "查看日志命令: journalctl -u limit-up-sniper -f"
