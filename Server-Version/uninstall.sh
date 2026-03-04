#!/usr/bin/env bash

# 涨停狙击手商业版卸载脚本
# 默认行为：仅卸载 systemd 服务和 Nginx 站点配置，保留应用目录与数据
# 用法:
#   sudo ./uninstall.sh
#   sudo ./uninstall.sh --remove-app

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

SERVICE_NAMES=("limit-up-sniper-commercial" "limit-up-sniper")
APP_DIRS=("/opt/limit-up-sniper-commercial" "/opt/limit-up-sniper")
NGINX_KEYS=("limit-up-sniper-commercial" "limit-up-sniper")

REMOVE_APP="false"

log_info() { echo -e "${GREEN}$1${NC}"; }
log_warn() { echo -e "${YELLOW}$1${NC}"; }
log_error() { echo -e "${RED}$1${NC}"; }

require_root() {
    if [ "${EUID}" -ne 0 ]; then
        log_error "[错误] 请使用 sudo 或 root 权限运行卸载脚本"
        exit 1
    fi
}

print_usage() {
    cat <<'EOF'
用法:
  sudo ./uninstall.sh [选项]

选项:
  --remove-app   同时删除 /opt 下应用目录（会删除运行数据）
  -h, --help     显示帮助
EOF
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --remove-app)
                REMOVE_APP="true"
                ;;
            -h|--help)
                print_usage
                exit 0
                ;;
            *)
                log_error "[错误] 未知参数: $1"
                print_usage
                exit 1
                ;;
        esac
        shift
    done
}

stop_and_remove_systemd_services() {
    log_warn "[1/4] 卸载 systemd 服务..."

    for service_name in "${SERVICE_NAMES[@]}"; do
        service_file="/etc/systemd/system/${service_name}.service"

        if systemctl list-unit-files | grep -q "^${service_name}\.service"; then
            systemctl stop "$service_name" || true
            systemctl disable "$service_name" || true
            log_info "已停止并禁用服务: ${service_name}"
        fi

        if [ -f "$service_file" ]; then
            rm -f "$service_file"
            log_info "已删除服务文件: ${service_file}"
        fi
    done

    systemctl daemon-reload
    systemctl reset-failed || true
}

remove_nginx_config() {
    log_warn "[2/4] 移除 Nginx 站点配置..."

    for key in "${NGINX_KEYS[@]}"; do
        rm -f "/etc/nginx/sites-enabled/${key}" || true
        rm -f "/etc/nginx/sites-available/${key}" || true
        rm -f "/etc/nginx/conf.d/${key}.conf" || true
    done

    if command -v nginx >/dev/null 2>&1; then
        if nginx -t >/dev/null 2>&1; then
            systemctl restart nginx || true
            log_info "Nginx 配置已刷新"
        else
            log_warn "Nginx 配置检查失败，请手动执行: nginx -t"
        fi
    else
        log_warn "未检测到 nginx 命令，已跳过 Nginx 重载"
    fi
}

remove_app_dirs_if_needed() {
    log_warn "[3/4] 处理应用目录..."

    if [ "$REMOVE_APP" != "true" ]; then
        log_info "默认保留应用目录与数据（如需彻底删除请加 --remove-app）"
        return
    fi

    log_warn "即将删除以下目录（包含运行数据）："
    for app_dir in "${APP_DIRS[@]}"; do
        if [ -d "$app_dir" ]; then
            echo "  - $app_dir"
        fi
    done

    read -r -p "确认删除上述目录？输入 YES 继续: " confirm
    if [ "$confirm" != "YES" ]; then
        log_warn "已取消删除应用目录"
        return
    fi

    for app_dir in "${APP_DIRS[@]}"; do
        if [ -d "$app_dir" ]; then
            rm -rf "$app_dir"
            log_info "已删除目录: $app_dir"
        fi
    done
}

show_result() {
    log_warn "[4/4] 卸载完成"
    rm -f /usr/local/bin/zt || true
    log_info "========================================="
    log_info "服务卸载已完成"
    if [ "$REMOVE_APP" = "true" ]; then
        log_info "应用目录: 已删除"
    else
        log_info "应用目录: 已保留"
    fi
    log_info "如需重新安装，可再次执行: sudo ./Server-Version/install.sh"
    log_info "========================================="
}

main() {
    parse_args "$@"
    require_root
    stop_and_remove_systemd_services
    remove_nginx_config
    remove_app_dirs_if_needed
    show_result
}

main "$@"
