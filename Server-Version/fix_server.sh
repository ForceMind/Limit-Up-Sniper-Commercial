#!/usr/bin/env bash

# 快速修复脚本：校正 service worker、重载服务并检查关键接口
# 用法：sudo bash fix_server.sh

set -euo pipefail

APP_NAME="limit-up-sniper-commercial"
SERVICE_NAME="${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_CMD=""

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}$1${NC}"; }
log_warn() { echo -e "${YELLOW}$1${NC}"; }
log_error() { echo -e "${RED}$1${NC}"; }

require_root() {
    if [ "${EUID}" -ne 0 ]; then
        log_error "[错误] 请使用 sudo 或 root 运行。"
        exit 1
    fi
}

select_python_cmd() {
    local candidates=(python3 python)
    local cmd
    for cmd in "${candidates[@]}"; do
        if command -v "$cmd" >/dev/null 2>&1; then
            PYTHON_CMD="$(command -v "$cmd")"
            return
        fi
    done
    log_error "[错误] 未找到 Python 解释器（python3/python）。"
    exit 1
}

resolve_internal_port() {
    local parsed
    parsed=$(grep -Eo -- '--port[[:space:]]+[0-9]+' "$SERVICE_FILE" | head -n 1 | awk '{print $2}') || true
    if [[ "$parsed" =~ ^[0-9]+$ ]]; then
        echo "$parsed"
    else
        echo "8000"
    fi
}

fix_service_workers() {
    log_info "检查并修复 service worker 数..."
    if grep -Eq -- '--workers[[:space:]]+[0-9]+' "$SERVICE_FILE"; then
        sed -i -E 's/--workers[[:space:]]+[0-9]+/--workers 1/g' "$SERVICE_FILE"
    elif grep -Eq 'workers[[:space:]]+[0-9]+' "$SERVICE_FILE"; then
        sed -i -E 's/workers[[:space:]]+[0-9]+/workers 1/g' "$SERVICE_FILE"
    else
        log_warn "未检测到 workers 参数，跳过 worker 修复。"
    fi
}

restart_services() {
    log_info "重载并重启后端服务..."
    systemctl daemon-reload
    systemctl restart "$SERVICE_NAME"
    systemctl is-active --quiet "$SERVICE_NAME"
}

check_nginx_ws_proxy() {
    if ! command -v nginx >/dev/null 2>&1; then
        log_warn "未检测到 nginx，跳过 nginx 检查。"
        return
    fi

    log_info "检查 nginx WebSocket 代理配置..."
    if nginx -T 2>/dev/null | grep -q 'location /ws'; then
        log_info "检测到 /ws 代理配置。"
    else
        log_warn "未检测到 /ws 代理配置，请检查 nginx 站点文件。"
    fi

    nginx -t
    systemctl restart nginx
}

probe_apis() {
    local internal_port
    internal_port=$(resolve_internal_port)

    local status_url="http://127.0.0.1:${internal_port}/api/status"
    local stocks_url="http://127.0.0.1:${internal_port}/api/stocks"
    local probe_device="healthcheck_$(date +%s)"

    log_info "探测接口: ${status_url}"
    curl -fsS "$status_url" >/dev/null

    log_info "探测接口: ${stocks_url}"
    local stocks_tmp
    stocks_tmp="$(mktemp)"
    local stocks_status
    stocks_status=$(curl -sS -H "X-Device-ID: ${probe_device}" --max-time 8 -o "$stocks_tmp" -w "%{http_code}" "$stocks_url" || echo "000")
    if [ "$stocks_status" != "200" ]; then
        rm -f "$stocks_tmp"
        log_error "[错误] /api/stocks 探测失败，HTTP ${stocks_status}"
        exit 1
    fi

    if ! "$PYTHON_CMD" - "$stocks_tmp" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    payload = json.load(f)
if not isinstance(payload, list):
    raise SystemExit(1)
PY
    then
        rm -f "$stocks_tmp"
        log_error "[错误] /api/stocks 返回格式异常（应为数组）"
        exit 1
    fi
    rm -f "$stocks_tmp"
}

main() {
    require_root
    select_python_cmd
    if [ ! -f "$SERVICE_FILE" ]; then
        log_error "[错误] 未找到服务文件: $SERVICE_FILE"
        exit 1
    fi

    fix_service_workers
    restart_services
    check_nginx_ws_proxy
    probe_apis

    log_info "修复完成。"
    systemctl status "$SERVICE_NAME" --no-pager | head -n 8 || true
}

main "$@"
