#!/usr/bin/env bash

# 涨停狙击手商业版通用更新脚本
# 用法: sudo ./update.sh [源码目录]

set -euo pipefail

APP_NAME="limit-up-sniper-commercial"
APP_DIR="/opt/${APP_NAME}"
SERVICE_NAME="${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
WORKER_COUNT="1"
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

resolve_install_target() {
    :
}

validate_existing_install() {
    if [ ! -d "$APP_DIR" ]; then
        log_error "[错误] 未检测到安装目录: $APP_DIR"
        log_error "请先执行安装脚本: sudo bash Server-Version/install.sh"
        exit 1
    fi

    if [ ! -f "$SERVICE_FILE" ]; then
        log_error "[错误] 未检测到 systemd 服务文件: $SERVICE_FILE"
        log_error "请先执行安装脚本重新创建服务: sudo bash Server-Version/install.sh"
        exit 1
    fi

    if [ ! -f "$APP_DIR/venv/bin/activate" ]; then
        log_error "[错误] 未检测到 Python 虚拟环境: $APP_DIR/venv"
        log_error "请先执行安装脚本修复运行环境: sudo bash Server-Version/install.sh"
        exit 1
    fi
}

calc_worker_count() {
    # 当前后端核心行情缓存是进程内内存结构；多 worker 会导致部分 worker 缓存未刷新。
    # 热修策略：更新脚本固定单 worker，保证 /api/stocks 与后台任务缓存一致。
    WORKER_COUNT="1"
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

is_valid_source_root() {
    local root="$1"
    [ -n "${root:-}" ] || return 1
    [ -d "$root/backend" ] || return 1
    [ -d "$root/frontend" ] || return 1
    return 0
}

is_git_source_root() {
    local root="$1"
    is_valid_source_root "$root" || return 1
    [ -d "$root/.git" ] || return 1
    return 0
}

discover_external_source_root() {
    local item=""
    local source_file="$APP_DIR/.source_root"

    if [ -n "${ZT_SOURCE_ROOT:-}" ] && is_git_source_root "$ZT_SOURCE_ROOT"; then
        echo "$ZT_SOURCE_ROOT"
        return
    fi

    if [ -f "$source_file" ]; then
        item="$(head -n 1 "$source_file" 2>/dev/null | tr -d '\r')"
        if is_git_source_root "$item"; then
            echo "$item"
            return
        fi
    fi

    for item in \
        "/root/Limit-Up-Sniper-Commercial" \
        "/root/limit-up-sniper-commercial" \
        "/root/Privy/Limit-Up-Sniper-Commercial" \
        "/root/Privy/limit-up-sniper-commercial"
    do
        if is_git_source_root "$item"; then
            echo "$item"
            return
        fi
    done

    for item in /root/* /root/*/* /home/*/*; do
        [ -d "$item" ] || continue
        if is_git_source_root "$item"; then
            echo "$item"
            return
        fi
    done

    echo ""
}

resolve_source() {
    local auto_root=""
    if [ -n "$SOURCE_ROOT_INPUT" ]; then
        SOURCE_ROOT="$SOURCE_ROOT_INPUT"
        SKIP_COPY=false
    elif [[ "$SCRIPT_DIR/.." -ef "$APP_DIR" ]]; then
        auto_root="$(discover_external_source_root)"
        if [ -n "$auto_root" ]; then
            SOURCE_ROOT="$auto_root"
            SKIP_COPY=false
            log_info "检测到外部源码仓库: $SOURCE_ROOT"
        else
            SOURCE_ROOT="$APP_DIR"
            SKIP_COPY=true
            log_warn "未检测到外部源码仓库，使用安装目录自更新（跳过 git pull/文件复制）"
        fi
    else
        SOURCE_ROOT="$DEFAULT_SOURCE_ROOT"
        SKIP_COPY=false
    fi

    if ! is_valid_source_root "$SOURCE_ROOT"; then
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
    local disable_public_frontend
    disable_public_frontend=$(grep -E '^Environment="DISABLE_PUBLIC_FRONTEND=' "$SERVICE_FILE" | head -n 1 | sed -E 's/^Environment="DISABLE_PUBLIC_FRONTEND=([^"]*)"/\1/') || true
    local auth_api_prefix
    auth_api_prefix=$(grep -E '^Environment="AUTH_API_PREFIX=' "$SERVICE_FILE" | head -n 1 | sed -E 's/^Environment="AUTH_API_PREFIX=([^"]*)"/\1/') || true
    local status_rate_window
    status_rate_window=$(grep -E '^Environment="STATUS_RATE_LIMIT_WINDOW_SECONDS=' "$SERVICE_FILE" | head -n 1 | sed -E 's/^Environment="STATUS_RATE_LIMIT_WINDOW_SECONDS=([^"]*)"/\1/') || true
    local status_rate_max
    status_rate_max=$(grep -E '^Environment="STATUS_RATE_LIMIT_MAX_REQUESTS=' "$SERVICE_FILE" | head -n 1 | sed -E 's/^Environment="STATUS_RATE_LIMIT_MAX_REQUESTS=([^"]*)"/\1/') || true

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

    if [ -n "$disable_public_frontend" ]; then
        sed -i -E '/^Environment="BACKGROUND_SINGLETON_PORT=/a Environment="DISABLE_PUBLIC_FRONTEND='"$disable_public_frontend"'"' "$SERVICE_FILE"
    fi
    if [ -n "$auth_api_prefix" ]; then
        sed -i -E '/^Environment="BACKGROUND_SINGLETON_PORT=/a Environment="AUTH_API_PREFIX='"$auth_api_prefix"'"' "$SERVICE_FILE"
    fi
    if [ -n "$status_rate_window" ]; then
        sed -i -E '/^Environment="BACKGROUND_SINGLETON_PORT=/a Environment="STATUS_RATE_LIMIT_WINDOW_SECONDS='"$status_rate_window"'"' "$SERVICE_FILE"
    fi
    if [ -n "$status_rate_max" ]; then
        sed -i -E '/^Environment="BACKGROUND_SINGLETON_PORT=/a Environment="STATUS_RATE_LIMIT_MAX_REQUESTS='"$status_rate_max"'"' "$SERVICE_FILE"
    fi

    systemctl daemon-reload
}

install_zt_launcher() {
    local launcher="/usr/local/bin/zt"
    cat > "$launcher" <<EOF
#!/usr/bin/env bash
set -euo pipefail

PANEL_SCRIPT="$APP_DIR/scripts/zt.sh"

if [ ! -f "\$PANEL_SCRIPT" ]; then
    echo "[错误] 未找到运维面板脚本: \$PANEL_SCRIPT"
    echo "请先执行安装或更新脚本同步脚本文件。"
    exit 1
fi

if [ "\${EUID}" -eq 0 ]; then
    exec "\$PANEL_SCRIPT" "\$@"
fi

if command -v sudo >/dev/null 2>&1; then
    exec sudo "\$PANEL_SCRIPT" "\$@"
fi

echo "[错误] 当前非 root 用户且未检测到 sudo，无法执行 zt 管理面板"
exit 1
EOF
    chmod +x "$launcher"
    log_info "已更新终端运维命令: zt"
}

verify_update_health() {
    log_warn "健康检查：校验更新后服务与接口..."

    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        log_error "[错误] 服务未启动成功: $SERVICE_NAME"
        log_error "请执行: sudo journalctl -u ${SERVICE_NAME} -n 120 --no-pager"
        log_error "若需回滚，可使用备份目录: $BACKUP_DIR"
        exit 1
    fi

    local internal_port
    internal_port=$(grep -Eo -- '--port[[:space:]]+[0-9]+' "$SERVICE_FILE" | head -n 1 | awk '{print $2}') || true
    if ! [[ "$internal_port" =~ ^[0-9]+$ ]]; then
        internal_port="8000"
    fi

    local internal_health_url="http://127.0.0.1:${internal_port}/api/status"
    local ok="false"
    local i
    local status_code
    local max_attempts="${ZT_HEALTHCHECK_MAX_ATTEMPTS:-90}"
    local sleep_sec="${ZT_HEALTHCHECK_SLEEP_SEC:-1}"
    for i in $(seq 1 "$max_attempts"); do
        status_code="$(curl -sS -o /dev/null -w "%{http_code}" --max-time 3 "$internal_health_url" || echo "000")"
        # 200: ready; 429: /api/status hit rate-limit but service is alive.
        if [ "$status_code" = "200" ] || [ "$status_code" = "429" ]; then
            ok="true"
            break
        fi
        sleep "$sleep_sec"
    done

    if [ "$ok" != "true" ]; then
        log_error "[错误] 健康检查失败: $internal_health_url (last_http=${status_code:-000}, attempts=$max_attempts)"
        log_error "请执行: sudo journalctl -u ${SERVICE_NAME} -n 120 --no-pager"
        log_error "若需回滚，可使用备份目录: $BACKUP_DIR"
        exit 1
    fi

    local admin_api_prefix_file="$APP_DIR/backend/data/admin_api_prefix.json"
    local admin_api_prefix="/api/admin"
    if [ -f "$admin_api_prefix_file" ]; then
        admin_api_prefix="$($APP_DIR/venv/bin/python - "$admin_api_prefix_file" <<'PY'
import json
import re
import sys

path = sys.argv[1]
value = "/api/admin"
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        raw = str(data.get("prefix", "/api/admin") or "").strip()
        if raw:
            if not raw.startswith("/"):
                raw = "/" + raw
            if not raw.startswith("/api/"):
                raw = "/api" + raw
            parts = [p for p in raw.split("/") if p]
            candidate = "/" + "/".join(parts)
            if candidate not in {"/", "/api"} and re.fullmatch(r"/[A-Za-z0-9/_-]+", candidate):
                value = candidate
except Exception:
    pass
print(value)
PY
)"
    fi

    local admin_probe_url="http://127.0.0.1:${internal_port}${admin_api_prefix}/panel_path"
    local admin_probe_status
    admin_probe_status=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "$admin_probe_url" || echo "000")
    if [ "$admin_probe_status" = "404" ] || [ "$admin_probe_status" = "000" ]; then
        log_error "[错误] 管理员API健康检查失败: $admin_probe_url (HTTP $admin_probe_status)"
        log_error "请确认 backend/data/admin_api_prefix.json 与当前后端代码一致"
        log_error "请执行: sudo journalctl -u ${SERVICE_NAME} -n 120 --no-pager"
        log_error "若需回滚，可使用备份目录: $BACKUP_DIR"
        exit 1
    fi

    log_info "管理员API探测通过: ${admin_api_prefix}/panel_path (HTTP $admin_probe_status)"

    local probe_device
    probe_device="healthcheck_probe_device"
    local db_path="$APP_DIR/backend/data/commercial.db"
    if ! "$APP_DIR/venv/bin/python" - "$db_path" "$probe_device" <<'PY'
import datetime
import os
import sqlite3
import sys

db_path = sys.argv[1]
device_id = sys.argv[2]
if not os.path.exists(db_path):
    raise SystemExit(1)

now = datetime.datetime.utcnow()
expires = now + datetime.timedelta(days=3650)
fmt = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S.%f")

with sqlite3.connect(db_path) as conn:
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE device_id = ?", (device_id,))
    row = cur.fetchone()
    if row:
        cur.execute(
            """
            UPDATE users
            SET version = ?, expires_at = ?, last_reset_date = ?,
                daily_ai_count = 0, daily_raid_count = 0, daily_review_count = 0
            WHERE id = ?
            """,
            ("flagship", fmt(expires), fmt(now), row[0]),
        )
    else:
        cur.execute(
            """
            INSERT INTO users
            (device_id, version, expires_at, created_at, last_reset_date, daily_ai_count, daily_raid_count, daily_review_count)
            VALUES (?, ?, ?, ?, ?, 0, 0, 0)
            """,
            (device_id, "flagship", fmt(expires), fmt(now), fmt(now)),
        )
    conn.commit()
PY
    then
        log_error "[错误] 无法写入健康检查探活账号: $db_path"
        log_error "请执行: sudo journalctl -u ${SERVICE_NAME} -n 120 --no-pager"
        exit 1
    fi
    local stocks_url="http://127.0.0.1:${internal_port}/api/stocks"
    local stocks_tmp
    stocks_tmp="$(mktemp)"
    local stocks_status
    stocks_status=$(curl -sS -H "X-Device-ID: ${probe_device}" --max-time 8 -o "$stocks_tmp" -w "%{http_code}" "$stocks_url" || echo "000")
    if [ "$stocks_status" != "200" ]; then
        log_error "[错误] 行情接口健康检查失败: $stocks_url (HTTP $stocks_status)"
        log_error "请执行: sudo journalctl -u ${SERVICE_NAME} -n 120 --no-pager"
        rm -f "$stocks_tmp"
        exit 1
    fi

    if ! "$APP_DIR/venv/bin/python" - "$stocks_tmp" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    payload = json.load(f)
if not isinstance(payload, list):
    raise SystemExit(1)
PY
    then
        log_error "[错误] 行情接口返回格式异常（应为数组）: $stocks_url"
        log_error "请执行: sudo journalctl -u ${SERVICE_NAME} -n 120 --no-pager"
        rm -f "$stocks_tmp"
        exit 1
    fi
    rm -f "$stocks_tmp"

    log_info "行情接口探测通过: /api/stocks (HTTP $stocks_status)"

    log_info "更新后健康检查通过"
}

main() {
    require_root
    resolve_install_target
    validate_existing_install
    calc_worker_count
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
    ensure_lhb_data_files
    fix_runtime_permissions
    install_dependencies
    tune_systemd_service
    install_zt_launcher

    echo "重启服务..."
    systemctl restart "$SERVICE_NAME"
    systemctl restart nginx || true
    verify_update_health

    log_info "========================================="
    log_info "更新完成，运行数据与配置已恢复"
    log_info "========================================="
    systemctl status "$SERVICE_NAME" --no-pager | head -n 8 || true
}

main "$@"
