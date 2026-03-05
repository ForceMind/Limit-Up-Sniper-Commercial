#!/usr/bin/env bash

set -euo pipefail

APP_NAME="limit-up-sniper-commercial"
APP_DIR="/opt/${APP_NAME}"
SERVICE_NAME="${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
BACKEND_DATA_DIR="${APP_DIR}/backend/data"
ADMIN_PANEL_PATH_FILE="${BACKEND_DATA_DIR}/admin_panel_path.json"
ADMIN_API_PREFIX_FILE="${BACKEND_DATA_DIR}/admin_api_prefix.json"
DEFAULT_NGINX_CONF_1="/etc/nginx/sites-available/${APP_NAME}"
DEFAULT_NGINX_CONF_2="/etc/nginx/conf.d/${APP_NAME}.conf"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}$1${NC}"; }
log_warn() { echo -e "${YELLOW}$1${NC}"; }
log_error() { echo -e "${RED}$1${NC}"; }

pause_enter() {
    read -r -p "按回车继续..." _
}

run_privileged() {
    if [ "${EUID}" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

detect_nginx_conf() {
    if [ -f "$DEFAULT_NGINX_CONF_1" ]; then
        echo "$DEFAULT_NGINX_CONF_1"
        return
    fi
    if [ -f "$DEFAULT_NGINX_CONF_2" ]; then
        echo "$DEFAULT_NGINX_CONF_2"
        return
    fi
    echo ""
}

backend_state() {
    systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo "inactive"
}

nginx_state() {
    systemctl is-active nginx 2>/dev/null || echo "inactive"
}

public_frontend_state() {
    if [ ! -f "$SERVICE_FILE" ]; then
        echo "未知"
        return
    fi
    local v
    v=$(grep -E '^Environment="DISABLE_PUBLIC_FRONTEND=' "$SERVICE_FILE" | head -n 1 | sed -E 's/^Environment="DISABLE_PUBLIC_FRONTEND=([^\"]*)"/\1/') || true
    v="${v:-0}"
    if [[ "$v" == "1" ]] || [[ "$v" == "true" ]] || [[ "$v" == "TRUE" ]]; then
        echo "已关闭(仅用户前端)"
    else
        echo "运行中"
    fi
}

parse_internal_port() {
    if [ ! -f "$SERVICE_FILE" ]; then
        echo "未知"
        return
    fi
    local p
    p=$(grep -Eo -- '--port[[:space:]]+[0-9]+' "$SERVICE_FILE" | head -n 1 | awk '{print $2}') || true
    if [[ "$p" =~ ^[0-9]+$ ]]; then
        echo "$p"
    else
        echo "未知"
    fi
}

parse_worker_count() {
    if [ ! -f "$SERVICE_FILE" ]; then
        echo "未知"
        return
    fi
    local c
    c=$(grep -Eo -- '--workers[[:space:]]+[0-9]+' "$SERVICE_FILE" | head -n 1 | awk '{print $2}') || true
    if [[ "$c" =~ ^[0-9]+$ ]]; then
        echo "$c"
    else
        echo "未知"
    fi
}

parse_external_port() {
    local conf
    conf="$(detect_nginx_conf)"
    if [ -z "$conf" ]; then
        echo "未知"
        return
    fi
    local p
    p=$(grep -E '^[[:space:]]*listen[[:space:]]+[0-9]+' "$conf" | head -n 1 | awk '{print $2}' | tr -d ';') || true
    if [[ "$p" =~ ^[0-9]+$ ]]; then
        echo "$p"
    else
        echo "未知"
    fi
}

current_admin_path() {
    if [ ! -f "$ADMIN_PANEL_PATH_FILE" ]; then
        echo "/admin"
        return
    fi

    python3 - "$ADMIN_PANEL_PATH_FILE" <<'PY' 2>/dev/null || echo "/admin"
import json
import sys
path = "/admin"
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, dict):
        v = str(data.get('path', '/admin') or '/admin').strip()
        if not v.startswith('/'):
            v = '/' + v
        parts = [p for p in v.split('/') if p]
        v = '/' + '/'.join(parts)
        if v and v != '/':
            path = v
except Exception:
    pass
print(path)
PY
}

current_admin_api_prefix() {
    if [ ! -f "$ADMIN_API_PREFIX_FILE" ]; then
        echo "/api/admin"
        return
    fi

    python3 - "$ADMIN_API_PREFIX_FILE" <<'PY' 2>/dev/null || echo "/api/admin"
import json
import sys
prefix = "/api/admin"
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, dict):
        v = str(data.get('prefix', '/api/admin') or '/api/admin').strip()
        if not v.startswith('/'):
            v = '/' + v
        if not v.startswith('/api/'):
            v = '/api' + v
        parts = [p for p in v.split('/') if p]
        v = '/' + '/'.join(parts)
        if v and v != '/api':
            prefix = v
except Exception:
    pass
print(prefix)
PY
}

show_status() {
    clear
    echo -e "${BLUE}==========================================${NC}"
    echo -e "${BLUE}     涨停狙击手运维面板 (zt)${NC}"
    echo -e "${BLUE}==========================================${NC}"
    echo "安装目录       : $APP_DIR"
    echo "后端服务       : $SERVICE_NAME ($(backend_state))"
    echo "Nginx 状态     : $(nginx_state)"
    echo "内网端口       : $(parse_internal_port)"
    echo "外网端口       : $(parse_external_port)"
    echo "后台路径       : $(current_admin_path)"
    echo "后台API前缀    : $(current_admin_api_prefix)"
    echo "Worker 数      : $(parse_worker_count)"
    echo "本站用户前端   : $(public_frontend_state)"
    echo ""
}

backend_start() { run_privileged systemctl start "$SERVICE_NAME"; }
backend_stop() { run_privileged systemctl stop "$SERVICE_NAME"; }
backend_restart() { run_privileged systemctl restart "$SERVICE_NAME"; }

frontend_start() {
    upsert_env_var "DISABLE_PUBLIC_FRONTEND" "0"
    run_privileged systemctl daemon-reload
    run_privileged systemctl restart "$SERVICE_NAME"
}

frontend_stop() {
    upsert_env_var "DISABLE_PUBLIC_FRONTEND" "1"
    run_privileged systemctl daemon-reload
    run_privileged systemctl restart "$SERVICE_NAME"
}

restart_all() {
    run_privileged systemctl restart "$SERVICE_NAME"
    run_privileged systemctl reload nginx || true
}

health_check() {
    local internal_port external_port
    internal_port="$(parse_internal_port)"
    external_port="$(parse_external_port)"
    echo "执行健康检查..."

    if [[ "$internal_port" =~ ^[0-9]+$ ]]; then
        if curl -fsS --max-time 5 "http://127.0.0.1:${internal_port}/api/status" >/dev/null 2>&1; then
            log_info "内部健康检查通过: :${internal_port}/api/status"
        else
            log_error "内部健康检查失败: :${internal_port}/api/status"
        fi
    else
        log_warn "跳过内部健康检查：未解析到内网端口"
    fi

    if [[ "$external_port" =~ ^[0-9]+$ ]]; then
        if curl -fsS --max-time 5 "http://127.0.0.1:${external_port}/api/status" >/dev/null 2>&1; then
            log_info "外部健康检查通过: :${external_port}/api/status"
        else
            log_warn "外部健康检查失败: :${external_port}/api/status"
        fi
    else
        log_warn "跳过外部健康检查：未解析到外网端口"
    fi
}

show_recent_logs() {
    echo "1) 后端最近100行日志"
    echo "2) 后端错误日志(优先级 err+)"
    echo "3) Nginx error.log 最近100行"
    echo "4) 返回"
    read -r -p "请选择: " c
    case "$c" in
        1)
            run_privileged journalctl -u "$SERVICE_NAME" -n 100 --no-pager
            ;;
        2)
            run_privileged journalctl -u "$SERVICE_NAME" -p err -n 100 --no-pager
            ;;
        3)
            if [ -f /var/log/nginx/error.log ]; then
                run_privileged tail -n 100 /var/log/nginx/error.log
            else
                log_warn "未找到 /var/log/nginx/error.log"
            fi
            ;;
        *)
            return
            ;;
    esac
    pause_enter
}

follow_logs() {
    echo "1) 实时跟踪后端日志"
    echo "2) 实时跟踪 Nginx error.log"
    echo "3) 返回"
    read -r -p "请选择: " c
    case "$c" in
        1)
            echo "按 Ctrl+C 退出日志跟踪"
            sleep 1
            run_privileged journalctl -u "$SERVICE_NAME" -f
            ;;
        2)
            if [ -f /var/log/nginx/error.log ]; then
                echo "按 Ctrl+C 退出日志跟踪"
                sleep 1
                run_privileged tail -f /var/log/nginx/error.log
            else
                log_warn "未找到 /var/log/nginx/error.log"
                pause_enter
            fi
            ;;
        *)
            return
            ;;
    esac
}

set_worker_count() {
    read -r -p "请输入 Worker 数 (1-8): " wc
    if ! [[ "$wc" =~ ^[1-8]$ ]]; then
        log_error "输入无效，必须是 1-8 的整数"
        pause_enter
        return
    fi
    if [ ! -f "$SERVICE_FILE" ]; then
        log_error "未找到服务文件: $SERVICE_FILE"
        pause_enter
        return
    fi

    run_privileged sed -i -E "s#(--workers[[:space:]]+)[0-9]+#\\1${wc}#" "$SERVICE_FILE"
    run_privileged systemctl daemon-reload
    run_privileged systemctl restart "$SERVICE_NAME"
    log_info "已设置 Worker 数为 $wc 并重启后端"
    pause_enter
}

upsert_env_var() {
    local key="$1"
    local val="$2"

    if [ ! -f "$SERVICE_FILE" ]; then
        log_error "未找到服务文件: $SERVICE_FILE"
        return 1
    fi

    if grep -q "^Environment=\"${key}=" "$SERVICE_FILE"; then
        run_privileged sed -i -E "s#^Environment=\"${key}=[^\"]*\"#Environment=\"${key}=${val}\"#" "$SERVICE_FILE"
    else
        run_privileged sed -i -E "/^Environment=\"BACKGROUND_SINGLETON_PORT=/a Environment=\"${key}=${val}\"" "$SERVICE_FILE"
    fi
}

set_status_rate_limit() {
    read -r -p "请输入状态接口限流窗口秒数(建议10): " window
    read -r -p "请输入窗口内最大请求数(建议30): " max_req
    if ! [[ "$window" =~ ^[0-9]+$ ]] || ! [[ "$max_req" =~ ^[0-9]+$ ]]; then
        log_error "输入无效，必须是非负整数"
        pause_enter
        return
    fi

    upsert_env_var "STATUS_RATE_LIMIT_WINDOW_SECONDS" "$window"
    upsert_env_var "STATUS_RATE_LIMIT_MAX_REQUESTS" "$max_req"
    run_privileged systemctl daemon-reload
    run_privileged systemctl restart "$SERVICE_NAME"
    log_info "已更新状态接口限流参数并重启后端"
    pause_enter
}

set_admin_path() {
    read -r -p "请输入新的后台路径(示例 /x9_admin): " raw
    local p
    p="${raw//[[:space:]]/}"
    if [ -z "$p" ]; then
        log_error "后台路径不能为空"
        pause_enter
        return
    fi
    if [[ "$p" != /* ]]; then
        p="/$p"
    fi
    if [[ "$p" == "/" ]] || [[ "$p" == /api* ]]; then
        log_error "后台路径不能为 / 且不能以 /api 开头"
        pause_enter
        return
    fi
    if [[ ! "$p" =~ ^/[A-Za-z0-9/_-]+$ ]]; then
        log_error "后台路径只允许字母、数字、/、_、-"
        pause_enter
        return
    fi

    run_privileged mkdir -p "$BACKEND_DATA_DIR"
    run_privileged bash -c "cat > '$ADMIN_PANEL_PATH_FILE' <<EOF
{
  \"path\": \"$p\",
  \"updated_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
}
EOF"
    log_info "后台路径已更新为: $p"
    pause_enter
}

set_admin_api_prefix() {
    read -r -p "请输入新的管理员API前缀(示例 /api/x9_admin 或 x9_admin): " raw
    local p
    p="${raw//[[:space:]]/}"
    if [ -z "$p" ]; then
        log_error "管理员API前缀不能为空"
        pause_enter
        return
    fi
    if [[ "$p" != /* ]]; then
        p="/$p"
    fi
    if [[ "$p" != /api/* ]]; then
        p="/api$p"
    fi
    if [[ "$p" == "/api" ]] || [[ "$p" == "/api/" ]]; then
        log_error "管理员API前缀不能为 /api"
        pause_enter
        return
    fi
    if [[ ! "$p" =~ ^/[A-Za-z0-9/_-]+$ ]]; then
        log_error "管理员API前缀只允许字母、数字、/、_、-"
        pause_enter
        return
    fi
    if [[ "$p" == /api/auth* ]] || [[ "$p" == /api/payment* ]]; then
        log_error "管理员API前缀不能与 /api/auth 或 /api/payment 冲突"
        pause_enter
        return
    fi

    run_privileged mkdir -p "$BACKEND_DATA_DIR"
    run_privileged bash -c "cat > '$ADMIN_API_PREFIX_FILE' <<EOF
{
  \"prefix\": \"$p\",
  \"updated_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
}
EOF"
    log_info "管理员API前缀已更新为: $p"
    log_warn "提示：后台前端会根据当前访问路径自动适配接口前缀。"
    pause_enter
}

toggle_autostart() {
    echo "1) 启用后端开机自启"
    echo "2) 禁用后端开机自启"
    echo "3) 返回"
    read -r -p "请选择: " c
    case "$c" in
        1)
            run_privileged systemctl enable "$SERVICE_NAME"
            log_info "已启用后端开机自启"
            ;;
        2)
            run_privileged systemctl disable "$SERVICE_NAME"
            log_info "已禁用后端开机自启"
            ;;
        *)
            return
            ;;
    esac
    pause_enter
}

params_menu() {
    while true; do
        clear
        echo "参数设置"
        echo "1) 设置 Worker 数"
        echo "2) 设置状态接口限流参数"
        echo "3) 设置后台访问路径"
        echo "4) 设置管理员API前缀"
        echo "5) 设置后端开机自启"
        echo "6) 返回"
        read -r -p "请选择: " c
        case "$c" in
            1) set_worker_count ;;
            2) set_status_rate_limit ;;
            3) set_admin_path ;;
            4) set_admin_api_prefix ;;
            5) toggle_autostart ;;
            6) return ;;
            *)
                log_warn "无效选择"
                sleep 1
                ;;
        esac
    done
}

main_menu() {
    while true; do
        show_status
        echo "1) 启动后端服务"
        echo "2) 重启后端服务"
        echo "3) 停止后端服务"
        echo "4) 启用本站用户前端"
        echo "5) 关闭本站用户前端"
        echo "6) 重启全部(后端+Nginx重载)"
        echo "7) 健康检查"
        echo "8) 查看日志(最近)"
        echo "9) 跟踪日志(实时)"
        echo "10) 参数设置"
        echo "0) 退出"
        read -r -p "请选择: " choice
        case "$choice" in
            1)
                backend_start
                log_info "后端已启动"
                pause_enter
                ;;
            2)
                backend_restart
                log_info "后端已重启"
                pause_enter
                ;;
            3)
                backend_stop
                log_info "后端已停止"
                pause_enter
                ;;
            4)
                frontend_start
                log_info "本站用户前端已启用（不影响其它 Nginx 站点）"
                pause_enter
                ;;
            5)
                frontend_stop
                log_info "本站用户前端已关闭（API/后台仍可用）"
                pause_enter
                ;;
            6)
                restart_all
                log_info "后端已重启，Nginx 已重载"
                pause_enter
                ;;
            7)
                health_check
                pause_enter
                ;;
            8)
                show_recent_logs
                ;;
            9)
                follow_logs
                ;;
            10)
                params_menu
                ;;
            0)
                exit 0
                ;;
            *)
                log_warn "无效选择"
                sleep 1
                ;;
        esac
    done
}

main_menu
