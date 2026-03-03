#!/usr/bin/env bash

# 涨停狙击手商业版通用更新脚本
# 用法: sudo ./update.sh [源码目录]

set -euo pipefail

APP_NAME="limit-up-sniper-commercial"
APP_DIR="/opt/${APP_NAME}"
SERVICE_NAME="${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LEGACY_APP_DIR="/opt/limit-up-sniper"
LEGACY_SERVICE_NAME="limit-up-sniper"
WORKER_COUNT="2"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DEFAULT_SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE_ROOT_INPUT="${1:-}"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}$1${NC}"; }
log_warn() { echo -e "${YELLOW}$1${NC}"; }
log_error() { echo -e "${RED}$1${NC}"; }

resolve_install_target() {
    if [ ! -d "$APP_DIR" ] && [ -d "$LEGACY_APP_DIR" ]; then
        APP_DIR="$LEGACY_APP_DIR"
        SERVICE_NAME="$LEGACY_SERVICE_NAME"
        SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    fi
}

calc_worker_count() {
    local cpu_count="2"
    if command -v nproc >/dev/null 2>&1; then
        cpu_count="$(nproc 2>/dev/null || echo 2)"
    fi
    if ! [[ "$cpu_count" =~ ^[0-9]+$ ]] || [ "$cpu_count" -lt 1 ]; then
        cpu_count="2"
    fi

    if [ "$cpu_count" -le 2 ]; then
        WORKER_COUNT="2"
    elif [ "$cpu_count" -le 4 ]; then
        WORKER_COUNT="3"
    else
        WORKER_COUNT="4"
    fi
}

require_root() {
    if [ "${EUID}" -ne 0 ]; then
        log_error "[错误] 请使用 sudo 或 root 权限运行更新脚本"
        exit 1
    fi
}

SKIP_COPY=false
SOURCE_ROOT=""
GIT_PULL_DIR=""
HAD_RUNTIME_DATA=false

resolve_source() {
    if [[ "$SCRIPT_DIR/.." -ef "$APP_DIR" ]]; then
        SOURCE_ROOT="$APP_DIR"
        SKIP_COPY=true
    else
        SOURCE_ROOT="${SOURCE_ROOT_INPUT:-$DEFAULT_SOURCE_ROOT}"
    fi

    if [ ! -d "$SOURCE_ROOT/backend" ] || [ ! -d "$SOURCE_ROOT/frontend" ]; then
        log_error "[错误] 源码目录无效: $SOURCE_ROOT"
        log_error "必须包含 backend/ 与 frontend/"
        exit 1
    fi

    if [ -d "$SOURCE_ROOT/.git" ]; then
        GIT_PULL_DIR="$SOURCE_ROOT"
    fi
}

prepare_backup_dir() {
    BACKUP_ROOT="$APP_DIR/backups"
    mkdir -p "$BACKUP_ROOT"
    BACKUP_DIR="$BACKUP_ROOT/update_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$BACKUP_DIR/backend" "$BACKUP_DIR/frontend"
}

backup_runtime_files() {
    log_warn "[1/5] 备份运行时数据与配置..."

    if [ -d "$APP_DIR/backend/data" ]; then
        HAD_RUNTIME_DATA=true
        cp -a "$APP_DIR/backend/data" "$BACKUP_DIR/backend/"
        echo "已备份 backend/data"
    else
        echo "跳过: 未找到 backend/data"
    fi

    if [ -f "$APP_DIR/backend/.env" ]; then
        cp -a "$APP_DIR/backend/.env" "$BACKUP_DIR/backend/.env"
        echo "已备份 backend/.env"
    else
        echo "跳过: 未找到 backend/.env"
    fi

    if [ -f "$APP_DIR/frontend/config.js" ]; then
        cp -a "$APP_DIR/frontend/config.js" "$BACKUP_DIR/frontend/config.js"
        echo "已备份 frontend/config.js"
    else
        echo "跳过: 未找到 frontend/config.js"
    fi
}

pull_latest_if_needed() {
    if [ -n "$GIT_PULL_DIR" ]; then
        log_warn "[2/5] 拉取最新代码..."
        if git -C "$GIT_PULL_DIR" pull --ff-only; then
            echo "Git 拉取成功"
        else
            log_warn "[警告] git pull --ff-only 失败，继续使用当前代码"
        fi
    else
        log_warn "[2/5] 未检测到 Git 仓库，跳过 git pull"
    fi
}

deploy_files() {
    if [ "$SKIP_COPY" = true ]; then
        log_warn "[3/5] 自更新模式，跳过文件复制"
        return
    fi

    log_warn "[3/5] 部署后端与前端代码（不带运行时 data）..."
    mkdir -p "$APP_DIR"
    rm -rf "$APP_DIR/backend" "$APP_DIR/frontend"
    cp -a "$SOURCE_ROOT/backend" "$APP_DIR/"
    cp -a "$SOURCE_ROOT/frontend" "$APP_DIR/"

    # 防止仓库内 data 文件覆盖线上运行数据
    rm -rf "$APP_DIR/backend/data"
    mkdir -p "$APP_DIR/backend/data"

    mkdir -p "$APP_DIR/scripts"
    if compgen -G "$SOURCE_ROOT/Server-Version/*.sh" > /dev/null; then
        cp -a "$SOURCE_ROOT/Server-Version/"*.sh "$APP_DIR/scripts/"
        chmod +x "$APP_DIR/scripts/"*.sh || true
    fi

    chmod -R 755 "$APP_DIR/frontend" || true
}

restore_runtime_files() {
    log_warn "[4/5] 恢复运行时数据与配置..."
    mkdir -p "$APP_DIR/backend" "$APP_DIR/frontend"

    if [ -d "$BACKUP_DIR/backend/data" ]; then
        mkdir -p "$APP_DIR/backend/data"
        cp -a "$BACKUP_DIR/backend/data/." "$APP_DIR/backend/data/"
        echo "已恢复 backend/data"
    elif [ "$HAD_RUNTIME_DATA" = true ]; then
        log_error "[错误] 更新前存在运行数据，但备份缺失。为避免数据丢失，已终止更新。"
        exit 1
    else
        mkdir -p "$APP_DIR/backend/data"
        echo "未找到历史数据备份，已创建空目录 backend/data"
    fi

    if [ -f "$BACKUP_DIR/backend/.env" ]; then
        cp -a "$BACKUP_DIR/backend/.env" "$APP_DIR/backend/.env"
        echo "已恢复 backend/.env"
    fi

    if [ -f "$BACKUP_DIR/frontend/config.js" ]; then
        cp -a "$BACKUP_DIR/frontend/config.js" "$APP_DIR/frontend/config.js"
        echo "已恢复 frontend/config.js"
    fi
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

fix_runtime_permissions() {
    mkdir -p "$APP_DIR/backend/data"
    chmod -R 777 "$APP_DIR/backend/data" || true
    touch "$APP_DIR/backend/app.log"
    chmod 666 "$APP_DIR/backend/app.log" || true
}

install_dependencies() {
    log_warn "[5/5] 安装/更新 Python 依赖..."
    if [ -f "$APP_DIR/venv/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$APP_DIR/venv/bin/activate"
        pip install -r "$APP_DIR/backend/requirements.txt" --no-cache-dir
    else
        echo "跳过: 未找到虚拟环境 $APP_DIR/venv"
    fi
}

tune_systemd_service() {
    if [ ! -f "$SERVICE_FILE" ]; then
        log_warn "未找到 systemd 服务文件，跳过性能参数更新"
        return
    fi

    local internal_port
    internal_port=$(grep -Eo -- '--port[[:space:]]+[0-9]+' "$SERVICE_FILE" | head -n 1 | awk '{print $2}') || true
    if ! [[ "$internal_port" =~ ^[0-9]+$ ]]; then
        internal_port="8000"
    fi

    local deepseek_key
    deepseek_key=$(grep -E '^Environment="DEEPSEEK_API_KEY=' "$SERVICE_FILE" | head -n 1 | sed -E 's/^Environment="DEEPSEEK_API_KEY=([^"]*)"/\1/') || true

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Limit-Up Sniper Commercial Backend
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=$APP_DIR/backend
Environment="PATH=$APP_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="DEEPSEEK_API_KEY=$deepseek_key"
Environment="ENABLE_BACKGROUND_TASKS=1"
Environment="BACKGROUND_SINGLETON_PORT=39731"
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $internal_port --workers $WORKER_COUNT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
}

main() {
    require_root
    resolve_install_target
    calc_worker_count
    resolve_source
    prepare_backup_dir

    log_info "=== 涨停狙击手商业版：通用更新脚本 ==="
    echo "安装目录: $APP_DIR"
    echo "源码目录: $SOURCE_ROOT"

    log_warn "停止服务..."
    systemctl stop limit-up-sniper || true

    backup_runtime_files
    pull_latest_if_needed
    deploy_files
    restore_runtime_files
    ensure_lhb_data_files
    fix_runtime_permissions
    install_dependencies
    tune_systemd_service

    echo "重启服务..."
    systemctl restart "$SERVICE_NAME"
    systemctl restart nginx || true

    log_info "========================================="
    log_info "更新完成，运行数据与配置已恢复"
    log_info "========================================="
    systemctl status "$SERVICE_NAME" --no-pager | head -n 8 || true
}

main "$@"
