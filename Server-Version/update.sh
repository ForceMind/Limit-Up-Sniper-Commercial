#!/usr/bin/env bash

# 涨停狙击手商业版通用更新脚本
# 用法: sudo ./update.sh [源码目录]

set -euo pipefail

APP_NAME="limit-up-sniper-commercial"
APP_DIR="/opt/${APP_NAME}"
SERVICE_NAME="$APP_NAME"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DEFAULT_SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE_ROOT_INPUT="${1:-}"
PYTHON_CMD=""

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}$1${NC}"; }
log_warn() { echo -e "${YELLOW}$1${NC}"; }
log_error() { echo -e "${RED}$1${NC}"; }

is_python_compatible() {
    local cmd="$1"
    "$cmd" - <<'PY' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY
}

select_python_cmd() {
    local candidates=(python3.12 python3.11 python3.10 python3.9 python3.8 python3 python)
    local cmd
    for cmd in "${candidates[@]}"; do
        if command -v "$cmd" >/dev/null 2>&1 && is_python_compatible "$cmd"; then
            PYTHON_CMD="$(command -v "$cmd")"
            break
        fi
    done

    if [ -z "$PYTHON_CMD" ]; then
        log_error "[错误] 未找到 Python 3.8+ 解释器，请先安装后再执行更新。"
        exit 1
    fi

    log_info "使用 Python 解释器: $PYTHON_CMD"
}

ensure_venv() {
    local need_recreate=false

    if [ ! -x "$APP_DIR/venv/bin/python" ]; then
        need_recreate=true
    else
        if ! "$APP_DIR/venv/bin/python" - <<'PY' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY
        then
            need_recreate=true
        fi
    fi

    if [ "$need_recreate" = true ]; then
        log_warn "检测到缺失或旧版虚拟环境（<3.8），正在重建..."
        rm -rf "$APP_DIR/venv"
        "$PYTHON_CMD" -m venv "$APP_DIR/venv"
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

fix_runtime_permissions() {
    mkdir -p "$APP_DIR/backend/data"
    chmod -R 777 "$APP_DIR/backend/data" || true
    touch "$APP_DIR/backend/app.log"
    chmod 666 "$APP_DIR/backend/app.log" || true
}

install_dependencies() {
    log_warn "[5/5] 安装/更新 Python 依赖..."
    ensure_venv
    "$APP_DIR/venv/bin/python" -m pip install --upgrade pip -q
    if ! "$APP_DIR/venv/bin/python" -m pip install -r "$APP_DIR/backend/requirements.txt" --no-cache-dir -i https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org; then
        log_warn "官方 PyPI 安装失败，尝试清华镜像..."
        if ! "$APP_DIR/venv/bin/python" -m pip install -r "$APP_DIR/backend/requirements.txt" --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn; then
            log_warn "清华镜像安装失败，尝试阿里云镜像..."
            "$APP_DIR/venv/bin/python" -m pip install -r "$APP_DIR/backend/requirements.txt" --no-cache-dir -i http://mirrors.cloud.aliyuncs.com/pypi/simple/ --trusted-host mirrors.cloud.aliyuncs.com
        fi
    fi
}

main() {
    require_root
    resolve_source
    select_python_cmd
    prepare_backup_dir

    log_info "=== 涨停狙击手商业版：通用更新脚本 ==="
    echo "安装目录: $APP_DIR"
    echo "源码目录: $SOURCE_ROOT"

    log_warn "停止服务..."
    systemctl stop "$SERVICE_NAME" || true

    backup_runtime_files
    pull_latest_if_needed
    deploy_files
    restore_runtime_files
    fix_runtime_permissions
    install_dependencies

    echo "重启服务..."
    systemctl restart "$SERVICE_NAME"
    systemctl restart nginx || true

    log_info "========================================="
    log_info "更新完成，运行数据与配置已恢复"
    log_info "========================================="
    systemctl status "$SERVICE_NAME" --no-pager | head -n 8 || true
}

main "$@"
