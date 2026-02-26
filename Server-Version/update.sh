#!/usr/bin/env bash
# 通用更新脚本
# 用法: sudo ./update.sh [源码目录]
# 流程: 备份本地数据 -> 拉取代码(可选) -> 部署 -> 恢复本地数据 -> 重启服务

set -euo pipefail

APP_DIR="/opt/limit-up-sniper"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DEFAULT_SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE_ROOT_INPUT="${1:-}"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}$1${NC}"
}

log_warn() {
    echo -e "${YELLOW}$1${NC}"
}

log_error() {
    echo -e "${RED}$1${NC}"
}

SKIP_COPY=false
SOURCE_ROOT=""
GIT_PULL_DIR=""

if [[ "$SCRIPT_DIR/.." -ef "$APP_DIR" ]]; then
    SOURCE_ROOT="$APP_DIR"
    SKIP_COPY=true
else
    SOURCE_ROOT="${SOURCE_ROOT_INPUT:-$DEFAULT_SOURCE_ROOT}"
fi

if [ ! -d "$SOURCE_ROOT/backend" ] || [ ! -d "$SOURCE_ROOT/frontend" ]; then
    log_error "[错误] 源码目录无效: $SOURCE_ROOT"
    log_error "必须包含 backend/ 和 frontend/ 目录"
    exit 1
fi

if [ -d "$SOURCE_ROOT/.git" ]; then
    GIT_PULL_DIR="$SOURCE_ROOT"
fi

BACKUP_DIR="$(mktemp -d /tmp/limit-up-sniper-backup.XXXXXX)"
cleanup() {
    rm -rf "$BACKUP_DIR"
}
trap cleanup EXIT

backup_runtime_files() {
    log_warn "[1/5] 备份本地运行数据和配置..."
    mkdir -p "$BACKUP_DIR/backend" "$BACKUP_DIR/frontend"

    if [ -d "$APP_DIR/backend/data" ]; then
        cp -a "$APP_DIR/backend/data" "$BACKUP_DIR/backend/"
        echo "已备份 backend/data"
    else
        echo "跳过：未找到 backend/data"
    fi

    if [ -f "$APP_DIR/backend/.env" ]; then
        cp -a "$APP_DIR/backend/.env" "$BACKUP_DIR/backend/.env"
        echo "已备份 backend/.env"
    else
        echo "跳过：未找到 backend/.env"
    fi

    if [ -f "$APP_DIR/frontend/config.js" ]; then
        cp -a "$APP_DIR/frontend/config.js" "$BACKUP_DIR/frontend/config.js"
        echo "已备份 frontend/config.js"
    else
        echo "跳过：未找到 frontend/config.js"
    fi
}

pull_latest_if_needed() {
    if [ -n "$GIT_PULL_DIR" ]; then
        log_warn "[2/5] 拉取最新代码..."
        if git -C "$GIT_PULL_DIR" pull --ff-only; then
            echo "Git 拉取成功"
        else
            log_warn "[警告] git pull --ff-only 失败，继续使用当前本地代码快照"
        fi
    else
        log_warn "[2/5] 未检测到 Git 仓库，跳过拉取"
    fi
}

deploy_files() {
    if [ "$SKIP_COPY" = true ]; then
        log_warn "[3/5] 自更新模式，跳过文件复制"
        return
    fi

    log_warn "[3/5] 部署后端和前端文件..."
    mkdir -p "$APP_DIR"
    rm -rf "$APP_DIR/backend" "$APP_DIR/frontend"
    cp -a "$SOURCE_ROOT/backend" "$APP_DIR/"
    cp -a "$SOURCE_ROOT/frontend" "$APP_DIR/"

    mkdir -p "$APP_DIR/scripts"
    if compgen -G "$SOURCE_ROOT/Server-Version/*.sh" > /dev/null; then
        cp -a "$SOURCE_ROOT/Server-Version/"*.sh "$APP_DIR/scripts/"
        chmod +x "$APP_DIR/scripts/"*.sh || true
    fi
    chmod -R 755 "$APP_DIR/frontend" || true
}

restore_runtime_files() {
    log_warn "[4/5] 恢复本地运行数据和配置..."
    mkdir -p "$APP_DIR/backend" "$APP_DIR/frontend"

    if [ -d "$BACKUP_DIR/backend/data" ]; then
        rm -rf "$APP_DIR/backend/data"
        cp -a "$BACKUP_DIR/backend/data" "$APP_DIR/backend/"
        echo "已恢复 backend/data"
    else
        mkdir -p "$APP_DIR/backend/data"
        echo "无备份 data，已创建空目录 backend/data"
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

fix_runtime_permissions() {
    mkdir -p "$APP_DIR/backend/data"
    chmod -R 777 "$APP_DIR/backend/data" || true
    touch "$APP_DIR/backend/app.log"
    chmod 666 "$APP_DIR/backend/app.log" || true
}

install_dependencies() {
    log_warn "[5/5] 更新 Python 依赖..."
    if [ -f "$APP_DIR/venv/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$APP_DIR/venv/bin/activate"
        pip install -r "$APP_DIR/backend/requirements.txt" --no-cache-dir -q
    else
        echo "跳过：未检测到 venv"
    fi
}

main() {
    log_info "=== Limit-Up Sniper 商业版：通用更新脚本 ==="
    echo "安装目录: $APP_DIR"
    echo "源码目录: $SOURCE_ROOT"

    backup_runtime_files

    log_warn "停止服务..."
    systemctl stop limit-up-sniper || true

    pull_latest_if_needed
    deploy_files
    restore_runtime_files
    fix_runtime_permissions
    install_dependencies

    echo "重启服务..."
    systemctl restart limit-up-sniper
    systemctl restart nginx || true

    log_info "========================================="
    log_info "更新完成（本地 data/.env/config.js 已保留）"
    log_info "========================================="
    systemctl status limit-up-sniper --no-pager | head -n 5 || true
}

main "$@"
