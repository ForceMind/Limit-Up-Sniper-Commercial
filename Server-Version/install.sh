#!/usr/bin/env bash

# 涨停狙击手商业版一键安装脚本
# 用法: sudo ./install.sh

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

APP_NAME="limit-up-sniper-commercial"
APP_DIR="/opt/${APP_NAME}"
SERVICE_NAME="${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULT_INTERNAL_PORT="8000"
INTERNAL_PORT="$DEFAULT_INTERNAL_PORT"
DEFAULT_EXTERNAL_PORT="80"
EXTERNAL_PORT="$DEFAULT_EXTERNAL_PORT"
USER_IP=""
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"

log_info() { echo -e "${GREEN}$1${NC}"; }
log_warn() { echo -e "${YELLOW}$1${NC}"; }
log_error() { echo -e "${RED}$1${NC}"; }

require_root() {
    if [ "${EUID}" -ne 0 ]; then
        log_error "[错误] 请使用 sudo 或 root 权限运行安装脚本"
        exit 1
    fi
}

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_NAME="${NAME:-Unknown}"
    else
        OS_NAME="Unknown"
    fi
}

is_valid_port() {
    local port="$1"
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ]
}

is_port_in_use() {
    local port="$1"

    if command -v ss >/dev/null 2>&1; then
        ss -ltnH "sport = :$port" 2>/dev/null | grep -q .
        return
    fi

    if command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1
        return
    fi

    if command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"
        return
    fi

    python3 - "$port" <<'PY' >/dev/null 2>&1
import socket
import sys

port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("0.0.0.0", port))
except OSError:
    sys.exit(0)
finally:
    s.close()
sys.exit(1)
PY
}

resolve_internal_port() {
    local preferred="$1"
    local candidate="$preferred"

    if ! is_valid_port "$candidate"; then
        candidate="$DEFAULT_INTERNAL_PORT"
    fi

    while is_port_in_use "$candidate"; do
        candidate=$((candidate + 1))
        if [ "$candidate" -gt 65535 ]; then
            log_error "[错误] 未找到可用内网端口"
            exit 1
        fi
    done

    INTERNAL_PORT="$candidate"
}

configure_ports() {
    log_warn "[4/7] 配置端口（外网自定义 + 内网自动避让）..."

    local existing_internal="$DEFAULT_INTERNAL_PORT"
    if [ -f "$SERVICE_FILE" ]; then
        local parsed_internal
        parsed_internal=$(grep -Eo -- '--port[[:space:]]+[0-9]+' "$SERVICE_FILE" | head -n 1 | awk '{print $2}') || true
        if is_valid_port "$parsed_internal"; then
            existing_internal="$parsed_internal"
        fi
    fi

    resolve_internal_port "$existing_internal"
    if [ "$INTERNAL_PORT" = "$existing_internal" ]; then
        log_info "内网端口使用: $INTERNAL_PORT"
    else
        log_warn "内网端口 $existing_internal 已被占用，自动切换为: $INTERNAL_PORT"
    fi

    local input_external
    while true; do
        read -r -p "请输入外网访问端口（默认: $DEFAULT_EXTERNAL_PORT）: " input_external
        EXTERNAL_PORT="${input_external:-$DEFAULT_EXTERNAL_PORT}"

        if ! is_valid_port "$EXTERNAL_PORT"; then
            log_warn "端口无效，请输入 1-65535"
            continue
        fi

        if [ "$EXTERNAL_PORT" = "$INTERNAL_PORT" ]; then
            log_warn "外网端口不能与内网端口相同（当前内网: $INTERNAL_PORT）"
            continue
        fi

        if is_port_in_use "$EXTERNAL_PORT"; then
            log_warn "端口 $EXTERNAL_PORT 已被占用，请更换"
            continue
        fi

        break
    done

    log_info "外网端口使用: $EXTERNAL_PORT"
}

install_deps() {
    log_warn "[1/7] 安装系统依赖..."

    if [[ "$OS_NAME" == *"Ubuntu"* ]] || [[ "$OS_NAME" == *"Debian"* ]]; then
        apt-get update -qq
        apt-get install -y python3 python3-pip python3-venv python3-dev build-essential git nginx curl bc
        return
    fi

    if [[ "$OS_NAME" == *"CentOS"* ]] || [[ "$OS_NAME" == *"Red Hat"* ]] || [[ "$OS_NAME" == *"Alibaba"* ]] || [[ "$OS_NAME" == *"Tencent"* ]] || [[ "$OS_NAME" == *"Fedora"* ]] || [[ "$OS_NAME" == *"OpenCloudOS"* ]] || [[ "$OS_NAME" == *"Rocky"* ]] || [[ "$OS_NAME" == *"Alma"* ]]; then
        if command -v dnf >/dev/null 2>&1; then
            PKG_MGR="dnf"
        else
            PKG_MGR="yum"
        fi
        "$PKG_MGR" install -y epel-release || true
        "$PKG_MGR" install -y python3 python3-pip python3-devel gcc git nginx curl bc
        return
    fi

    log_warn "[警告] 未识别系统: $OS_NAME，尝试通用安装"
    if command -v apt >/dev/null 2>&1; then
        apt update
        apt install -y python3 python3-pip python3-venv git nginx curl bc
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 python3-pip python3-devel gcc git nginx curl bc
    else
        log_error "[错误] 无法自动安装依赖，请手动安装 python3/pip/nginx"
        exit 1
    fi
}

check_source_tree() {
    if [ ! -d "$SOURCE_ROOT/backend" ] || [ ! -d "$SOURCE_ROOT/frontend" ]; then
        log_error "[错误] 源码目录不完整: $SOURCE_ROOT"
        log_error "必须包含 backend/ 与 frontend/"
        exit 1
    fi
}

prepare_backup_dirs() {
    RUNTIME_BACKUP_DIR="$(mktemp -d /tmp/${APP_NAME}-install-backup.XXXXXX)"
    mkdir -p "$RUNTIME_BACKUP_DIR/backend" "$RUNTIME_BACKUP_DIR/frontend"
    HAD_OLD_DATA="false"
    HAD_OLD_FRONT_CONFIG="false"

    if [ -d "$APP_DIR/backend/data" ]; then
        cp -a "$APP_DIR/backend/data" "$RUNTIME_BACKUP_DIR/backend/data"
        HAD_OLD_DATA="true"
    fi

    if [ -f "$APP_DIR/frontend/config.js" ]; then
        cp -a "$APP_DIR/frontend/config.js" "$RUNTIME_BACKUP_DIR/frontend/config.js"
        HAD_OLD_FRONT_CONFIG="true"
    fi
}

deploy_code_only() {
    log_warn "[2/7] 部署代码文件（不覆盖运行时数据）..."
    systemctl stop "$SERVICE_NAME" || true

    mkdir -p "$APP_DIR"
    rm -rf "$APP_DIR/backend" "$APP_DIR/frontend"
    cp -a "$SOURCE_ROOT/backend" "$APP_DIR/"
    cp -a "$SOURCE_ROOT/frontend" "$APP_DIR/"

    # 防止仓库中的 data 文件覆盖线上运行数据
    rm -rf "$APP_DIR/backend/data"
    mkdir -p "$APP_DIR/backend/data"

    if [ "$HAD_OLD_DATA" = "true" ] && [ -d "$RUNTIME_BACKUP_DIR/backend/data" ]; then
        cp -a "$RUNTIME_BACKUP_DIR/backend/data/." "$APP_DIR/backend/data/"
        log_info "已保留服务器原有 backend/data"
    else
        log_warn "未检测到历史 data，已创建空目录"
    fi

    if [ "$HAD_OLD_FRONT_CONFIG" = "true" ] && [ -f "$RUNTIME_BACKUP_DIR/frontend/config.js" ]; then
        cp -a "$RUNTIME_BACKUP_DIR/frontend/config.js" "$APP_DIR/frontend/config.js"
        log_info "已保留服务器原有 frontend/config.js"
    fi

    mkdir -p "$APP_DIR/scripts"
    if compgen -G "$SOURCE_ROOT/Server-Version/*.sh" > /dev/null; then
        cp -a "$SOURCE_ROOT/Server-Version/"*.sh "$APP_DIR/scripts/"
        chmod +x "$APP_DIR/scripts/"*.sh || true
    fi

    chmod -R 755 "$APP_DIR/frontend" || true
}

ensure_runtime_files() {
    log_warn "[3/7] 初始化运行目录与默认文件..."

    DATA_DIR="$APP_DIR/backend/data"
    CONFIG_FILE="$DATA_DIR/config.json"
    mkdir -p "$DATA_DIR"

    if [ ! -f "$CONFIG_FILE" ]; then
        cat > "$CONFIG_FILE" <<'EOF'
{
  "auto_analysis_enabled": true,
  "use_smart_schedule": true,
  "fixed_interval_minutes": 60,
  "email_config": {
    "enabled": false,
    "smtp_server": "",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_password": "",
    "recipient_email": ""
  },
  "api_keys": {
    "deepseek": "",
    "aliyun": "",
    "other": ""
  },
  "data_provider_config": {
    "biying_enabled": false,
    "biying_license_key": "",
    "biying_endpoint": "https://api.biyingapi.com",
    "biying_cert_path": "",
    "biying_minute_limit": 3000
  },
  "community_config": {
    "qq_group_number": "",
    "qq_group_link": "",
    "welcome_text": "欢迎加入技术交流群，获取版本更新与使用答疑。"
  },
  "referral_config": {
    "enabled": true,
    "reward_days": 30,
    "share_base_url": "",
    "share_template": "我在用涨停狙击手，注册链接：{invite_link}，邀请码：{invite_code}。注册后在充值页填写邀请码，可获得赠送权益。"
  }
}
EOF
    fi

    touch "$APP_DIR/backend/app.log"
    chmod -R 777 "$DATA_DIR" || true
    chmod 666 "$APP_DIR/backend/app.log" || true

    # 安装时强制重置后台路径为默认 /admin
    ADMIN_PANEL_PATH_FILE="$DATA_DIR/admin_panel_path.json"
    cat > "$ADMIN_PANEL_PATH_FILE" <<EOF
{
  "path": "/admin",
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    log_info "后台路径已重置为默认: /admin"
}

ensure_lhb_data_files() {
    local target_data_dir="$APP_DIR/backend/data"
    local source_data_dir="$SOURCE_ROOT/backend/data"

    mkdir -p "$target_data_dir"

    if [ ! -f "$target_data_dir/seat_mappings.json" ]; then
        if [ -f "$source_data_dir/seat_mappings.json" ]; then
            cp -a "$source_data_dir/seat_mappings.json" "$target_data_dir/seat_mappings.json"
            log_info "已补齐 seat_mappings.json"
        else
            echo '{}' > "$target_data_dir/seat_mappings.json"
            log_warn "未找到 seat_mappings.json 模板，已创建空映射文件"
        fi
    fi

    if [ ! -f "$target_data_dir/vip_seats.json" ]; then
        if [ -f "$source_data_dir/vip_seats.json" ]; then
            cp -a "$source_data_dir/vip_seats.json" "$target_data_dir/vip_seats.json"
            log_info "已补齐 vip_seats.json"
        else
            echo '[]' > "$target_data_dir/vip_seats.json"
            log_warn "未找到 vip_seats.json 模板，已创建空 VIP 列表"
        fi
    fi
}

read_existing_config_values() {
    CONFIG_FILE="$APP_DIR/backend/data/config.json"

    eval "$(python3 - "$CONFIG_FILE" <<'PY'
import json
import shlex
import sys

path = sys.argv[1]
cfg = {}
try:
    with open(path, 'r', encoding='utf-8') as f:
        loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg = loaded
except Exception:
    cfg = {}

api_keys = cfg.get('api_keys') if isinstance(cfg.get('api_keys'), dict) else {}
provider = cfg.get('data_provider_config') if isinstance(cfg.get('data_provider_config'), dict) else {}

def q(v):
    return shlex.quote(str(v if v is not None else ''))

def q_bool(v):
    return shlex.quote('true' if bool(v) else 'false')

print('CONFIG_DEEPSEEK=' + q(api_keys.get('deepseek', '')))
print('BIYING_ENABLED_DEFAULT=' + q_bool(provider.get('biying_enabled', False)))
print('BIYING_KEY_DEFAULT=' + q(provider.get('biying_license_key', '')))
endpoint = str(provider.get('biying_endpoint', '') or '').strip() or 'https://api.biyingapi.com'
print('BIYING_ENDPOINT_DEFAULT=' + q(endpoint))
print('BIYING_CERT_DEFAULT=' + q(provider.get('biying_cert_path', '')))
try:
    daily = int(provider.get('biying_minute_limit', 3000) or 3000)
except Exception:
    daily = 3000
if daily < 1:
    daily = 3000
print('BIYING_MINUTE_LIMIT_DEFAULT=' + q(daily))
PY
)"

    DEFAULT_KEY="$CONFIG_DEEPSEEK"
    if [ -z "$DEFAULT_KEY" ] && [ -f "$SERVICE_FILE" ]; then
        SERVICE_KEY=$(grep -E '^Environment="DEEPSEEK_API_KEY=' "$SERVICE_FILE" | head -n 1 | sed -E 's/^Environment="DEEPSEEK_API_KEY=([^\"]*)"/\1/') || true
        DEFAULT_KEY="$SERVICE_KEY"
    fi
}

prompt_keys_and_merge_config() {
    CONFIG_FILE="$APP_DIR/backend/data/config.json"

    if [ -n "$DEFAULT_KEY" ]; then
        log_info "检测到已有 Deepseek Key: ${DEFAULT_KEY:0:5}******${DEFAULT_KEY: -4}"
    fi
    read -r -p "请输入 Deepseek API Key（回车保留现有）：" INPUT_KEY
    API_KEY="${INPUT_KEY:-$DEFAULT_KEY}"

    if [ "$BIYING_ENABLED_DEFAULT" = "true" ]; then
        BIYING_HINT="Y"
    else
        BIYING_HINT="N"
    fi

    read -r -p "是否启用必盈数据源 [y/N]（当前: $BIYING_HINT）：" INPUT_BIYING_ENABLED
    case "$INPUT_BIYING_ENABLED" in
        [Yy]|[Yy][Ee][Ss]) BIYING_ENABLED="true" ;;
        [Nn]|[Nn][Oo]) BIYING_ENABLED="false" ;;
        "") BIYING_ENABLED="$BIYING_ENABLED_DEFAULT" ;;
        *) BIYING_ENABLED="$BIYING_ENABLED_DEFAULT" ;;
    esac

    if [ "$BIYING_ENABLED" = "true" ]; then
        read -r -p "请输入必盈 License Key（回车保留现有）：" INPUT_BIYING_KEY
        BIYING_LICENSE_KEY="${INPUT_BIYING_KEY:-$BIYING_KEY_DEFAULT}"
    else
        BIYING_LICENSE_KEY="$BIYING_KEY_DEFAULT"
    fi

    BIYING_ENDPOINT="$BIYING_ENDPOINT_DEFAULT"
    BIYING_CERT_PATH="$BIYING_CERT_DEFAULT"
    BIYING_MINUTE_LIMIT="$BIYING_MINUTE_LIMIT_DEFAULT"

    if ! [[ "$BIYING_MINUTE_LIMIT" =~ ^[0-9]+$ ]] || [ "$BIYING_MINUTE_LIMIT" -lt 1 ]; then
        BIYING_MINUTE_LIMIT=3000
    fi

    python3 - "$CONFIG_FILE" "$API_KEY" "$BIYING_ENABLED" "$BIYING_LICENSE_KEY" "$BIYING_ENDPOINT" "$BIYING_CERT_PATH" "$BIYING_MINUTE_LIMIT" <<'PY'
import json
import sys

path = sys.argv[1]
api_key = sys.argv[2]
biying_enabled = sys.argv[3].lower() == 'true'
biying_key = sys.argv[4]
biying_endpoint = sys.argv[5]
biying_cert = sys.argv[6]
try:
    biying_minute = int(sys.argv[7] or 3000)
except Exception:
    biying_minute = 3000
if biying_minute < 1:
    biying_minute = 3000

cfg = {}
try:
    with open(path, 'r', encoding='utf-8') as f:
        loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg = loaded
except Exception:
    cfg = {}

api_keys = cfg.get('api_keys') if isinstance(cfg.get('api_keys'), dict) else {}
if api_key:
    api_keys['deepseek'] = api_key
api_keys.setdefault('deepseek', '')
api_keys.setdefault('aliyun', '')
api_keys.setdefault('other', '')
cfg['api_keys'] = api_keys

provider = cfg.get('data_provider_config') if isinstance(cfg.get('data_provider_config'), dict) else {}
provider['biying_enabled'] = biying_enabled
provider['biying_license_key'] = biying_key
provider['biying_endpoint'] = biying_endpoint
provider['biying_cert_path'] = biying_cert
provider['biying_minute_limit'] = biying_minute
provider.pop('biying_daily_limit', None)
cfg['data_provider_config'] = provider

email_cfg = cfg.get('email_config') if isinstance(cfg.get('email_config'), dict) else {}
email_cfg.setdefault('enabled', False)
email_cfg.setdefault('smtp_server', '')
email_cfg.setdefault('smtp_port', 465)
email_cfg.setdefault('smtp_user', '')
email_cfg.setdefault('smtp_password', '')
email_cfg.setdefault('recipient_email', '')
cfg['email_config'] = email_cfg

community_cfg = cfg.get('community_config') if isinstance(cfg.get('community_config'), dict) else {}
community_cfg.setdefault('qq_group_number', '')
community_cfg.setdefault('qq_group_link', '')
community_cfg.setdefault('welcome_text', '欢迎加入技术交流群，获取版本更新与使用答疑。')
cfg['community_config'] = community_cfg

referral_cfg = cfg.get('referral_config') if isinstance(cfg.get('referral_config'), dict) else {}
referral_cfg.setdefault('enabled', True)
referral_cfg.setdefault('reward_days', 30)
referral_cfg.setdefault('share_base_url', '')
referral_cfg.setdefault('share_template', '我在用涨停狙击手，注册链接：{invite_link}，邀请码：{invite_code}。注册后在充值页填写邀请码，可获得赠送权益。')
cfg['referral_config'] = referral_cfg

cfg.setdefault('auto_analysis_enabled', True)
cfg.setdefault('use_smart_schedule', True)
cfg.setdefault('fixed_interval_minutes', 60)
cfg.setdefault('lhb_enabled', True)
cfg.setdefault('lhb_days', 3)
cfg.setdefault('lhb_min_amount', 20000000)

with open(path, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PY

    FINAL_DEEPSEEK_KEY="$(python3 - "$CONFIG_FILE" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        data = json.load(f)
        keys = data.get('api_keys') if isinstance(data.get('api_keys'), dict) else {}
        print(str(keys.get('deepseek', '') or '').strip())
except Exception:
    print('')
PY
)"
}

setup_python_venv() {
    log_warn "[5/7] 配置 Python 虚拟环境并安装依赖..."

    mkdir -p "$APP_DIR"
    cd "$APP_DIR/backend"

    if [ ! -d "$APP_DIR/venv" ]; then
        python3 -m venv "$APP_DIR/venv"
    fi

    # shellcheck disable=SC1091
    source "$APP_DIR/venv/bin/activate"
    pip install --upgrade pip -q
    pip install -r requirements.txt --no-cache-dir
}

setup_systemd() {
    log_warn "[6/7] 配置 systemd 服务..."

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Limit-Up Sniper Commercial Backend
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=$APP_DIR/backend
Environment="PATH=$APP_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="DEEPSEEK_API_KEY=$FINAL_DEEPSEEK_KEY"
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $INTERNAL_PORT --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
}

setup_nginx() {
    log_warn "[7/7] 配置 Nginx 反向代理..."

    SERVER_IP="$(curl -s ifconfig.me || true)"
    SERVER_IP="${SERVER_IP:-服务器IP}"
    read -r -p "请输入服务器域名或IP（默认: $SERVER_IP）：" USER_IP
    USER_IP="${USER_IP:-$SERVER_IP}"

    if [ -d "/etc/nginx/sites-available" ]; then
        NGINX_CONF="/etc/nginx/sites-available/${APP_NAME}"
        NGINX_LINK="/etc/nginx/sites-enabled/${APP_NAME}"
        mkdir -p /etc/nginx/sites-enabled
    else
        NGINX_CONF="/etc/nginx/conf.d/${APP_NAME}.conf"
        NGINX_LINK=""
    fi

    cat > "$NGINX_CONF" <<EOF
server {
    listen $EXTERNAL_PORT;
    server_name $USER_IP;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:$INTERNAL_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /ws {
        proxy_pass http://127.0.0.1:$INTERNAL_PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 86400;
    }
}
EOF

    if [ -n "$NGINX_LINK" ]; then
        ln -sf "$NGINX_CONF" "$NGINX_LINK"
        rm -f /etc/nginx/sites-enabled/default
    fi

    nginx -t
    systemctl enable nginx
    systemctl restart nginx
}

show_result() {
    ADMIN_HINT="已保留服务器现有管理员账号配置（用户名默认为 admin）"
    if [ ! -f "$APP_DIR/backend/data/admin_credentials.json" ]; then
        ADMIN_HINT="首次登录默认 admin / admin123456（请登录后立即修改）"
    fi

    log_info "========================================="
    log_info "部署完成"
    log_info "========================================="
    ACCESS_BASE_URL="http://${USER_IP}"
    if [ "$EXTERNAL_PORT" != "80" ]; then
        ACCESS_BASE_URL="http://${USER_IP}:${EXTERNAL_PORT}"
    fi
    echo "前台地址: ${ACCESS_BASE_URL}/"
    echo "后台地址: ${ACCESS_BASE_URL}/admin/index.html"
    echo "管理员登录: $ADMIN_HINT"
    echo "部署目录: $APP_DIR"
    echo "服务名称: $SERVICE_NAME"
    echo "端口映射: 外网 ${EXTERNAL_PORT} -> 内网 ${INTERNAL_PORT}"
    echo "日志查看: journalctl -u ${SERVICE_NAME} -f"
}

cleanup_temp() {
    if [ -n "${RUNTIME_BACKUP_DIR:-}" ] && [ -d "$RUNTIME_BACKUP_DIR" ]; then
        rm -rf "$RUNTIME_BACKUP_DIR"
    fi
}

main() {
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN}   Limit-Up Sniper 商业版一键部署      ${NC}"
    echo -e "${GREEN}=========================================${NC}"

    require_root
    detect_os
    check_source_tree

    log_info "检测到源码目录: $SOURCE_ROOT"

    install_deps
    prepare_backup_dirs
    deploy_code_only
    ensure_runtime_files
    ensure_lhb_data_files
    read_existing_config_values
    prompt_keys_and_merge_config
    configure_ports
    setup_python_venv
    setup_systemd
    setup_nginx
    show_result
    cleanup_temp
}

main "$@"
