from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, BackgroundTasks, Query, Depends, HTTPException, Response, Header
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db.database import get_db
import requests
import json
import hashlib
import hmac
import base64
import os
import shutil
import asyncio
import time
import copy
import secrets
import threading
import socket
import re
import ipaddress
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from starlette.routing import Match
from app.core.news_analyzer import generate_watchlist, analyze_single_stock, analyze_daily_lhb
from app.core.market_scanner import scan_limit_up_pool, scan_broken_limit_pool, get_market_overview
from app.core.stock_utils import calculate_metrics, is_trading_time, is_market_open_day
from app.core.data_provider import data_provider
from app.core.lhb_manager import lhb_manager, KLINE_DIR
from app.core.ai_cache import ai_cache
from app.core.config_manager import SYSTEM_CONFIG, save_config, DEFAULT_SCHEDULE
from app.core.ws_hub import ws_hub
from app.core.runtime_logs import add_runtime_log, get_runtime_logs
from app.core.operation_log import log_user_operation
from app.api import auth, admin, payment
from app.db import database, models
from app.dependencies import get_current_user, check_ai_permission, check_raid_permission, check_review_permission, check_data_permission, QuotaLimitExceeded, UpgradeRequired
from app.core import user_service
from app.core import watchlist_stats, account_store

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Ensure data dir exists
DATA_DIR.mkdir(exist_ok=True)
DAY_KLINE_CACHE_DIR = DATA_DIR / "kline_day_cache"
DAY_KLINE_CACHE_DIR.mkdir(exist_ok=True)

# Centralized cache policy:
# user-facing APIs read from cache only; background tasks refresh network data.
REALTIME_CACHE_INTERVAL_SEC = 20
BIYING_BASE_SNAPSHOT_CHECK_INTERVAL_SEC = 300
KLINE_BG_SCAN_INTERVAL_SEC = 60
KLINE_MIN_REFRESH_SEC = 600
DAY_KLINE_REFRESH_SEC = 3600
DAY_KLINE_RETRY_SEC = 600
KLINE_BG_SCAN_BATCH_PER_CYCLE = 40
KLINE_ERROR_LOG_WINDOW_SEC = 60
KLINE_ERROR_LOG_MAX_PER_WINDOW = 8
KLINE_MIN_CACHE_EXPIRE_DAYS = 7
KLINE_DAY_CACHE_EXPIRE_DAYS = 30
MARKET_SENTIMENT_PROBE_COOLDOWN_SEC = 900
KLINE_NON_TRADING_PROBE_DATES = 8
KLINE_NON_TRADING_LOOKBACK_DAYS = 90

# Initialize DB Tables
database.Base.metadata.create_all(bind=database.engine)

app = FastAPI()
SERVER_VERSION = "v3.1.0"

# 重要：CORS 中间件必须在任何路由 (app.include_router) 和中间件注册之前被加载
# 否则会导致 OPTIONS preflight 请求被后续路由拦截（比如被报 "Missing Device ID"）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源，生产环境应限制为前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(payment.router, prefix="/api/payment", tags=["payment"])

FRONTEND_DIR = BASE_DIR.parent / "frontend"
ADMIN_INDEX_FILE = FRONTEND_DIR / "admin" / "index.html"
AUTH_API_PREFIX_FILE = DATA_DIR / "auth_api_prefix.json"
IP_BANLIST_FILE = DATA_DIR / "ip_banlist.json"
USER_OP_LOG_SKIP_PREFIXES = (
    "/api/stocks",
    "/api/indices",
    "/api/limit_up_pool",
    "/api/intraday_pool",
    "/api/market_sentiment",
    "/api/lhb/status",
    "/api/status",
    "/api/config",
    "/api/admin/logs/system",
    "/api/admin/logs/user_ops",
    "/api/admin/monitor/ai_cache",
    "/api/admin/monitor/ai_cache/item",
    "/api/admin/monitor/ai_cost/report",
    "/api/admin/logs/security",
    "/api/admin/data/export/url",
    "/api/admin/data/export/download",
    "/api/admin/login",
    "/api/admin/logout",
    "/api/admin/update_password",
    "/api/admin/update_account",
    "/api/admin/panel_path",
    "/api/admin/api_prefix",
    "/api/admin/auth_api_prefix",
    "/api/admin/users/reset_password",
    "/api/admin/users/add_time",
    "/api/admin/users/ban",
    "/api/admin/users/set_membership",
    "/api/admin/users/delete_all_guests",
    "/api/admin/orders/approve",
    "/api/admin/config",
    "/api/admin/referrals",
    "/api/admin/data/export",
    "/api/admin/data/restore",
    "/api/auth/login_user",
    "/api/auth/register",
    "/api/auth/apply_trial",
    "/api/auth/invite_info",
    "/api/ws/token",
    "/api/client/error_popup",
)

API_DEVICE_AUTH_EXEMPT_PATHS = {
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/login_user",
    "/api/admin/login",
    "/api/status",
}
PRESENCE_HEARTBEAT_SECONDS = 15 * 60
_presence_last_logged: dict = {}
_presence_lock = threading.Lock()
_bg_singleton_socket = None
STATUS_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("STATUS_RATE_LIMIT_WINDOW_SECONDS", "10") or 10)
STATUS_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("STATUS_RATE_LIMIT_MAX_REQUESTS", "30") or 30)
_status_rate_limit_hits: dict = {}
_status_rate_limit_lock = threading.Lock()
ADMIN_REFRESH_THROTTLE_SECONDS = float(os.getenv("ADMIN_REFRESH_THROTTLE_SECONDS", "1.5") or 1.5)
_admin_refresh_last_push_ts: dict = {}
_admin_refresh_lock = threading.Lock()
WS_TOKEN_SECRET_FILE = DATA_DIR / "ws_token_secret.txt"
WS_TOKEN_TTL_SECONDS = int(os.getenv("WS_TOKEN_TTL_SECONDS", "600") or 600)
WS_TOKEN_TTL_SECONDS = min(3600, max(120, WS_TOKEN_TTL_SECONDS))
WS_TOKEN_CLOCK_SKEW_SECONDS = int(os.getenv("WS_TOKEN_CLOCK_SKEW_SECONDS", "30") or 30)
WS_TOKEN_RENEW_AHEAD_SECONDS = int(os.getenv("WS_TOKEN_RENEW_AHEAD_SECONDS", "90") or 90)
WS_TOKEN_RENEW_AHEAD_SECONDS = min(300, max(30, WS_TOKEN_RENEW_AHEAD_SECONDS))
WS_TOKEN_BIND_MODE = str(os.getenv("WS_TOKEN_BIND_MODE", "ua") or "ua").strip().lower()
if WS_TOKEN_BIND_MODE not in {"ua", "ip_ua"}:
    WS_TOKEN_BIND_MODE = "ua"
_ws_token_secret_cache = None
_AUTH_API_PREFIX_CACHE_TTL_SEC = 5.0
_auth_api_prefix_cache: Dict[str, Any] = {"ts": 0.0, "value": "/api/auth"}
_auth_api_prefix_lock = threading.Lock()
_ip_ban_lock = threading.Lock()
_ip_ban_cache: Dict[str, Any] = {"loaded": False, "items": {}}
UNKNOWN_API_BAN_REASON = "access_unknown_api"


def _bool_env(name: str, default: bool = True) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def _normalize_auth_api_prefix(raw_prefix: str) -> str:
    value = str(raw_prefix or "").strip()
    if not value:
        return "/api/auth"
    if not value.startswith("/"):
        value = "/" + value
    if not value.startswith("/api/"):
        value = "/api" + value

    parts = [p for p in value.split("/") if p]
    value = "/" + "/".join(parts)
    if value in {"/", "/api"}:
        raise ValueError("Auth API prefix cannot be / or /api")
    if value.startswith("/api/admin") or value.startswith("/api/payment"):
        raise ValueError("Auth API prefix cannot conflict with admin/payment routes")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]+", value):
        raise ValueError("Auth API prefix only allows letters, numbers, /, _, -")
    return value


def get_auth_api_prefix() -> str:
    now_ts = time.time()
    with _auth_api_prefix_lock:
        cached_ts = float(_auth_api_prefix_cache.get("ts", 0) or 0)
        cached_value = str(_auth_api_prefix_cache.get("value", "/api/auth") or "/api/auth").strip() or "/api/auth"
        if now_ts - cached_ts <= _AUTH_API_PREFIX_CACHE_TTL_SEC:
            return cached_value

    resolved = "/api/auth"
    env_val = str(os.getenv("AUTH_API_PREFIX", "") or "").strip()
    if env_val:
        try:
            resolved = _normalize_auth_api_prefix(env_val)
        except ValueError:
            resolved = "/api/auth"
    else:
        payload = {}
        try:
            if AUTH_API_PREFIX_FILE.exists():
                payload = json.loads(str(AUTH_API_PREFIX_FILE.read_text(encoding="utf-8") or "{}"))
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            try:
                resolved = _normalize_auth_api_prefix(payload.get("prefix", "/api/auth"))
            except ValueError:
                resolved = "/api/auth"

    with _auth_api_prefix_lock:
        _auth_api_prefix_cache["ts"] = time.time()
        _auth_api_prefix_cache["value"] = resolved
    return resolved


def _inject_frontend_runtime_vars(
    html_text: str,
    *,
    admin_api_prefix_hint: str = "",
    auth_api_prefix: str = "",
) -> str:
    raw = str(html_text or "")
    cleaned = re.sub(
        r"\s*<!--\s*Generated by backend runtime injection\s*-->\s*<script>[\s\S]*?</script>\s*",
        "\n",
        raw,
        flags=re.IGNORECASE,
    )
    lines: List[str] = []
    admin_hint = str(admin_api_prefix_hint or "").strip()
    auth_prefix = str(auth_api_prefix or "").strip()
    if admin_hint:
        lines.append(f'window.__ADMIN_API_PREFIX_HINT__ = "{admin_hint}";')
    if auth_prefix:
        lines.append(f'window.__AUTH_API_PREFIX__ = "{auth_prefix}";')
    if not lines:
        return cleaned
    inject_block = (
        "<!-- Generated by backend runtime injection -->\n"
        f"<script>\n{chr(10).join(lines)}\n</script>\n"
    )
    head_open = re.search(r"<head[^>]*>", cleaned, flags=re.IGNORECASE)
    if head_open:
        insert_at = head_open.end()
        return f"{cleaned[:insert_at]}\n{inject_block}{cleaned[insert_at:]}"
    if "</head>" in cleaned:
        return cleaned.replace("</head>", inject_block + "</head>", 1)
    return inject_block + cleaned


def _render_frontend_html_with_runtime_vars(
    html_path: Path,
    *,
    admin_api_prefix_hint: str = "",
    auth_api_prefix: str = "",
) -> Optional[HTMLResponse]:
    if not html_path.exists():
        return None
    try:
        raw = html_path.read_text(encoding="utf-8", errors="ignore")
        patched = _inject_frontend_runtime_vars(
            raw,
            admin_api_prefix_hint=admin_api_prefix_hint,
            auth_api_prefix=auth_api_prefix,
        )
        return HTMLResponse(content=patched)
    except Exception:
        return None


def _ip_addr_obj(ip_text: str):
    text = str(ip_text or "").strip()
    if not text:
        return None
    try:
        return ipaddress.ip_address(text)
    except Exception:
        return None


def _is_local_or_private_ip(ip_text: str) -> bool:
    ip_obj = _ip_addr_obj(ip_text)
    if ip_obj is None:
        return True
    return bool(
        ip_obj.is_loopback
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _load_ip_ban_items_locked() -> Dict[str, Dict[str, Any]]:
    if bool(_ip_ban_cache.get("loaded")):
        items = _ip_ban_cache.get("items", {})
        return items if isinstance(items, dict) else {}

    data = {}
    try:
        if IP_BANLIST_FILE.exists():
            data = json.loads(str(IP_BANLIST_FILE.read_text(encoding="utf-8") or "{}"))
    except Exception:
        data = {}

    items: Dict[str, Dict[str, Any]] = {}
    src = data.get("items", {}) if isinstance(data, dict) else {}
    if isinstance(src, dict):
        for raw_ip, meta in src.items():
            ip_text = str(raw_ip or "").strip()
            if not ip_text:
                continue
            if _is_local_or_private_ip(ip_text):
                continue
            if isinstance(meta, dict):
                items[ip_text] = {
                    "reason": str(meta.get("reason", "") or "").strip(),
                    "banned_at": str(meta.get("banned_at", "") or "").strip(),
                    "path": str(meta.get("path", "") or "").strip(),
                    "method": str(meta.get("method", "") or "").strip().upper(),
                }
            else:
                items[ip_text] = {
                    "reason": "",
                    "banned_at": "",
                    "path": "",
                    "method": "",
                }

    _ip_ban_cache["loaded"] = True
    _ip_ban_cache["items"] = items
    return items


def _save_ip_ban_items_locked(items: Dict[str, Dict[str, Any]]) -> None:
    payload = {"items": items if isinstance(items, dict) else {}}
    try:
        IP_BANLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        IP_BANLIST_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _is_ip_banned(ip_text: str) -> bool:
    ip_addr = str(ip_text or "").strip()
    if not ip_addr or _is_local_or_private_ip(ip_addr):
        return False
    with _ip_ban_lock:
        items = _load_ip_ban_items_locked()
        return ip_addr in items


def _ban_ip(ip_text: str, *, path: str, method: str, reason: str = UNKNOWN_API_BAN_REASON) -> bool:
    ip_addr = str(ip_text or "").strip()
    if not ip_addr or _is_local_or_private_ip(ip_addr):
        return False

    banned_now = False
    with _ip_ban_lock:
        items = _load_ip_ban_items_locked()
        if ip_addr in items:
            meta = items.get(ip_addr, {})
            if isinstance(meta, dict):
                meta["path"] = str(path or "").strip()
                meta["method"] = str(method or "").strip().upper()
                items[ip_addr] = meta
                _save_ip_ban_items_locked(items)
            return False
        items[ip_addr] = {
            "reason": str(reason or "").strip() or UNKNOWN_API_BAN_REASON,
            "banned_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "path": str(path or "").strip(),
            "method": str(method or "").strip().upper(),
        }
        _save_ip_ban_items_locked(items)
        banned_now = True

    if banned_now:
        try:
            log_user_operation(
                "ip_auto_ban",
                status="success",
                actor="system",
                method=str(method or "").upper(),
                path=str(path or "").strip(),
                ip=ip_addr,
                detail=f"reason={str(reason or UNKNOWN_API_BAN_REASON)}",
            )
        except Exception:
            pass
    return banned_now


def _api_path_has_matching_route(request: Request) -> bool:
    path = str(request.scope.get("path", "") or "").strip()
    if not path.startswith("/api/"):
        return True
    scope = dict(request.scope)
    scope["path"] = path
    scope["root_path"] = ""
    for route in app.router.routes:
        route_path = str(getattr(route, "path", "") or "").strip()
        if not route_path.startswith("/api/"):
            continue
        try:
            match, _ = route.matches(scope)
        except Exception:
            continue
        if match in {Match.FULL, Match.PARTIAL}:
            return True
    return False


DISABLE_PUBLIC_FRONTEND = _bool_env("DISABLE_PUBLIC_FRONTEND", False)


def _acquire_background_singleton() -> bool:
    global _bg_singleton_socket
    if _bg_singleton_socket is not None:
        return True
    lock_port = int(os.getenv("BACKGROUND_SINGLETON_PORT", "39731") or 39731)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", lock_port))
        sock.listen(1)
        _bg_singleton_socket = sock
        return True
    except OSError:
        try:
            sock.close()
        except Exception:
            pass
        return False


def _client_ip_from_request(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return ""


def _client_ip_from_websocket(websocket: WebSocket) -> str:
    forwarded = str(websocket.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = str(websocket.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    if websocket.client and websocket.client.host:
        return str(websocket.client.host).strip()
    return ""


def _normalize_ws_ua(raw: str) -> str:
    return " ".join(str(raw or "").strip().lower().split())[:240]


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _urlsafe_b64decode(text: str) -> bytes:
    safe = str(text or "").strip()
    if not safe:
        return b""
    padding = "=" * ((4 - (len(safe) % 4)) % 4)
    return base64.urlsafe_b64decode((safe + padding).encode("ascii"))


def _load_ws_token_secret() -> bytes:
    global _ws_token_secret_cache
    if isinstance(_ws_token_secret_cache, bytes) and _ws_token_secret_cache:
        return _ws_token_secret_cache
    raw = ""
    try:
        if WS_TOKEN_SECRET_FILE.exists():
            raw = str(WS_TOKEN_SECRET_FILE.read_text(encoding="utf-8") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        raw = secrets.token_urlsafe(48)
        try:
            WS_TOKEN_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
            WS_TOKEN_SECRET_FILE.write_text(raw, encoding="utf-8")
            try:
                os.chmod(WS_TOKEN_SECRET_FILE, 0o600)
            except Exception:
                pass
        except Exception:
            pass
    _ws_token_secret_cache = raw.encode("utf-8")
    return _ws_token_secret_cache


def _ws_token_sign(text: str) -> str:
    secret = _load_ws_token_secret()
    digest = hmac.new(secret, text.encode("utf-8"), hashlib.sha256).digest()
    return _urlsafe_b64encode(digest)


def _build_ws_bind(device_id: str, client_ip: str, user_agent: str) -> str:
    did = str(device_id or "").strip()
    ua = _normalize_ws_ua(user_agent)
    if WS_TOKEN_BIND_MODE == "ip_ua":
        ip = str(client_ip or "").strip()
        base = f"{did}|{ip}|{ua}"
    else:
        # Default mode avoids proxy IP mismatch while still binding token to device+UA.
        base = f"{did}|{ua}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _mint_ws_token(
    *,
    device_id: str,
    channel: str,
    client_ip: str,
    user_agent: str,
) -> Dict[str, Any]:
    now_ts = int(time.time())
    exp_ts = now_ts + int(WS_TOKEN_TTL_SECONDS)
    header = {"alg": "HS256", "typ": "WST"}
    payload = {
        "v": 1,
        "did": str(device_id or "").strip(),
        "ch": str(channel or "").strip().lower(),
        "iat": now_ts,
        "exp": exp_ts,
        "nonce": secrets.token_hex(8),
        "bind": _build_ws_bind(device_id, client_ip, user_agent),
    }
    header_b64 = _urlsafe_b64encode(
        json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    payload_b64 = _urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}"
    signature = _ws_token_sign(signing_input)
    return {
        "token": f"{signing_input}.{signature}",
        "expires_at": exp_ts,
        "issued_at": now_ts,
    }


def _verify_ws_token(
    *,
    token: str,
    device_id: str,
    channel: str,
    client_ip: str,
    user_agent: str,
) -> bool:
    raw = str(token or "").strip()
    if not raw:
        return False
    parts = raw.split(".")
    if len(parts) != 3:
        return False
    header_b64, payload_b64, signature_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}"
    expected_sig = _ws_token_sign(signing_input)
    if not hmac.compare_digest(signature_b64, expected_sig):
        return False
    try:
        payload_raw = _urlsafe_b64decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_raw)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False

    did = str(payload.get("did") or "").strip()
    ch = str(payload.get("ch") or "").strip().lower()
    if did != str(device_id or "").strip():
        return False
    if ch != str(channel or "").strip().lower():
        return False

    now_ts = int(time.time())
    try:
        exp_ts = int(payload.get("exp") or 0)
        iat_ts = int(payload.get("iat") or 0)
    except Exception:
        return False
    if exp_ts <= 0 or iat_ts <= 0:
        return False
    if exp_ts < now_ts - int(WS_TOKEN_CLOCK_SKEW_SECONDS):
        return False
    if iat_ts > now_ts + int(WS_TOKEN_CLOCK_SKEW_SECONDS):
        return False

    expected_bind = _build_ws_bind(device_id, client_ip, user_agent)
    token_bind = str(payload.get("bind") or "").strip()
    if not token_bind:
        return False
    if hmac.compare_digest(token_bind, expected_bind):
        return True

    # Compatibility: allow either strict(ip+ua) or relaxed(ua) bind to tolerate proxy topology.
    did = str(device_id or "").strip()
    ip = str(client_ip or "").strip()
    ua = _normalize_ws_ua(user_agent)
    strict_bind = hashlib.sha256(f"{did}|{ip}|{ua}".encode("utf-8")).hexdigest()
    relaxed_bind = hashlib.sha256(f"{did}|{ua}".encode("utf-8")).hexdigest()
    if hmac.compare_digest(token_bind, strict_bind) or hmac.compare_digest(token_bind, relaxed_bind):
        return True
    return False


def _is_status_rate_limited(request: Request) -> bool:
    ip = _client_ip_from_request(request)
    if ip in {"127.0.0.1", "::1", "localhost"}:
        return False

    now_ts = time.time()
    with _status_rate_limit_lock:
        hits = _status_rate_limit_hits.get(ip, [])
        hits = [t for t in hits if now_ts - t <= STATUS_RATE_LIMIT_WINDOW_SECONDS]
        if len(hits) >= STATUS_RATE_LIMIT_MAX_REQUESTS:
            _status_rate_limit_hits[ip] = hits
            return True
        hits.append(now_ts)
        _status_rate_limit_hits[ip] = hits
    return False


def _parse_device_os(user_agent: str, platform_hint: str, header_os: str) -> str:
    hint = (header_os or "").strip()
    if hint:
        return hint

    ua = (user_agent or "").lower()
    if "windows nt 10.0" in ua:
        return "Windows"
    if "windows" in ua:
        return "Windows"
    if "android" in ua:
        m = re.search(r"android\s+([0-9.]+)", ua)
        return f"Android {m.group(1)}" if m else "Android"
    if "iphone os" in ua:
        m = re.search(r"iphone os\s+([0-9_]+)", ua)
        return f"iOS {m.group(1).replace('_', '.')}" if m else "iOS"
    if "ipad" in ua:
        return "iPadOS"
    if "mac os x" in ua or "macintosh" in ua:
        return "macOS"
    if "linux" in ua:
        return "Linux"

    platform = (platform_hint or "").strip().strip('"')
    if platform:
        return platform
    return "Unknown OS"


def _parse_device_browser(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return "Unknown Browser"
    if "edg/" in ua:
        return "Edge"
    if "firefox/" in ua:
        return "Firefox"
    if "chrome/" in ua and "edg/" not in ua:
        return "Chrome"
    if "safari/" in ua and "chrome/" not in ua:
        return "Safari"
    if "micromessenger/" in ua:
        return "WeChat"
    return "Other"


def _resolve_device_type(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if any(k in ua for k in ("mobile", "android", "iphone", "ipad")):
        return "mobile"
    return "desktop"


def _device_info_from_request(request: Request) -> str:
    ua = (request.headers.get("User-Agent") or "").strip()
    platform_hint = (request.headers.get("Sec-CH-UA-Platform") or "").strip()
    model = (request.headers.get("X-Device-Model") or "").strip()
    header_os = (request.headers.get("X-Device-OS") or "").strip()
    app_version = (request.headers.get("X-App-Version") or "").strip()

    parts = []
    if model:
        parts.append(model)
    parts.append(_parse_device_os(ua, platform_hint, header_os))
    parts.append(_parse_device_browser(ua))
    parts.append(_resolve_device_type(ua))
    if app_version:
        parts.append(f"App {app_version}")

    info = " | ".join([x for x in parts if x]).strip()
    return info[:240]


def _normalize_auth_request_path(path: str) -> str:
    safe_path = str(path or "").strip()
    normalized = safe_path.rstrip("/") or "/"
    auth_api_prefix = str(get_auth_api_prefix() or "/api/auth").strip() or "/api/auth"
    if auth_api_prefix != "/api/auth" and (
        normalized == auth_api_prefix or normalized.startswith(auth_api_prefix + "/")
    ):
        suffix = safe_path[len(auth_api_prefix):]
        return "/api/auth" + suffix
    return safe_path


def _should_log_api_path(path: str) -> bool:
    normalized_path = _normalize_auth_request_path(path)
    if not normalized_path.startswith("/api/"):
        return False
    return not any(normalized_path.startswith(prefix) for prefix in USER_OP_LOG_SKIP_PREFIXES)


def _is_admin_api_path(path: str) -> bool:
    safe_path = str(path or "").strip()
    normalized = safe_path.rstrip("/") or "/"
    if normalized == "/api/admin" or safe_path.startswith("/api/admin/"):
        return True
    try:
        custom_prefix = str(admin.get_admin_api_prefix() or "/api/admin").strip() or "/api/admin"
    except Exception:
        custom_prefix = "/api/admin"
    if custom_prefix != "/api/admin" and (
        normalized == custom_prefix or normalized.startswith(custom_prefix + "/")
    ):
        return True
    return False


def _resolve_actor(path: str, request: Request) -> str:
    admin_token = (request.headers.get("X-Admin-Token") or "").strip()
    if admin_token:
        return "admin"
    if _is_admin_api_path(path):
        return "admin"
    device_id = (request.headers.get("X-Device-ID") or "").strip()
    if device_id.startswith("guest") or device_id.startswith("visitor_"):
        return "guest"
    if device_id:
        return "user"
    return "anonymous"


def _maybe_log_online_presence(
    request: Request,
    *,
    status_code: int,
    username: str,
    device_id: str,
    device_info: str,
    client_ip: str,
) -> None:
    path = request.url.path
    if not path.startswith("/api/"):
        return
    if _is_admin_api_path(path):
        return
    if int(status_code or 0) >= 400:
        return

    actor = _resolve_actor(path, request)
    if actor == "admin":
        return

    key = str(device_id or "").strip() or f"user:{str(username or '').strip()}"
    if not key or key == "user:":
        return

    now_ts = time.time()
    should_log = False
    with _presence_lock:
        last_ts = float(_presence_last_logged.get(key, 0) or 0)
        if now_ts - last_ts >= PRESENCE_HEARTBEAT_SECONDS:
            _presence_last_logged[key] = now_ts
            should_log = True
    if not should_log:
        return

    log_user_operation(
        "online_presence",
        status="success",
        actor=actor,
        method=request.method,
        path=path,
        ip=client_ip,
        username=username,
        device_id=device_id,
        device_info=device_info,
        detail="在线心跳",
    )


def _safe_bool_text(value: Optional[bool]) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def _resolve_admin_refresh_targets(path: str, method: str, status_code: int) -> List[str]:
    safe_path = _normalize_auth_request_path(path).strip().lower()
    safe_method = str(method or "").upper()
    if int(status_code or 0) >= 400:
        return []
    if safe_method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return []

    targets: List[str] = []

    if safe_path.startswith("/api/admin/"):
        targets.append("overview")
        if "/orders" in safe_path:
            targets.extend(["orders", "referrals", "users"])
        if "/users" in safe_path:
            targets.append("users")
        if "/watchlist" in safe_path:
            targets.append("watchlist")
        if "/news" in safe_path:
            targets.append("news")
        if "/lhb" in safe_path:
            targets.append("lhb")
        if "/config" in safe_path or "/panel_path" in safe_path or "/api_prefix" in safe_path:
            targets.append("config")
        if "/data/" in safe_path:
            targets.append("data_files")
        if "/login" in safe_path or "/logout" in safe_path:
            targets.append("online_users")

    if safe_path.startswith("/api/payment/"):
        targets.extend(["overview", "orders", "referrals", "users"])
    if safe_path in {"/api/auth/register", "/api/auth/login_user", "/api/auth/apply_trial"}:
        targets.extend(["overview", "users", "online_users"])
    if safe_path.startswith("/api/watchlist/stat/") or safe_path in {
        "/api/add_watchlist",
        "/api/remove_watchlist",
        "/api/add_stock",
        "/api/watchlist/remove",
    }:
        targets.extend(["overview", "watchlist"])
    if safe_path.startswith("/api/lhb/"):
        targets.extend(["overview", "lhb"])

    if not targets:
        return []
    # Deduplicate while preserving order.
    unique: List[str] = []
    seen = set()
    for item in targets:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


async def _maybe_emit_admin_refresh_event(path: str, method: str, status_code: int) -> None:
    targets = _resolve_admin_refresh_targets(path, method, status_code)
    if not targets:
        return

    key = "|".join(sorted(targets))
    now_ts = time.time()
    with _admin_refresh_lock:
        last_ts = float(_admin_refresh_last_push_ts.get(key, 0) or 0)
        if now_ts - last_ts < max(0.2, ADMIN_REFRESH_THROTTLE_SECONDS):
            return
        _admin_refresh_last_push_ts[key] = now_ts

    try:
        await ws_hub.broadcast_admin_event(
            {
                "event": "admin_refresh",
                "targets": targets,
                "source": "api_write",
                "ts": int(now_ts),
            }
        )
    except Exception:
        pass


def _log_ai_quota_event(
    request: Request,
    user: models.User,
    *,
    feature: str,
    source: str,
    cached: Optional[bool],
    real_call: Optional[bool],
    extra: Optional[dict] = None,
) -> None:
    payload = {
        "feature": str(feature or "").strip() or "未分类",
        "source": str(source or "").strip() or "-",
        "cached": _safe_bool_text(cached),
        "real_call": _safe_bool_text(real_call),
    }
    if isinstance(extra, dict):
        for k, v in extra.items():
            payload[str(k)] = v
    try:
        log_user_operation(
            "ai_quota_consume",
            status="success",
            actor="user",
            method=request.method,
            path=request.url.path,
            username=str(getattr(user, "username", "") or ""),
            device_id=str(getattr(user, "device_id", "") or (request.headers.get("X-Device-ID") or "").strip()),
            device_info=_device_info_from_request(request),
            ip=_client_ip_from_request(request),
            detail=f"feature={payload.get('feature')} cached={payload.get('cached')} real_call={payload.get('real_call')}",
            extra=payload,
        )
    except Exception:
        pass


@app.middleware("http")
async def admin_panel_custom_path_guard(request: Request, call_next):
    path = request.url.path
    normalized = path.rstrip("/") or "/"

    admin_api_prefix = admin.get_admin_api_prefix()
    if admin_api_prefix != "/api/admin":
        if normalized == "/api/admin" or normalized.startswith("/api/admin/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        if normalized == admin_api_prefix or normalized.startswith(admin_api_prefix + "/"):
            suffix = path[len(admin_api_prefix):]
            new_path = "/api/admin" + suffix
            request.scope["path"] = new_path
            request.scope["raw_path"] = new_path.encode("utf-8")
            path = new_path
            normalized = path.rstrip("/") or "/"

    if path.startswith("/api/"):
        return await call_next(request)

    admin_path = admin.get_admin_panel_path()

    if admin_path != "/admin" and (normalized == "/admin" or normalized.startswith("/admin/")):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    if ADMIN_INDEX_FILE.exists():
        if normalized == admin_path and path.endswith("/") and path != "/":
            return RedirectResponse(url=admin_path, status_code=307)
        if normalized == admin_path or normalized.startswith(admin_path + "/"):
            injected = _render_frontend_html_with_runtime_vars(
                ADMIN_INDEX_FILE,
                admin_api_prefix_hint=admin_api_prefix,
                auth_api_prefix=get_auth_api_prefix(),
            )
            if injected is not None:
                return injected
            return FileResponse(str(ADMIN_INDEX_FILE))

    if DISABLE_PUBLIC_FRONTEND:
        return JSONResponse({"detail": "Public frontend disabled"}, status_code=404)

    return await call_next(request)


@app.middleware("http")
async def api_device_auth_guard(request: Request, call_next):
    path = request.url.path
    normalized = path.rstrip("/") or "/"
    method = str(request.method or "").upper()
    admin_api_prefix = admin.get_admin_api_prefix()

    auth_api_prefix = get_auth_api_prefix()
    if auth_api_prefix != "/api/auth":
        if normalized == "/api/auth" or normalized.startswith("/api/auth/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if normalized == auth_api_prefix or normalized.startswith(auth_api_prefix + "/"):
            suffix = path[len(auth_api_prefix):]
            new_path = "/api/auth" + suffix
            request.scope["path"] = new_path
            request.scope["raw_path"] = new_path.encode("utf-8")
            path = new_path
            normalized = path.rstrip("/") or "/"

    if method == "OPTIONS":
        return await call_next(request)
    if not path.startswith("/api/"):
        return await call_next(request)

    client_ip = _client_ip_from_request(request)
    if _is_ip_banned(client_ip):
        return JSONResponse(status_code=403, content={"detail": "IP has been banned"})

    has_admin_token = bool((request.headers.get("X-Admin-Token") or "").strip())
    is_admin_namespace = bool(
        path.startswith("/api/admin/")
        or (
            admin_api_prefix != "/api/admin"
            and (normalized == admin_api_prefix or normalized.startswith(admin_api_prefix + "/"))
        )
    )
    should_check_unknown_api = bool(
        (not has_admin_token)
        and (not _is_local_or_private_ip(client_ip))
        and (not is_admin_namespace)
    )
    if should_check_unknown_api and (not _api_path_has_matching_route(request)):
        _ban_ip(
            client_ip,
            path=path,
            method=method,
            reason=UNKNOWN_API_BAN_REASON,
        )
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})

    if is_admin_namespace:
        return await call_next(request)
    if path in API_DEVICE_AUTH_EXEMPT_PATHS:
        return await call_next(request)

    device_id = (request.headers.get("X-Device-ID") or "").strip()
    if not device_id:
        return JSONResponse(status_code=401, content={"detail": "Missing Device ID"})
    try:
        account_store.ensure_device_not_banned(device_id)
    except Exception as e:
        return JSONResponse(status_code=403, content={"detail": str(e) or "Device is banned"})
    return await call_next(request)


@app.middleware("http")
async def user_operation_logger(request: Request, call_next):
    path = request.url.path
    method = request.method
    start_ts = time.time()
    username = (request.headers.get("X-User-Name") or "").strip()
    device_id = (request.headers.get("X-Device-ID") or "").strip()
    device_info = _device_info_from_request(request)
    client_ip = _client_ip_from_request(request)

    is_options = str(method or "").upper() == "OPTIONS"

    try:
        response = await call_next(request)
    except Exception as e:
        if (not is_options) and _should_log_api_path(path):
            log_user_operation(
                "api_call",
                status="failed",
                actor=_resolve_actor(path, request),
                method=method,
                path=path,
                ip=client_ip,
                username=username,
                device_id=device_id,
                device_info=device_info,
                detail=f"exception={str(e)}",
            )
        raise

    if (not is_options) and _should_log_api_path(path):
        status_code = int(getattr(response, "status_code", 0) or 0)
        latency_ms = int((time.time() - start_ts) * 1000)
        log_user_operation(
            "api_call",
            status="success" if status_code < 400 else "failed",
            actor=_resolve_actor(path, request),
            method=method,
            path=path,
            ip=client_ip,
            username=username,
            device_id=device_id,
            device_info=device_info,
            detail=f"status_code={status_code}, latency_ms={latency_ms}",
            extra={"status_code": status_code, "latency_ms": latency_ms},
        )
    else:
        status_code = int(getattr(response, "status_code", 0) or 0)

    _maybe_log_online_presence(
        request,
        status_code=status_code,
        username=username,
        device_id=device_id,
        device_info=device_info,
        client_ip=client_ip,
    )
    await _maybe_emit_admin_refresh_event(path=path, method=method, status_code=status_code)

    return response

@app.get("/api/status")
async def get_system_status(request: Request):
    """获取系统状态（交易日/时间）"""
    if _is_status_rate_limited(request):
        return JSONResponse(status_code=429, content={"detail": "Too many status requests"})
    auth_api_prefix = get_auth_api_prefix()
    return {
        "status": "success",
        "is_trading_time": is_trading_time(),
        "is_market_open_day": is_market_open_day(),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "server_version": SERVER_VERSION,
        "auth_api_prefix": auth_api_prefix,
    }

@app.get("/api/news_history/clear")
async def clear_news_history(range: str = "all", user: models.User = Depends(check_data_permission)):
    """历史兼容接口：服务端新闻历史不再支持清理，仅允许客户端本地缓存清理。"""
    return {
        "status": "disabled",
        "message": "服务器新闻历史已改为长期保留，不支持服务端清理；请清理客户端本地缓存。",
    }

def load_watchlist():
    """加载复盘生成的关注列表"""
    file_path = DATA_DIR / "watchlist.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data
        except:
            return []
    return []

def save_watchlist(data):
    """保存关注列表"""
    file_path = DATA_DIR / "watchlist.json"
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存监控列表失败: {e}")

def load_favorites():
    """加载自选股列表（长期关注）"""
    file_path = DATA_DIR / "favorites.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_favorites(data):
    """保存自选股列表"""
    file_path = DATA_DIR / "favorites.json"
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存自选列表失败: {e}")

def clean_watchlist():
    """
    Clean up watchlist (AI generated only)
    """
    global watchlist_data
    
    if not watchlist_data:
        return

    seen = set()
    new_list = []
    
    # Keep unique codes
    for item in watchlist_data:
        if item['code'] not in seen:
            seen.add(item['code'])
            new_list.append(item)
            
    # Limit size
    if len(new_list) > 100:
        new_list = new_list[:100]
        
    # Daily cleanup: keep non-discarded entries only.
    final_list = []
    for item in new_list:
        if item.get('strategy_type') == 'Discarded':
            continue
        final_list.append(item)
    
    watchlist_data = final_list
    save_watchlist(watchlist_data)
    reload_watchlist_globals()

def reload_watchlist_globals():
    """重新加载全局变量"""
    global watchlist_data, watchlist_map, WATCH_LIST, favorites_data, favorites_map
    watchlist_data = load_watchlist()
    watchlist_map = {item['code']: item for item in watchlist_data}
    
    favorites_data = load_favorites()
    favorites_map = {item['code']: item for item in favorites_data}
    
    # WATCH_LIST includes both
    WATCH_LIST = list(set(list(watchlist_map.keys()) + list(favorites_map.keys())))

@app.get("/api/news_history")
async def get_news_history(
    since_ts: Optional[int] = None,
    limit: int = 2000,
    user: models.User = Depends(check_data_permission),
):
    """获取新闻历史记录"""
    history_file = DATA_DIR / "news_history.json"
    if history_file.exists():
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                items = data if isinstance(data, list) else []
                safe_limit = max(1, min(int(limit or 2000), 5000))
                safe_since = int(since_ts or 0)

                if safe_since > 0:
                    filtered = []
                    for row in items:
                        if not isinstance(row, dict):
                            continue
                        ts = int(row.get("timestamp", 0) or 0)
                        if ts > safe_since:
                            filtered.append(row)
                    filtered.sort(key=lambda x: int(x.get("timestamp", 0) or 0), reverse=True)
                    filtered = filtered[:safe_limit]
                    latest_ts = max([int(x.get("timestamp", 0) or 0) for x in items], default=0)
                    return {
                        "status": "success",
                        "data": filtered,
                        "latest_timestamp": int(latest_ts),
                        "total": len(items),
                        "delta": len(filtered),
                    }

                items_sorted = sorted(
                    [x for x in items if isinstance(x, dict)],
                    key=lambda x: int(x.get("timestamp", 0) or 0),
                    reverse=True,
                )
                items_sorted = items_sorted[:safe_limit]
                latest_ts = max([int(x.get("timestamp", 0) or 0) for x in items], default=0)
                return {
                    "status": "success",
                    "data": items_sorted,
                    "latest_timestamp": int(latest_ts),
                    "total": len(items),
                    "delta": len(items_sorted),
                }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "success", "data": [], "latest_timestamp": 0, "total": 0, "delta": 0}

# 全局变量
watchlist_data = load_watchlist()
watchlist_map = {item['code']: item for item in watchlist_data}
favorites_data = load_favorites()
favorites_map = {item['code']: item for item in favorites_data}
WATCH_LIST = list(set(list(watchlist_map.keys()) + list(favorites_map.keys())))
limit_up_pool_data = []
broken_limit_pool_data = []
intraday_pool_data = [] # New global for fast intraday pool
ANALYSIS_CACHE = {} # Cache for AI analysis results: {code: {content: str, timestamp: float}}

cache_lock = threading.Lock()
stock_quotes_cache = []
stock_quotes_cache_ts = 0.0
stock_quotes_refresh_guard = threading.Lock()
indices_cache = []
indices_cache_ts = 0.0
indices_refresh_guard = threading.Lock()
market_sentiment_cache = {}
market_sentiment_cache_ts = 0.0
market_sentiment_refresh_guard = threading.Lock()
market_circ_map_cache = {}
market_circ_map_cache_ts = 0.0
market_circ_map_refresh_guard = threading.Lock()
market_ws_last_stock_hash = ""
market_ws_last_pool_hash = ""
market_ws_last_sentiment_hash = ""
market_sentiment_probe_last_ts = 0.0
market_ws_last_lhb_syncing = None
market_ws_last_quote_state = {}
admin_ws_last_overview_hint_hash = ""
admin_ws_last_online_users_hash = ""
day_kline_refresh_ts = {}
day_kline_attempt_ts = {}
kline_update_cursor = 0
kline_error_window_start_ts = 0.0
kline_error_window_count = 0
kline_error_suppressed = 0
analysis_key_locks = {}
market_sentiment_cache_last_persist_hash = ""

DEFAULT_MARKET_STATS = {
    "limit_up_count": 0,
    "limit_down_count": 0,
    "broken_count": 0,
    "up_count": 0,
    "down_count": 0,
    "flat_count": 0,
    "total_volume": 0,
    "sentiment": "Neutral",
    "suggestion": "观察",
}
DEFAULT_INDICES_ROWS = [
    {"name": "上证指数", "current": 0, "change": 0, "amount": 0},
    {"name": "深证成指", "current": 0, "change": 0, "amount": 0},
    {"name": "创业板指", "current": 0, "change": 0, "amount": 0},
]
MARKET_SENTIMENT_CACHE_FILE = DATA_DIR / "market_sentiment_cache.json"

def load_analysis_cache():
    """Load AI analysis cache from disk"""
    global ANALYSIS_CACHE
    file_path = DATA_DIR / "analysis_cache.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                ANALYSIS_CACHE = json.load(f)
        except:
            pass

def save_analysis_cache():
    """Save AI analysis cache to disk"""
    try:
        file_path = DATA_DIR / "analysis_cache.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(ANALYSIS_CACHE, f, ensure_ascii=False)
    except:
        pass


def _market_sentiment_payload_hash(payload: Any) -> str:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        encoded = str(payload)
    return hashlib.md5(encoded.encode("utf-8")).hexdigest()


def _market_sentiment_stats_has_breadth(stats: Any) -> bool:
    if not isinstance(stats, dict):
        return False
    try:
        up_count = int(stats.get("up_count") or 0)
        down_count = int(stats.get("down_count") or 0)
        flat_count = int(stats.get("flat_count") or 0)
    except Exception:
        return False
    return (up_count + down_count + flat_count) > 0


def _market_sentiment_has_meaningful_indices(rows: Any) -> bool:
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            current = float(row.get("current") or 0)
            amount = float(row.get("amount") or 0)
        except Exception:
            continue
        if current > 0 or amount > 0:
            return True
    return False


def load_market_sentiment_cache():
    global market_sentiment_cache
    global market_sentiment_cache_ts
    global indices_cache
    global indices_cache_ts
    global market_sentiment_cache_last_persist_hash

    if not MARKET_SENTIMENT_CACHE_FILE.exists():
        return
    try:
        with open(MARKET_SENTIMENT_CACHE_FILE, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        raw_payload = snapshot.get("payload") if isinstance(snapshot, dict) else {}
        payload = _build_market_sentiment_payload(raw_payload, fallback_indices=(raw_payload or {}).get("indices", []))
        saved_ts = 0.0
        if isinstance(snapshot, dict):
            saved_ts = float(snapshot.get("updated_at_ts", 0) or 0)
        if saved_ts <= 0:
            try:
                saved_ts = float(MARKET_SENTIMENT_CACHE_FILE.stat().st_mtime)
            except Exception:
                saved_ts = time.time()

        with cache_lock:
            market_sentiment_cache = payload
            market_sentiment_cache_ts = saved_ts
            if payload.get("indices"):
                indices_cache = copy.deepcopy(payload.get("indices"))
                indices_cache_ts = max(indices_cache_ts, saved_ts)
            market_sentiment_cache_last_persist_hash = _market_sentiment_payload_hash(payload)
    except Exception as e:
        print(f"加载市场情绪缓存失败: {e}")


def save_market_sentiment_cache(payload: Any = None, updated_ts: Optional[float] = None):
    try:
        if payload is None:
            with cache_lock:
                payload = copy.deepcopy(market_sentiment_cache)
                updated_ts = float(market_sentiment_cache_ts or time.time())

        normalized = _build_market_sentiment_payload(
            payload,
            fallback_indices=(payload or {}).get("indices", []) if isinstance(payload, dict) else [],
        )
        has_breadth = _market_sentiment_stats_has_breadth((normalized or {}).get("stats"))
        has_indices = _market_sentiment_has_meaningful_indices((normalized or {}).get("indices"))
        if not has_breadth and not has_indices:
            return

        snapshot = {
            "updated_at_ts": float(updated_ts or time.time()),
            "payload": normalized,
        }
        tmp_path = MARKET_SENTIMENT_CACHE_FILE.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp_path, MARKET_SENTIMENT_CACHE_FILE)
    except Exception as e:
        print(f"保存市场情绪缓存失败: {e}")

def cleanup_analysis_cache(max_age_days=7):
    """清理超过指定天数的分析缓存"""
    global ANALYSIS_CACHE
    now = time.time()
    max_age_seconds = max_age_days * 86400
    initial_count = len(ANALYSIS_CACHE)
    
    ANALYSIS_CACHE = {
        k: v for k, v in ANALYSIS_CACHE.items() 
        if now - v.get('timestamp', 0) < max_age_seconds
    }
    
    if len(ANALYSIS_CACHE) < initial_count:
        save_analysis_cache()
        print(f"清理完成：移除了 {initial_count - len(ANALYSIS_CACHE)} 条过期分析缓存。")


def _analysis_cache_is_expired(cache_entry, prompt_type: str, now: Optional[datetime] = None) -> bool:
    """Decide whether a cached analysis is still valid for the given prompt type."""
    if not cache_entry:
        return True
    timestamp = float(cache_entry.get("timestamp", 0) or 0)
    if timestamp <= 0:
        return True

    cache_time = datetime.fromtimestamp(timestamp)
    current_time = now or datetime.now()

    if cache_time.date() < current_time.date():
        return True

    if prompt_type == "min_trading_signal":
        if (
            (cache_time.hour < 9 or (cache_time.hour == 9 and cache_time.minute < 30))
            and (current_time.hour > 9 or (current_time.hour == 9 and current_time.minute >= 30))
        ):
            return True
        return False

    if prompt_type == "day_trading_signal":
        return False

    if cache_time.hour < 15 and current_time.hour >= 15:
        return True
    return False


def _get_valid_analysis_content(cache_key: str, prompt_type: str, force: bool = False):
    if force:
        return None
    cache_entry = ANALYSIS_CACHE.get(cache_key)
    if not cache_entry:
        return None
    if _analysis_cache_is_expired(cache_entry, prompt_type):
        return None
    return cache_entry.get("content")


def _get_analysis_lock(cache_key: str):
    lock = analysis_key_locks.get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        analysis_key_locks[cache_key] = lock
    return lock


def _set_stock_quotes_cache(rows):
    global stock_quotes_cache, stock_quotes_cache_ts
    with cache_lock:
        stock_quotes_cache = rows or []
        stock_quotes_cache_ts = time.time()


def _get_stock_quotes_cache():
    with cache_lock:
        # Return a shallow list snapshot to avoid repeated deep-copy overhead
        # on hot paths (market WS broadcaster + high-frequency API reads).
        return list(stock_quotes_cache)


def _normalize_market_code(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith(("sh", "sz", "bj")):
        return raw
    digits = "".join(filter(str.isdigit, raw))
    if len(digits) == 6:
        if digits.startswith("6"):
            return f"sh{digits}"
        if digits.startswith(("0", "3")):
            return f"sz{digits}"
        if digits.startswith(("8", "4", "9")):
            return f"bj{digits}"
    return raw


def _safe_float_number(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _set_market_circ_map_cache(payload: dict):
    global market_circ_map_cache, market_circ_map_cache_ts
    now_ts = time.time()
    with cache_lock:
        market_circ_map_cache = payload if isinstance(payload, dict) else {}
        market_circ_map_cache_ts = now_ts


def _get_market_circ_map_cache() -> dict:
    with cache_lock:
        return copy.deepcopy(market_circ_map_cache)


def _build_market_circ_map_from_df(market_df) -> dict:
    market_map = {}
    if market_df is None or market_df.empty:
        return market_map
    for _, row in market_df.iterrows():
        raw_code = str(row.get("code", "")).strip().lower()
        if not raw_code:
            continue
        circ_mv = _safe_float_number(row.get("circ_mv", row.get("circulation_value", 0)))
        norm_code = _normalize_market_code(raw_code)
        digits = "".join(filter(str.isdigit, norm_code or raw_code))
        market_map[raw_code] = circ_mv
        if norm_code:
            market_map[norm_code] = circ_mv
        if len(digits) == 6:
            market_map[digits] = circ_mv
    return market_map


def refresh_market_circ_map_cache(allow_network: bool = True):
    market_map = {}
    try:
        market_df = data_provider.fetch_all_market_data() if allow_network else data_provider.get_cached_all_market_data()
        market_map = _build_market_circ_map_from_df(market_df)
    except Exception:
        return
    if market_map:
        _set_market_circ_map_cache(market_map)


def ensure_market_circ_map_cache(max_age_sec: int = 600, allow_network: bool = True):
    now_ts = time.time()
    with cache_lock:
        has_rows = bool(market_circ_map_cache)
        cache_age = (now_ts - market_circ_map_cache_ts) if market_circ_map_cache_ts > 0 else float("inf")
    if has_rows and cache_age <= max_age_sec:
        return

    with market_circ_map_refresh_guard:
        now_ts = time.time()
        with cache_lock:
            has_rows = bool(market_circ_map_cache)
            cache_age = (now_ts - market_circ_map_cache_ts) if market_circ_map_cache_ts > 0 else float("inf")
        if has_rows and cache_age <= max_age_sec:
            return
        refresh_market_circ_map_cache(allow_network=allow_network)


def _normalize_indices_rows(rows):
    if not isinstance(rows, list):
        return []
    normalized = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append(copy.deepcopy(row))
    return normalized


def _build_market_sentiment_payload(data=None, fallback_indices=None):
    src = data if isinstance(data, dict) else {}
    raw_indices = src.get("indices")
    normalized_indices = _normalize_indices_rows(raw_indices)
    if not normalized_indices:
        normalized_indices = _normalize_indices_rows(fallback_indices)
    if not normalized_indices:
        normalized_indices = copy.deepcopy(DEFAULT_INDICES_ROWS)

    stats_src = src.get("stats")
    if not isinstance(stats_src, dict):
        stats_src = {}
    stats = dict(DEFAULT_MARKET_STATS)
    for key in DEFAULT_MARKET_STATS.keys():
        if key in stats_src and stats_src.get(key) is not None:
            stats[key] = stats_src.get(key)

    return {
        "indices": normalized_indices,
        "stats": stats,
    }


def refresh_indices_cache():
    global indices_cache, indices_cache_ts
    try:
        rows = _normalize_indices_rows(data_provider.fetch_indices() or [])
        if not rows:
            return
        now_ts = time.time()
        with cache_lock:
            indices_cache = rows
            indices_cache_ts = now_ts
    except Exception as e:
        print(f"刷新指数缓存失败: {e}")


def get_indices_cache():
    with cache_lock:
        rows = copy.deepcopy(indices_cache)
        fallback = copy.deepcopy((market_sentiment_cache or {}).get("indices", []))
    normalized_fallback = _normalize_indices_rows(fallback)
    return rows or normalized_fallback or copy.deepcopy(DEFAULT_INDICES_ROWS)


def ensure_indices_cache(max_age_sec: int = max(60, REALTIME_CACHE_INTERVAL_SEC * 3)):
    now_ts = time.time()
    with cache_lock:
        has_rows = bool(indices_cache)
        cache_age = (now_ts - indices_cache_ts) if indices_cache_ts > 0 else float("inf")
    if has_rows and cache_age <= max_age_sec:
        return

    with indices_refresh_guard:
        now_ts = time.time()
        with cache_lock:
            has_rows = bool(indices_cache)
            cache_age = (now_ts - indices_cache_ts) if indices_cache_ts > 0 else float("inf")
        if has_rows and cache_age <= max_age_sec:
            return
        refresh_indices_cache()


def refresh_market_sentiment_cache(allow_non_trading_probe: bool = False):
    global market_sentiment_cache, market_sentiment_cache_ts, indices_cache, indices_cache_ts
    global market_sentiment_cache_last_persist_hash
    try:
        data = get_market_overview(allow_non_trading_probe=allow_non_trading_probe) or {}
        with cache_lock:
            previous_payload = copy.deepcopy(market_sentiment_cache if isinstance(market_sentiment_cache, dict) else {})
            fallback_indices = copy.deepcopy(indices_cache) or copy.deepcopy((market_sentiment_cache or {}).get("indices", []))
        payload = _build_market_sentiment_payload(data, fallback_indices=fallback_indices)

        previous_stats = (previous_payload or {}).get("stats", {}) if isinstance(previous_payload, dict) else {}
        current_stats = (payload or {}).get("stats", {}) if isinstance(payload, dict) else {}
        if _market_sentiment_stats_has_breadth(previous_stats) and not _market_sentiment_stats_has_breadth(current_stats):
            merged_stats = dict(DEFAULT_MARKET_STATS)
            for key in DEFAULT_MARKET_STATS.keys():
                if key in previous_stats and previous_stats.get(key) is not None:
                    merged_stats[key] = previous_stats.get(key)
            payload["stats"] = merged_stats

        now_ts = time.time()
        persist_payload = None
        persist_ts = now_ts
        with cache_lock:
            market_sentiment_cache = payload
            market_sentiment_cache_ts = now_ts
            if payload.get("indices"):
                indices_cache = copy.deepcopy(payload.get("indices"))
                indices_cache_ts = now_ts
            payload_hash = _market_sentiment_payload_hash(payload)
            if payload_hash != market_sentiment_cache_last_persist_hash:
                market_sentiment_cache_last_persist_hash = payload_hash
                has_breadth = _market_sentiment_stats_has_breadth((payload or {}).get("stats"))
                has_indices = _market_sentiment_has_meaningful_indices((payload or {}).get("indices"))
                if has_breadth or has_indices:
                    persist_payload = copy.deepcopy(payload)
                    persist_ts = now_ts
        if persist_payload is not None:
            save_market_sentiment_cache(payload=persist_payload, updated_ts=persist_ts)
    except Exception as e:
        print(f"刷新市场情绪缓存失败: {e}")


def get_market_sentiment_cache():
    with cache_lock:
        payload = copy.deepcopy(market_sentiment_cache)
        fallback_indices = copy.deepcopy(indices_cache)
    return _build_market_sentiment_payload(payload, fallback_indices=fallback_indices)


def ensure_market_sentiment_cache(max_age_sec: int = max(60, REALTIME_CACHE_INTERVAL_SEC * 3)):
    global market_sentiment_probe_last_ts
    now_ts = time.time()
    with cache_lock:
        payload = market_sentiment_cache if isinstance(market_sentiment_cache, dict) else {}
        has_breadth = _market_sentiment_stats_has_breadth(payload.get("stats"))
        has_indices = _market_sentiment_has_meaningful_indices(payload.get("indices"))
        has_payload = has_breadth or has_indices
        cache_age = (now_ts - market_sentiment_cache_ts) if market_sentiment_cache_ts > 0 else float("inf")
    if has_payload and cache_age <= max_age_sec:
        return

    with market_sentiment_refresh_guard:
        now_ts = time.time()
        with cache_lock:
            payload = market_sentiment_cache if isinstance(market_sentiment_cache, dict) else {}
            has_breadth = _market_sentiment_stats_has_breadth(payload.get("stats"))
            has_indices = _market_sentiment_has_meaningful_indices(payload.get("indices"))
            has_payload = has_breadth or has_indices
            cache_age = (now_ts - market_sentiment_cache_ts) if market_sentiment_cache_ts > 0 else float("inf")
        if has_payload and cache_age <= max_age_sec:
            return
        refresh_market_sentiment_cache()
        now_ts = time.time()
        with cache_lock:
            payload = market_sentiment_cache if isinstance(market_sentiment_cache, dict) else {}
            has_breadth = _market_sentiment_stats_has_breadth(payload.get("stats"))
            has_indices = _market_sentiment_has_meaningful_indices(payload.get("indices"))
            need_probe = (not has_breadth) and (not has_indices) and (
                now_ts - float(market_sentiment_probe_last_ts or 0) >= MARKET_SENTIMENT_PROBE_COOLDOWN_SEC
            )
            if need_probe:
                market_sentiment_probe_last_ts = now_ts
        if need_probe:
            refresh_market_sentiment_cache(allow_non_trading_probe=True)


def _day_kline_cache_path(clean_code: str) -> Path:
    return DAY_KLINE_CACHE_DIR / f"{clean_code}.json"


def get_day_kline_from_cache(clean_code: str):
    path = _day_kline_cache_path(clean_code)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def refresh_day_kline_cache_for_code(clean_code: str, force: bool = False):
    # Day-K is end-of-day data. Skip network refresh during intraday or non-trading days
    # to reduce unnecessary upstream pressure and anti-crawl risk.
    if not force:
        now_dt = datetime.now()
        if now_dt.weekday() >= 5:
            return
        if now_dt.hour < 15 or (now_dt.hour == 15 and now_dt.minute < 30):
            return

    now_ts = time.time()
    cache_path = _day_kline_cache_path(clean_code)
    if lhb_manager.is_kline_network_paused():
        # Minute-kline pause should not permanently block day-kline warmup.
        # Reuse existing cache if present; otherwise allow a throttled one-shot refresh.
        if cache_path.exists():
            return
    last_attempt_ts = day_kline_attempt_ts.get(clean_code, 0)
    if now_ts - last_attempt_ts < DAY_KLINE_RETRY_SEC:
        return
    day_kline_attempt_ts[clean_code] = now_ts
    if (not force) and cache_path.exists():
        try:
            mtime = cache_path.stat().st_mtime
            if now_ts - mtime < DAY_KLINE_REFRESH_SEC:
                day_kline_refresh_ts[clean_code] = mtime
                return
        except Exception:
            pass
    last_ts = day_kline_refresh_ts.get(clean_code, 0)
    if (not force) and now_ts - last_ts < DAY_KLINE_REFRESH_SEC:
        return

    try:
        biying_cfg = data_provider._get_biying_config()
        biying_enabled = data_provider._biying_enabled(biying_cfg)
        biying_rows = data_provider.fetch_day_kline_history(clean_code, days=365)
        if biying_rows:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(biying_rows, f, ensure_ascii=False)
            day_kline_refresh_ts[clean_code] = now_ts
            return
        if biying_enabled:
            # If Biying is enabled, do not fall back to AKShare to avoid anti-crawl pressure.
            return

        import akshare as ak
        import pandas as pd

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')
        df = data_provider._call_provider(
            "akshare",
            lambda: ak.stock_zh_a_hist(symbol=clean_code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq"),
        )
        if df is None or df.empty:
            return

        columns = list(df.columns)
        value_start = 1
        if len(columns) > 1:
            second_col = df.iloc[:, 1].astype(str).str.extract(r"(\d{6})", expand=False)
            if second_col.notna().mean() > 0.5:
                value_start = 2

        def pick_col(candidates, fallback_index=None):
            for c in candidates:
                if c in df.columns:
                    return c
            if fallback_index is not None and len(columns) > fallback_index:
                return columns[fallback_index]
            return None

        date_col = pick_col(["date", "day"], 0)
        open_col = pick_col(["open"], value_start)
        close_col = pick_col(["close"], value_start + 1)
        high_col = pick_col(["high"], value_start + 2)
        low_col = pick_col(["low"], value_start + 3)
        volume_col = pick_col(["volume"], value_start + 4)
        if not all([date_col, open_col, close_col, high_col, low_col]):
            return

        out_df = pd.DataFrame({
            "date": df[date_col],
            "open": df[open_col],
            "close": df[close_col],
            "high": df[high_col],
            "low": df[low_col],
        })
        if volume_col:
            out_df["volume"] = df[volume_col]
        out_df["date"] = out_df["date"].astype(str).str.slice(0, 10)
        for col in ["open", "close", "high", "low", "volume"]:
            if col in out_df.columns:
                out_df[col] = pd.to_numeric(out_df[col], errors="coerce")
        out_df = out_df.dropna(subset=["date", "open", "close", "high", "low"])
        if out_df.empty:
            return

        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(out_df.to_dict("records"), f, ensure_ascii=False)
        day_kline_refresh_ts[clean_code] = now_ts
    except Exception as e:
        global kline_error_window_start_ts, kline_error_window_count, kline_error_suppressed
        if now_ts - kline_error_window_start_ts >= KLINE_ERROR_LOG_WINDOW_SEC:
            if kline_error_suppressed > 0:
                print(f"[K线] 已抑制 {kline_error_suppressed} 条日K刷新错误日志")
            kline_error_window_start_ts = now_ts
            kline_error_window_count = 0
            kline_error_suppressed = 0
        if kline_error_window_count < KLINE_ERROR_LOG_MAX_PER_WINDOW:
            kline_error_window_count += 1
            print(f"[K线] 日K刷新失败 {clean_code}: {e}")
        else:
            kline_error_suppressed += 1
        lhb_manager.register_external_kline_failure(e)


def _pick_row_value(row: dict, candidate_keys: List[str], fallback_index: Optional[int] = None):
    if not isinstance(row, dict):
        return None
    lower_key_map = {}
    for raw_key in row.keys():
        if isinstance(raw_key, str):
            lower_key_map[raw_key.lower()] = raw_key
    for key in candidate_keys:
        if key in row:
            value = row.get(key)
            if value is not None and value != "":
                return value
        if isinstance(key, str):
            mapped_key = lower_key_map.get(key.lower())
            if mapped_key is not None and mapped_key in row:
                value = row.get(mapped_key)
                if value is not None and value != "":
                    return value
    if fallback_index is not None:
        values = list(row.values())
        if 0 <= fallback_index < len(values):
            return values[fallback_index]
    return None


def _safe_float_or_none(value):
    try:
        num = float(value)
        if num != num:
            return None
        return num
    except Exception:
        return None


def _normalize_kline_time_text(raw_value) -> str:
    text = str(raw_value or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 14:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
    if len(digits) == 12:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:00"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text


def _normalize_intraday_kline_rows(raw_rows) -> List[Dict[str, Any]]:
    if raw_rows is None:
        return []

    rows = raw_rows
    if hasattr(raw_rows, "to_dict"):
        try:
            rows = raw_rows.to_dict("records")
        except Exception:
            rows = []
    if not isinstance(rows, list):
        return []

    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        date_raw = _pick_row_value(
            row,
            ["date", "time", "day", "datetime", "trade_time", "t", "d", "时间"],
            0,
        )
        date_text = _normalize_kline_time_text(date_raw)
        if not date_text:
            continue

        close_value = _safe_float_or_none(
            _pick_row_value(row, ["close", "c", "latest", "price", "p", "收盘"], 2)
        )
        if close_value is None:
            continue
        open_value = _safe_float_or_none(_pick_row_value(row, ["open", "o", "开盘"], 1))
        high_value = _safe_float_or_none(_pick_row_value(row, ["high", "h", "最高"], 3))
        low_value = _safe_float_or_none(_pick_row_value(row, ["low", "l", "最低"], 4))
        volume_value = _safe_float_or_none(
            _pick_row_value(row, ["volume", "vol", "v", "tv", "pv", "成交量"], 5)
        )

        if open_value is None:
            open_value = close_value
        if high_value is None:
            high_value = max(open_value, close_value)
        if low_value is None:
            low_value = min(open_value, close_value)
        if volume_value is None:
            volume_value = 0.0

        high_value = max(high_value, open_value, close_value)
        low_value = min(low_value, open_value, close_value)

        normalized.append({
            "date": date_text,
            "open": float(open_value),
            "close": float(close_value),
            "high": float(high_value),
            "low": float(low_value),
            "volume": float(volume_value),
        })

    normalized.sort(key=lambda x: str(x.get("date", "")))
    return normalized


def _probe_trade_dates_for_intraday(max_results: int = KLINE_NON_TRADING_PROBE_DATES) -> List[str]:
    end_date = datetime.now().date()
    lookback = max(14, int(KLINE_NON_TRADING_LOOKBACK_DAYS))
    start_date = end_date - timedelta(days=lookback)

    dates = []
    try:
        dates = lhb_manager.get_trade_dates_between(
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
        ) or []
    except Exception:
        dates = []

    normalized = []
    seen = set()
    for item in dates:
        text = str(item or "").strip()[:10]
        if not text:
            continue
        try:
            dt = datetime.strptime(text, "%Y-%m-%d").date()
        except Exception:
            continue
        if dt >= end_date:
            continue
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)

    if not normalized:
        for offset in range(1, lookback + 1):
            dt = end_date - timedelta(days=offset)
            if dt.weekday() >= 5:
                continue
            normalized.append(dt.strftime("%Y-%m-%d"))
            if len(normalized) >= max_results:
                break
        return normalized

    recent = normalized[-max_results:]
    recent.reverse()
    return recent


def _collect_kline_target_codes(max_count=180):
    codes = set()
    for c in WATCH_LIST or []:
        if c:
            codes.add(c)
    for src in (limit_up_pool_data or []):
        c = src.get("code")
        if c:
            codes.add(c)
    for src in (intraday_pool_data or []):
        c = src.get("code")
        if c:
            codes.add(c)
    normalized = []
    for c in codes:
        nc = normalize_stock_code(str(c))
        if nc:
            normalized.append(nc)
    normalized = list(dict.fromkeys(normalized))
    return normalized[:max_count]


def _slice_codes_for_cycle(codes, cursor, batch_size):
    if not codes:
        return [], 0
    total = len(codes)
    size = max(1, min(int(batch_size or 1), total))
    start = int(cursor or 0) % total
    selected = [codes[(start + i) % total] for i in range(size)]
    return selected, (start + size) % total


async def realtime_cache_updater_task():
    while True:
        try:
            await asyncio.to_thread(refresh_stock_quotes_cache)
            await asyncio.to_thread(refresh_indices_cache)
            await asyncio.to_thread(refresh_market_sentiment_cache)
        except Exception as e:
            print(f"实时缓存更新任务错误: {e}")
        await asyncio.sleep(REALTIME_CACHE_INTERVAL_SEC)


async def biying_base_snapshot_task():
    while True:
        try:
            refreshed = await asyncio.to_thread(data_provider.maybe_refresh_biying_base_snapshot, False)
            if refreshed:
                await asyncio.to_thread(refresh_market_circ_map_cache, False)
        except Exception as e:
            print(f"biying base snapshot task error: {e}")
        await asyncio.sleep(BIYING_BASE_SNAPSHOT_CHECK_INTERVAL_SEC)


async def kline_cache_updater_task():
    global kline_update_cursor
    while True:
        try:
            if lhb_manager.is_kline_network_paused():
                await asyncio.sleep(KLINE_BG_SCAN_INTERVAL_SEC)
                continue

            target_codes = _collect_kline_target_codes()
            if target_codes:
                now = datetime.now()
                is_trade_window = (
                    now.weekday() < 5
                    and (now.hour > 9 or (now.hour == 9 and now.minute >= 30))
                    and now.hour < 15
                )
                date_str = datetime.now().strftime('%Y-%m-%d')
                cycle_codes, kline_update_cursor = _slice_codes_for_cycle(
                    target_codes,
                    kline_update_cursor,
                    KLINE_BG_SCAN_BATCH_PER_CYCLE,
                )
                for code in cycle_codes:
                    clean_code = "".join(filter(str.isdigit, code))
                    if not clean_code:
                        continue
                    await asyncio.to_thread(
                        lhb_manager.get_kline_1min,
                        clean_code,
                        date_str,
                        KLINE_MIN_REFRESH_SEC,
                        is_trade_window,
                    )
                    if lhb_manager.is_kline_network_paused():
                        break
                    await asyncio.to_thread(refresh_day_kline_cache_for_code, clean_code, False)
                    if lhb_manager.is_kline_network_paused():
                        break
        except Exception as e:
            print(f"K线缓存更新任务错误: {e}")
        await asyncio.sleep(KLINE_BG_SCAN_INTERVAL_SEC)

async def update_intraday_pool():
    global intraday_pool_data
    # ... (Implementation of scan)
    pass 
    # Placeholder, actual logic is in endpoints or separate scanner calls

@app.get("/api/add_watchlist")
async def add_to_watchlist_api(code: str, name: str, reason: str = "手动添加", authorized: bool = Depends(admin.verify_admin)):
    global favorites_data, watchlist_map
    
    # Check if exists in favorites
    for item in favorites_data:
        if item['code'] == code:
            return {"status": "exists", "msg": "已在自选列表中"}
            
    # Try to preserve existing info if it was in AI list
    existing_info = watchlist_map.get(code, {})
    
    concept = existing_info.get("concept", "")
    # If concept is missing, try to fetch it
    if not concept:
        try:
            info = await asyncio.to_thread(data_provider.fetch_stock_info, code)
            concept = info.get('concept', '')
        except:
            pass

    new_item = {
        "code": code,
        "name": name,
        "reason": reason,
        "strategy_type": "Manual",
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "concept": concept,
        "initial_score": existing_info.get("initial_score", 0)
    }
    
    # Insert at top
    favorites_data.insert(0, new_item)
    save_favorites(favorites_data)
    reload_watchlist_globals()
    
    return {"status": "ok", "msg": "添加成功"}

@app.get("/api/remove_watchlist")
async def remove_from_watchlist_api(code: str, authorized: bool = Depends(admin.verify_admin)):
    global favorites_data, watchlist_data
    
    removed = False
    
    # Remove from favorites
    original_len = len(favorites_data)
    favorites_data = [item for item in favorites_data if item['code'] != code]
    if len(favorites_data) < original_len:
        save_favorites(favorites_data)
        removed = True
        
    # Remove from AI watchlist
    original_len_w = len(watchlist_data)
    watchlist_data = [item for item in watchlist_data if item['code'] != code]
    if len(watchlist_data) < original_len_w:
        save_watchlist(watchlist_data)
        removed = True
    
    if removed:
        reload_watchlist_globals()
        return {"status": "ok", "msg": "删除成功"}
        
    return {"status": "error", "msg": "未找到该股票"}


def load_market_pools():
    """Load market pools from disk"""
    global limit_up_pool_data, broken_limit_pool_data, intraday_pool_data
    file_path = DATA_DIR / "market_pools.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                limit_up_pool_data = data.get("limit_up", [])
                broken_limit_pool_data = data.get("broken", [])
                intraday_pool_data = data.get("intraday", [])
        except:
            pass

def save_market_pools():
    """Save market pools to disk"""
    try:
        file_path = DATA_DIR / "market_pools.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump({
                "limit_up": limit_up_pool_data,
                "broken": broken_limit_pool_data,
                "intraday": intraday_pool_data
            }, f, ensure_ascii=False)
    except:
        pass

# Load caches on startup
load_market_pools()
load_analysis_cache()
load_market_sentiment_cache()

from app.core.stock_utils import calculate_metrics, is_trading_time, is_market_open_day

async def update_market_pools_task():
    global limit_up_pool_data, broken_limit_pool_data
    loop = asyncio.get_event_loop()
    while True:
        try:
            # 1. Limit Up Pool
            new_limit_up = await loop.run_in_executor(None, scan_limit_up_pool)
            if new_limit_up is not None: 
                 # Enrich with metrics immediately to prevent flickering
                enriched_pool = []
                for stock in new_limit_up:
                    try:
                        code = stock['code']
                        # Calculate metrics
                        metrics = calculate_metrics(code)
                        stock.update(metrics) # Merges seal_rate, broken_rate, etc.
                        
                        # Enrich reason/info from Watchlist if available
                        if code in watchlist_map:
                            wl_item = watchlist_map[code]
                            # Priority: Manual Reason > AI Reason > Scanner Reason
                            # Note: scanner uses 'reason' for "X杩炴澘"
                            # If manual/AI reason exists, maybe we append or replace? 
                            # User wants "reason" visible.
                            ai_reason = wl_item.get('reason') or wl_item.get('news_summary')
                            if ai_reason:
                                stock['reason'] = f"{stock.get('reason','')} | {ai_reason}"
                                stock['news_summary'] = ai_reason
                        
                        enriched_pool.append(stock)
                    except Exception as e:
                        print(f"补充股票信息失败 {stock.get('code')}: {e}")
                        enriched_pool.append(stock) # Add anyway
                
                limit_up_pool_data = enriched_pool
            
            # 2. Broken Pool
            new_broken = await loop.run_in_executor(None, scan_broken_limit_pool)
            if new_broken is not None:
                broken_limit_pool_data = new_broken
                
            await loop.run_in_executor(None, save_market_pools)
        except Exception as e:
            print(f"股票池更新错误: {e}")
        
        await asyncio.sleep(20) # Increase interval slightly to allow for metrics calc

async def update_intraday_pool_task():
    """Fast loop for intraday scanner"""
    global intraday_pool_data, limit_up_pool_data, watchlist_data, watchlist_map, WATCH_LIST
    from app.core.market_scanner import scan_intraday_limit_up
    loop = asyncio.get_event_loop()
    while True:
        try:
            # Only run during trading hours (approx) and weekdays
            now = datetime.now()
            if now.weekday() < 5 and 9 <= now.hour < 15:
                result = await loop.run_in_executor(None, scan_intraday_limit_up)
                if result:
                    intraday_stocks, sealed_stocks = result
                    intraday_pool_data = intraday_stocks
                    
                    # Merge into watchlist to avoid disappearing after speed decay.
                    changed = False
                    for s in intraday_stocks:
                        if s['code'] not in watchlist_map:
                            new_item = {
                                "code": s['code'],
                                "name": s['name'],
                                "concept": s['concept'],
                                "news_summary": s['reason'], # 统一使用 news_summary
                                "strategy_type": "LimitUp",
                                "added_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "initial_score": s.get('score', 0)
                            }
                            watchlist_data.append(new_item)
                            watchlist_map[s['code']] = new_item
                            changed = True
                    
                    if changed:
                        WATCH_LIST = list(set(list(watchlist_map.keys()) + list(favorites_map.keys())))
                        await loop.run_in_executor(None, save_watchlist, watchlist_data)
                    
                    # [New] 竞价列表清理逻辑（10:00 后清理竞价策略股票）
                    if now.hour >= 10:
                        cleanup_changed = False
                        sealed_codes = {s['code'] for s in limit_up_pool_data}
                        for item in watchlist_data:
                            if item.get('strategy_type') == 'Aggressive' and '已剔除' not in item.get('news_summary', ''):
                                if item['code'] not in sealed_codes:
                                    item['strategy_type'] = 'Discarded'
                                    item['news_summary'] = f"[竞价过期] {item.get('news_summary', '')}"
                                    cleanup_changed = True
                        
                        if cleanup_changed:
                            await loop.run_in_executor(None, save_watchlist, watchlist_data)
                    
                    # Merge sealed stocks into limit_up_pool_data if not already present
                    if sealed_stocks:
                        existing_codes = {s['code'] for s in limit_up_pool_data}
                        for s in sealed_stocks:
                            if s['code'] not in existing_codes:
                                limit_up_pool_data.append(s)
                                existing_codes.add(s['code'])
            
            # Normal sleep
            await asyncio.sleep(10)
            
        except Exception as e:
            print(f"盘中扫描错误: {e}")
            # Sleep longer on error to avoid hammering
            await asyncio.sleep(60)

if not WATCH_LIST:
    WATCH_LIST = ['sh600519', 'sz002405', 'sz300059']

def refresh_watchlist():
    """刷新全局监控列表"""
    global watchlist_data, watchlist_map, WATCH_LIST
    watchlist_data = load_watchlist()
    watchlist_map = {item['code']: item for item in watchlist_data}
    WATCH_LIST = list(watchlist_map.keys())
    if not WATCH_LIST:
        WATCH_LIST = ['sh600519', 'sz002405', 'sz300059']

class WsTokenIssueRequest(BaseModel):
    channel: Optional[str] = "client"


@app.post("/api/ws/token")
async def issue_ws_token(
    request: Request,
    payload: WsTokenIssueRequest,
    x_device_id: str = Header(None, alias="X-Device-ID"),
):
    device_id = str(x_device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="Missing Device ID")
    account_store.ensure_device_not_banned(device_id)

    channel = str(payload.channel or "client").strip().lower()
    if channel != "client":
        raise HTTPException(status_code=400, detail="Unsupported channel")

    issued = _mint_ws_token(
        device_id=device_id,
        channel=channel,
        client_ip=_client_ip_from_request(request),
        user_agent=str(request.headers.get("user-agent") or ""),
    )
    return {
        "status": "success",
        "channel": channel,
        "token": issued["token"],
        "issued_at": int(issued["issued_at"]),
        "expires_at": int(issued["expires_at"]),
        "ttl_sec": int(WS_TOKEN_TTL_SECONDS),
        "renew_ahead_sec": int(WS_TOKEN_RENEW_AHEAD_SECONDS),
    }


@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    channel = (websocket.query_params.get("channel") or "logs").strip().lower()
    device_id = (websocket.query_params.get("device_id") or "").strip()
    ws_token = (websocket.query_params.get("ws_token") or "").strip()
    admin_token = (websocket.query_params.get("admin_token") or "").strip()
    if channel not in ("logs", "notify", "market", "admin", "client"):
        channel = "logs"
    if channel in ("logs", "notify", "market", "client") and not device_id:
        await websocket.close(code=1008)
        return
    if channel == "client":
        if not _verify_ws_token(
            token=ws_token,
            device_id=device_id,
            channel="client",
            client_ip=_client_ip_from_websocket(websocket),
            user_agent=str(websocket.headers.get("user-agent") or ""),
        ):
            await websocket.close(code=1008)
            return
    if channel == "admin":
        sessions = admin._cleanup_sessions(admin._load_sessions())
        if not admin_token or admin_token not in sessions:
            await websocket.close(code=1008)
            return
    else:
        try:
            account_store.ensure_device_not_banned(device_id)
        except Exception:
            await websocket.close(code=1008)
            return

    await ws_hub.register(websocket, channel=channel, device_id=device_id)
    try:
        if channel == "logs":
            for line in _recent_analysis_log_lines(limit=120):
                try:
                    await websocket.send_text(str(line))
                except Exception:
                    break
        elif channel == "client":
            try:
                await websocket.send_json(
                    {
                        "event": "log_history",
                        "lines": [str(x) for x in _recent_analysis_log_lines(limit=80)],
                    }
                )
            except Exception:
                pass
        while True:
            await websocket.receive_text()  # Keep connection open
    except WebSocketDisconnect:
        await ws_hub.unregister(websocket, channel=channel, device_id=device_id)

@app.get("/api/search")
async def search_stock(q: str, user: models.User = Depends(check_data_permission)):
    """
    搜索股票（本地索引：支持代码、名称）
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: data_provider.search_stock(q))


def normalize_stock_code(code: str):
    if not code:
        return ""
    raw = code.strip().lower()
    if raw.startswith("sh") or raw.startswith("sz") or raw.startswith("bj"):
        return raw
    if len(raw) == 6 and raw.isdigit():
        if raw.startswith("6"):
            return f"sh{raw}"
        if raw.startswith("0") or raw.startswith("3"):
            return f"sz{raw}"
        if raw.startswith("8") or raw.startswith("4") or raw.startswith("9"):
            return f"bj{raw}"
    return raw


class FavoriteStatRequest(BaseModel):
    code: str
    stock_name: str = ""


class ClientErrorPopupRequest(BaseModel):
    message: str
    source: Optional[str] = ""
    code: Optional[str] = ""


@app.get("/api/favorites/quotes")
async def api_favorite_quotes(codes: str = "", user: models.User = Depends(check_data_permission)):
    code_list = [normalize_stock_code(c) for c in codes.split(",") if c.strip()]
    code_list = [c for c in code_list if c]
    if not code_list:
        return []

    unique_codes = list(dict.fromkeys(code_list))
    await asyncio.to_thread(ensure_stock_quotes_cache)
    cached_quotes = _get_stock_quotes_cache()
    cached_map = {}

    def _store_quote_row(raw_row):
        if not isinstance(raw_row, dict):
            return
        c = normalize_stock_code(str(raw_row.get("code", "")))
        if not c:
            return
        row = copy.deepcopy(raw_row)
        row["code"] = c
        cached_map[c] = row
        digits = "".join(filter(str.isdigit, c))
        if len(digits) == 6:
            cached_map[digits] = row

    for row in cached_quotes:
        _store_quote_row(row)

    missing_codes = []
    for req_code in unique_codes:
        key = req_code
        digits = "".join(filter(str.isdigit, req_code))
        hit = cached_map.get(key) or (cached_map.get(digits) if len(digits) == 6 else None)
        if not hit:
            missing_codes.append(req_code)

    if missing_codes:
        try:
            fresh_quotes = await asyncio.to_thread(data_provider.fetch_quotes, missing_codes)
            for row in (fresh_quotes or []):
                _store_quote_row(row)
        except Exception:
            pass

    enriched = []
    for req_code in unique_codes:
        req_digits = "".join(filter(str.isdigit, req_code))
        stock = copy.deepcopy(cached_map.get(req_code) or cached_map.get(req_digits) or {})
        code = normalize_stock_code(stock.get("code", req_code))
        if not code:
            continue
        stock["code"] = code
        fallback_name = str(data_provider.get_stock_name_local(code) or "").strip() or req_code
        stock.setdefault("name", fallback_name)
        stock.setdefault("current", 0)
        stock.setdefault("change_percent", 0)
        stock.setdefault("turnover", 0)
        stock.setdefault("circulation_value", 0)
        metrics = calculate_metrics(code)
        stock.update(metrics)
        stock["is_favorite"] = True
        stock["strategy"] = "Manual"
        stock["news_summary"] = stock.get("news_summary") or "本地自选"
        fav_meta = favorites_map.get(code) or {}
        added_ts = 0
        try:
            added_ts = int(float(fav_meta.get("added_time", 0) or 0))
        except Exception:
            added_ts = 0
        if added_ts <= 0:
            added_at = str(fav_meta.get("added_at", "") or "").strip()
            if added_at:
                try:
                    parsed_dt = datetime.strptime(added_at, "%Y-%m-%d %H:%M:%S")
                    added_ts = int(parsed_dt.timestamp() * 1000)
                except Exception:
                    added_ts = 0
        stock["added_time"] = added_ts
        enriched.append(stock)
    return enriched


@app.post("/api/client/error_popup")
async def report_client_error_popup(
    payload: ClientErrorPopupRequest,
    request: Request,
    user: models.User = Depends(get_current_user),
):
    message = str(payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Missing message")

    source = str(payload.source or "").strip()
    code = str(payload.code or "").strip()
    device_id = str(user.device_id or "").strip()
    username = account_store.get_username_by_device_id(device_id) or ""
    actor = "guest" if device_id.startswith("guest") else "user"
    client_ip = _client_ip_from_request(request)
    device_info = _device_info_from_request(request)

    log_user_operation(
        "frontend_error_popup",
        status="success",
        actor=actor,
        method="POST",
        path="/api/client/error_popup",
        detail=message[:280],
        username=username,
        device_id=device_id,
        device_info=device_info,
        ip=client_ip,
        extra={
            "source": source or "-",
            "code": code or "-",
        },
    )
    add_runtime_log(
        f"[前端] 错误弹窗: user={username or '-'}, device={device_id or '-'}, source={source or '-'}, code={code or '-'}, msg={message[:80]}"
    )
    return {"status": "success"}


@app.post("/api/watchlist/stat/add")
async def add_watchlist_stat(payload: FavoriteStatRequest, user: models.User = Depends(get_current_user)):
    code = normalize_stock_code(payload.code)
    if not code:
        return {"status": "error", "message": "Invalid code"}
    watchlist_stats.add_favorite_stat(str(user.id), code, payload.stock_name)
    return {"status": "success"}


@app.post("/api/watchlist/stat/remove")
async def remove_watchlist_stat(payload: FavoriteStatRequest, user: models.User = Depends(get_current_user)):
    code = normalize_stock_code(payload.code)
    if not code:
        return {"status": "error", "message": "Invalid code"}
    watchlist_stats.remove_favorite_stat(str(user.id), code)
    return {"status": "success"}

@app.post("/api/add_stock")
async def add_stock(code: str, authorized: bool = Depends(admin.verify_admin)):
    """手动添加股票到监控列表"""
    global watchlist_data, watchlist_map, WATCH_LIST
    
    code = code.lower().strip()
    
    # 自动补全前缀
    if len(code) == 6 and code.isdigit():
        if code.startswith('6'):
            code = f"sh{code}"
        elif code.startswith('0') or code.startswith('3'):
            code = f"sz{code}"
        elif code.startswith('8') or code.startswith('4') or code.startswith('9'):
            code = f"bj{code}"
        else:
            # 默认按 sh 处理（或直接报错）
            pass
    
    # 简单的格式校验
    if not (code.startswith('sh') or code.startswith('sz') or code.startswith('bj')):
        return {"status": "error", "message": "Invalid code format"}
        
    # 如果已存在，强制更新为 Manual 策略
    if code in watchlist_map:
        watchlist_map[code]['strategy_type'] = 'Manual'
        watchlist_map[code]['news_summary'] = '手动添加（覆盖）'
        # Save
        try:
            file_path = DATA_DIR / "watchlist.json"
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(watchlist_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存监控列表失败: {e}")
        return {"status": "success", "message": "已更新为手动策略"}
        
    # 计算高级指标
    metrics = calculate_metrics(code)
    
    # 获取股票详细信息（名称 + 行业/概念）
    name, concept = data_provider.get_stock_info(code)
    
    # 添加新股票
    new_item = {
        "code": code,
        "name": name, 
        "news_summary": "手动添加",
        "concept": concept,
        "initial_score": 5, # 默认中等分数
        "strategy_type": "Manual",
        "seal_rate": metrics['seal_rate'],
        "broken_rate": metrics['broken_rate'],
        "next_day_premium": metrics['next_day_premium'],
        "limit_up_days": metrics['limit_up_days'],
        "added_time": time.time() # Record add time for sorting
    }
    
    # Check if exists to update instead of append (though we handled map above, list needs care)
    existing_idx = -1
    for i, item in enumerate(watchlist_data):
        if item['code'] == code:
            existing_idx = i
            break
            
    if existing_idx >= 0:
        watchlist_data[existing_idx] = new_item
    else:
        watchlist_data.insert(0, new_item) # Prepend to list
        
    watchlist_map[code] = new_item
    if code not in WATCH_LIST:
        WATCH_LIST.append(code)
        
    # 保存到文件
    try:
        file_path = DATA_DIR / "watchlist.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(watchlist_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存监控列表失败: {e}")
        
    return {"status": "success"}

import queue
# 使用全局 Queue 来传递日志
log_queue = queue.Queue()

async def log_broadcaster():
    """从队列读取日志并广播"""
    while True:
        try:
            # Non-blocking read
            msg = log_queue.get_nowait()
            await ws_hub.broadcast_log(msg)
        except queue.Empty:
            await asyncio.sleep(0.1)


def _market_hash_obj(value) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        encoded = str(value)
    return hashlib.md5(encoded.encode("utf-8")).hexdigest()


def _json_etag(value) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        encoded = str(value)
    return f'W/"{hashlib.md5(encoded.encode("utf-8")).hexdigest()}"'


def _is_not_modified(request: Request, etag: str) -> bool:
    inm = str(request.headers.get("if-none-match") or "").strip()
    if not inm or not etag:
        return False
    return inm == etag


def ensure_runtime_indexes():
    """Create hot-path indexes when missing (idempotent)."""
    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_users_created_at_runtime ON users (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_purchase_orders_created_at_runtime ON purchase_orders (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_purchase_orders_status_runtime ON purchase_orders (status)",
    ]
    try:
        with database.engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))
    except Exception as e:
        print(f"创建运行时索引失败: {e}")


def _compact_quote_ticks(rows):
    compact = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code", "")).strip()
        if not code:
            continue
        compact.append({
            "code": code,
            "current": row.get("current", 0),
            "change_percent": row.get("change_percent", 0),
            "time": row.get("time", ""),
        })
    return compact


async def market_event_broadcaster():
    global market_ws_last_stock_hash, market_ws_last_pool_hash, market_ws_last_sentiment_hash
    global market_ws_last_lhb_syncing, market_ws_last_quote_state
    while True:
        try:
            if not await ws_hub.has_market_subscribers():
                market_ws_last_stock_hash = ""
                market_ws_last_pool_hash = ""
                market_ws_last_sentiment_hash = ""
                market_ws_last_lhb_syncing = None
                market_ws_last_quote_state = {}
                await asyncio.sleep(2)
                continue

            quotes = _get_stock_quotes_cache()
            if quotes:
                compact_quotes = _compact_quote_ticks(quotes)
                next_quote_state = {}
                changed_quotes = []
                for row in compact_quotes:
                    code = str(row.get("code", "")).strip()
                    if not code:
                        continue
                    signature = (
                        row.get("current", 0),
                        row.get("change_percent", 0),
                        row.get("time", ""),
                    )
                    next_quote_state[code] = signature
                    if market_ws_last_quote_state.get(code) != signature:
                        changed_quotes.append(row)
                market_ws_last_quote_state = next_quote_state

                if changed_quotes:
                    await ws_hub.broadcast_market_event({
                        "event": "quotes_tick",
                        "ts": int(time.time()),
                        "quotes": changed_quotes,
                    })

            refresh_targets = []

            stocks_fingerprint = [
                {
                    "code": str(x.get("code", "")),
                    "strategy": str(x.get("strategy", "")),
                    "is_favorite": bool(x.get("is_favorite", False)),
                }
                for x in (quotes or [])
                if isinstance(x, dict)
            ]
            stock_hash = _market_hash_obj(stocks_fingerprint)
            if stock_hash != market_ws_last_stock_hash:
                market_ws_last_stock_hash = stock_hash
                refresh_targets.append("stocks")

            pool_fingerprint = {
                "limit_up": [str(x.get("code", "")) for x in (limit_up_pool_data or []) if isinstance(x, dict)],
                "broken": [str(x.get("code", "")) for x in (broken_limit_pool_data or []) if isinstance(x, dict)],
            }
            pool_hash = _market_hash_obj(pool_fingerprint)
            if pool_hash != market_ws_last_pool_hash:
                market_ws_last_pool_hash = pool_hash
                refresh_targets.append("limit_up_pool")

            sentiment_payload = get_market_sentiment_cache() or {}
            sentiment_hash = _market_hash_obj(sentiment_payload)
            if sentiment_hash != market_ws_last_sentiment_hash:
                market_ws_last_sentiment_hash = sentiment_hash
                await ws_hub.broadcast_market_event({
                    "event": "market_sentiment",
                    "ts": int(time.time()),
                    "data": sentiment_payload,
                })

            lhb_syncing = bool(lhb_manager.is_syncing)
            if lhb_syncing != market_ws_last_lhb_syncing:
                market_ws_last_lhb_syncing = lhb_syncing
                await ws_hub.broadcast_market_event({
                    "event": "lhb_status",
                    "is_syncing": lhb_syncing,
                })

            if refresh_targets:
                await ws_hub.broadcast_market_event({
                    "event": "market_refresh",
                    "targets": refresh_targets,
                    "ts": int(time.time()),
                })
        except Exception as e:
            print(f"市场WS广播任务错误: {e}")

        await asyncio.sleep(2)


async def admin_event_broadcaster():
    global admin_ws_last_overview_hint_hash, admin_ws_last_online_users_hash
    while True:
        try:
            if not await ws_hub.has_admin_subscribers():
                admin_ws_last_overview_hint_hash = ""
                admin_ws_last_online_users_hash = ""
                await asyncio.sleep(2)
                continue

            ws_stats = await ws_hub.snapshot_stats()
            overview_hint = {
                "watchlist_size": len(watchlist_data or []),
                "favorites_size": len(favorites_data or []),
                "quotes_bucket": int((stock_quotes_cache_ts or 0) // 30),
                "indices_bucket": int((indices_cache_ts or 0) // 60),
                "sentiment_bucket": int((market_sentiment_cache_ts or 0) // 60),
                "limit_up_count": len(limit_up_pool_data or []),
                "broken_count": len(broken_limit_pool_data or []),
                "ws": ws_stats,
            }
            hint_hash = _market_hash_obj(overview_hint)

            active_devices = await ws_hub.snapshot_active_devices()
            online_hash = _market_hash_obj(active_devices)

            targets = []
            if hint_hash != admin_ws_last_overview_hint_hash:
                admin_ws_last_overview_hint_hash = hint_hash
                targets.append("overview")
            if online_hash != admin_ws_last_online_users_hash:
                admin_ws_last_online_users_hash = online_hash
                targets.append("online_users")

            if targets:
                await ws_hub.broadcast_admin_event(
                    {
                        "event": "admin_refresh",
                        "targets": targets,
                        "source": "runtime",
                        "ts": int(time.time()),
                    }
                )
        except Exception as e:
            print(f"后台管理WS广播任务错误: {e}")

        await asyncio.sleep(3)

def update_limit_up_pool_task():
    """更新涨停股票池"""
    global limit_up_pool_data
    try:
        # Scan
        pool = scan_limit_up_pool()
        
        enriched_pool = []
        for stock in pool:
            code = stock['code']
            
            # Calculate metrics (Historical)
            metrics = calculate_metrics(code)
            
            # Check watchlist for reason
            reason = "市场强势涨停"
            if code in watchlist_map:
                reason = watchlist_map[code].get('news_summary', '自选股涨停')
            
            stock['seal_rate'] = metrics['seal_rate']
            stock['broken_rate'] = metrics['broken_rate']
            stock['next_day_premium'] = metrics['next_day_premium']
            stock['limit_up_days'] = metrics['limit_up_days']
            stock['reason'] = reason
            # Ensure concept and associated are present (from scanner)
            if 'concept' not in stock:
                stock['concept'] = '-'
            if 'associated' not in stock:
                stock['associated'] = stock.get('concept', '-')
            
            enriched_pool.append(stock)
            
        limit_up_pool_data = enriched_pool
    except Exception as e:
        print(f"更新涨停池失败: {e}")

@app.on_event("startup")
async def startup_event():
    # Load caches
    load_analysis_cache()
    load_market_sentiment_cache()
    ensure_runtime_indexes()

    # Always start lightweight websocket log broadcaster.
    asyncio.create_task(log_broadcaster())
    asyncio.create_task(market_event_broadcaster())
    asyncio.create_task(admin_event_broadcaster())

    if not _bool_env("ENABLE_BACKGROUND_TASKS", True):
        msg = "启动：已禁用后台任务（ENABLE_BACKGROUND_TASKS=0）"
        print(msg)
        add_runtime_log(msg)
        return

    if not _acquire_background_singleton():
        msg = "启动：后台任务由其他 worker 持有，当前实例仅提供 API"
        print(msg)
        add_runtime_log(
            "[系统守护] 后台任务实例未获取到执行锁。原因: 已有其他worker在运行定时任务。处理建议: 多worker部署属正常；若为单实例部署，请结束残留进程或调整 BACKGROUND_SINGLETON_PORT。",
            level="WARNING",
        )
        return

    # Update base info only during market trading session.
    if is_market_open_day() and is_trading_time():
        print("启动：正在更新股票基础信息...")
        add_runtime_log("启动：正在更新股票基础信息...")
        await asyncio.to_thread(data_provider.update_base_info)
    else:
        print("启动：当前非交易时段，跳过基础信息更新。")
        add_runtime_log("启动：当前非交易时段，跳过基础信息更新。")

    # Base snapshot policy:
    # startup backfill when cache is missing; pre-open/post-close handled by low-frequency task.
    try:
        await asyncio.to_thread(data_provider.maybe_refresh_biying_base_snapshot, False)
        await asyncio.to_thread(refresh_market_circ_map_cache, False)
    except Exception:
        pass

    # Warm core caches once at startup.
    await asyncio.to_thread(refresh_stock_quotes_cache)
    await asyncio.to_thread(refresh_indices_cache)
    await asyncio.to_thread(refresh_market_sentiment_cache)
    with cache_lock:
        sentiment_snapshot = copy.deepcopy(market_sentiment_cache if isinstance(market_sentiment_cache, dict) else {})
    has_sentiment_breadth = _market_sentiment_stats_has_breadth((sentiment_snapshot or {}).get("stats"))
    if not has_sentiment_breadth:
        msg = "启动：市场情绪广度缓存缺失，尝试一次非交易时段快照抓取..."
        print(msg)
        add_runtime_log(msg)
        await asyncio.to_thread(refresh_market_sentiment_cache, True)

    # Start background scheduler
    asyncio.create_task(scheduler_loop())
    # Start centralized cache updater (all user APIs read these caches only)
    asyncio.create_task(realtime_cache_updater_task())
    asyncio.create_task(biying_base_snapshot_task())
    asyncio.create_task(kline_cache_updater_task())
    # Start market pool updater
    asyncio.create_task(update_market_pools_task())
    # Start fast intraday scanner
    asyncio.create_task(update_intraday_pool_task())
    # Start periodic cleanup task
    asyncio.create_task(periodic_cleanup_task())

    # 启动时检查是否需要补跑一次盘中扫描（由 last_run_time 控制，非用户触发）。
    startup_scan_msg = f"启动：后台任务已接管(pid={os.getpid()})，检查是否需要首次盘中扫描..."
    print(startup_scan_msg)
    add_runtime_log(startup_scan_msg)
    asyncio.create_task(run_initial_scan())

def cleanup_kline_cache_files(min_cache_days: int = KLINE_MIN_CACHE_EXPIRE_DAYS, day_cache_days: int = KLINE_DAY_CACHE_EXPIRE_DAYS):
    now_ts = time.time()
    min_cutoff = now_ts - max(1, int(min_cache_days)) * 86400
    day_cutoff = now_ts - max(1, int(day_cache_days)) * 86400
    removed_min = 0
    removed_day = 0

    try:
        if KLINE_DIR.exists():
            for fp in KLINE_DIR.glob("*.csv"):
                try:
                    if fp.stat().st_mtime < min_cutoff:
                        fp.unlink(missing_ok=True)
                        removed_min += 1
                except Exception:
                    continue
    except Exception:
        pass

    try:
        if DAY_KLINE_CACHE_DIR.exists():
            for fp in DAY_KLINE_CACHE_DIR.glob("*.json"):
                try:
                    if fp.stat().st_mtime < day_cutoff:
                        fp.unlink(missing_ok=True)
                        removed_day += 1
                except Exception:
                    continue
    except Exception:
        pass

    if removed_min or removed_day:
        msg = f"[Cleanup] K线缓存清理完成: 分时={removed_min}, 日K={removed_day}"
        print(msg)
        add_runtime_log(msg)


async def periodic_cleanup_task():
    """定期清理缓存文件"""
    while True:
        try:
            print("正在执行周期清理...")
            # 1. 清理 AI 分析缓存（7天）
            cleanup_analysis_cache(max_age_days=7)
            # 2. 清理 AI 原始数据缓存（7天）
            ai_cache.cleanup(max_age_seconds=7 * 86400)
            # 3. 清理过期 K 线缓存
            cleanup_kline_cache_files()
            
            # 每24小时运行一次
            await asyncio.sleep(86400)
        except Exception as e:
            print(f"清理任务错误: {e}")
            await asyncio.sleep(3600)


def _resolve_runtime_interval(now: datetime):
    """
    返回当前应使用的调度周期（秒）与模式。
    模式可能为: intraday / after_hours / none
    """
    if not SYSTEM_CONFIG.get("use_smart_schedule", True):
        interval_seconds = max(1, int(SYSTEM_CONFIG.get("fixed_interval_minutes", 60) or 60)) * 60
        if (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 15:
            mode = "intraday"
        else:
            mode = "after_hours"
        return interval_seconds, mode

    current_time_str = now.strftime("%H:%M")
    interval_seconds = 3600
    mode = "after_hours"
    for rule in SYSTEM_CONFIG.get("schedule_plan", DEFAULT_SCHEDULE):
        start = str(rule.get("start", "00:00"))
        end = str(rule.get("end", "00:00"))
        in_range = False
        if start <= end:
            if start <= current_time_str < end:
                in_range = True
        else:
            if start <= current_time_str or current_time_str < end:
                in_range = True
        if not in_range:
            continue
        mode = str(rule.get("mode", "after_hours") or "after_hours")
        interval_seconds = max(1, int(rule.get("interval", 60) or 60)) * 60
        break

    if mode == "none":
        interval_seconds = 999999
    return interval_seconds, mode


async def run_initial_scan():
    """服务启动后按持久化调度状态决定是否补跑一次扫描。"""
    try:
        # 等待几秒确保其他组件就绪
        await asyncio.sleep(2)
        # 仅在交易日且配置开启时执行初始扫描；并严格按上次运行时间判断是否到期。
        if not is_market_open_day() or not SYSTEM_CONFIG["auto_analysis_enabled"]:
            msg = "启动：跳过首次扫描（非交易日或已禁用自动分析）。"
            print(msg)
            add_runtime_log(msg)
            return

        now = datetime.now()
        interval_seconds, mode = _resolve_runtime_interval(now)
        if mode == "none":
            msg = "启动：当前时段配置为暂停，跳过首次扫描。"
            print(msg)
            add_runtime_log(msg)
            return

        now_ts = time.time()
        last_run_ts = float(SYSTEM_CONFIG.get("last_run_time", 0) or 0)
        if last_run_ts > 0:
            elapsed = max(0, now_ts - last_run_ts)
            if elapsed < interval_seconds:
                remain = int(interval_seconds - elapsed)
                msg = f"启动：距离下次分析还有 {remain}s，跳过首次扫描。"
                print(msg)
                add_runtime_log(msg)
                return

        msg = f"启动：首次扫描条件满足，开始执行（模式={mode}）。"
        print(msg)
        add_runtime_log(msg)
        await asyncio.to_thread(execute_analysis, mode)
        msg = "启动：首次扫描完成。"
        print(msg)
        add_runtime_log(msg)
        SYSTEM_CONFIG["last_run_time"] = now_ts
        save_config()
    except Exception as e:
        err = f"初始扫描错误: {e}"
        print(err)
        add_runtime_log(err, level="ERROR")


def _public_system_config():
    safe = copy.deepcopy(SYSTEM_CONFIG)
    safe.pop("api_keys", None)
    provider_cfg = safe.get("data_provider_config")
    if isinstance(provider_cfg, dict):
        provider_cfg = dict(provider_cfg)
        provider_cfg["biying_license_key"] = ""
        provider_cfg["biying_cert_path"] = ""
        safe["data_provider_config"] = provider_cfg

    # Keep email settings structure for compatibility, but never expose password.
    email_cfg = safe.get("email_config")
    if isinstance(email_cfg, dict):
        email_cfg = dict(email_cfg)
        email_cfg["smtp_password"] = ""
        safe["email_config"] = email_cfg

    return safe


def _public_client_config():
    community = SYSTEM_CONFIG.get("community_config", {}) or {}
    if not isinstance(community, dict):
        community = {}
    return {
        "auto_analysis_enabled": bool(SYSTEM_CONFIG.get("auto_analysis_enabled", True)),
        "community_config": {
            "qq_group_number": str(community.get("qq_group_number", "") or "").strip(),
            "qq_group_link": str(community.get("qq_group_link", "") or "").strip(),
            "welcome_text": str(community.get("welcome_text", "") or "").strip(),
        },
    }


@app.get("/api/config")
async def get_config(user: models.User = Depends(check_data_permission)):
    return _public_client_config()

class ConfigUpdate(BaseModel):
    auto_analysis_enabled: bool
    use_smart_schedule: bool
    fixed_interval_minutes: int
    schedule_plan: Optional[List[dict]] = None
    news_auto_clean_enabled: Optional[bool] = True
    news_auto_clean_days: Optional[int] = 14

@app.post("/api/config")
async def update_config(config: ConfigUpdate, authorized: bool = Depends(admin.verify_admin)):
    global SYSTEM_CONFIG
    SYSTEM_CONFIG["auto_analysis_enabled"] = config.auto_analysis_enabled
    SYSTEM_CONFIG["use_smart_schedule"] = config.use_smart_schedule
    SYSTEM_CONFIG["fixed_interval_minutes"] = config.fixed_interval_minutes
    if config.schedule_plan:
        SYSTEM_CONFIG["schedule_plan"] = config.schedule_plan
    
    if config.news_auto_clean_enabled is not None:
        SYSTEM_CONFIG["news_auto_clean_enabled"] = config.news_auto_clean_enabled
    if config.news_auto_clean_days is not None:
        SYSTEM_CONFIG["news_auto_clean_days"] = config.news_auto_clean_days
    
    # [Fix] Reset last_run_time to now to prevent immediate scan if interval was reduced
    # This ensures the next scan happens AFTER the interval, not immediately.
    SYSTEM_CONFIG["last_run_time"] = time.time()
    
    save_config() # Persist changes
    return {"status": "success", "config": _public_client_config()}

async def scheduler_loop():
    """Background scheduler for periodic tasks"""
    print("正在启动后台调度器...")
    last_pool_update_time = 0
    
    # Startup Check: If watchlist was updated recently (< 1 hour), skip immediate analysis
    # Check file modification time of watchlist.json
    try:
        watchlist_path = DATA_DIR / "watchlist.json"
        if watchlist_path.exists():
            mtime = watchlist_path.stat().st_mtime
            if time.time() - mtime < 3600:
                print("监控列表在1小时内刚更新，启动时跳过立即分析。")
                # Set last_run_time to mtime so scheduler thinks it just ran
                SYSTEM_CONFIG["last_run_time"] = mtime
                save_config()
    except Exception as e:
        print(f"启动检查失败: {e}")

    while True:
        try:
            current_timestamp = time.time()
            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute
            
            # Weekend Check (Saturday=5, Sunday=6)
            if not is_market_open_day():
                # Sleep longer on weekends
                await asyncio.sleep(3600)
                continue
                
            # --- Schedule Logic ---
            interval_seconds = 3600 # Default 1h
            lookback_hours = 1
            mode = "after_hours"
            
            # Reset active rule index
            SYSTEM_CONFIG["active_rule_index"] = -1
            
            if SYSTEM_CONFIG["use_smart_schedule"]:
                current_time_str = now.strftime("%H:%M")
                matched_rule = None
                
                # Default fallback
                interval_seconds = 3600
                mode = "after_hours"

                for index, rule in enumerate(SYSTEM_CONFIG.get("schedule_plan", DEFAULT_SCHEDULE)):
                    start = rule["start"]
                    end = rule["end"]
                    
                    # Check if time is in range
                    in_range = False
                    if start <= end:
                        if start <= current_time_str < end:
                            in_range = True
                    else: # Cross midnight (e.g. 23:00 to 06:00)
                        if start <= current_time_str or current_time_str < end:
                            in_range = True
                            
                    if in_range:
                        matched_rule = rule
                        SYSTEM_CONFIG["active_rule_index"] = index
                        break
                
                if matched_rule:
                    interval_seconds = matched_rule["interval"] * 60
                    mode = matched_rule["mode"]
                    if matched_rule["mode"] == "none":
                        interval_seconds = 999999
                
                lookback_hours = max(0.25, interval_seconds / 3600)

                # Special Trigger: Force run at 15:15 if last run was intraday (before 15:15)
                if current_hour == 15 and current_minute >= 15:
                    last_run_dt = datetime.fromtimestamp(SYSTEM_CONFIG["last_run_time"]) if SYSTEM_CONFIG["last_run_time"] > 0 else datetime.fromtimestamp(0)
                    # If last run was today but before 15:15
                    if last_run_dt.date() == now.date() and (last_run_dt.hour < 15 or (last_run_dt.hour == 15 and last_run_dt.minute < 15)):
                        interval_seconds = 0 # Force run
            else:
                # Manual Interval
                interval_seconds = SYSTEM_CONFIG["fixed_interval_minutes"] * 60
                lookback_hours = SYSTEM_CONFIG["fixed_interval_minutes"] / 60
                # Simple mode logic for manual
                if (current_hour > 9 or (current_hour == 9 and current_minute >= 30)) and current_hour < 15:
                    mode = "intraday"
                else:
                    mode = "after_hours"

            # Safety check: If last_run_time is in the future, reset it
            # [Moved here to ensure interval_seconds is defined]
            if SYSTEM_CONFIG["last_run_time"] > current_timestamp:
                print(f"检测到 last_run_time 在未来，已重置: {SYSTEM_CONFIG['last_run_time']} -> {current_timestamp}")
                SYSTEM_CONFIG["last_run_time"] = current_timestamp - interval_seconds # Force run if needed
                save_config()

            # Update Next Run Time for UI
            # If we just ran (last_run_time is very close to now), next run is now + interval
            # If we haven't run in a while, next run is effectively "now" (pending execution)
            if SYSTEM_CONFIG["last_run_time"] == 0:
                SYSTEM_CONFIG["next_run_time"] = current_timestamp
            else:
                # Calculate next run based on last run
                next_run = SYSTEM_CONFIG["last_run_time"] + interval_seconds
                # If next run is in the past (overdue), show it as now
                if next_run < current_timestamp:
                    SYSTEM_CONFIG["next_run_time"] = current_timestamp
                else:
                    SYSTEM_CONFIG["next_run_time"] = next_run

            # Task 1: Analysis
            if SYSTEM_CONFIG["auto_analysis_enabled"]:
                if current_timestamp - SYSTEM_CONFIG["last_run_time"] >= interval_seconds:
                    # Special check to avoid running during the 15:00-15:15 gap (only in smart mode)
                    should_run = True
                    if SYSTEM_CONFIG["use_smart_schedule"] and (current_hour == 15 and current_minute < 15):
                        should_run = False
                    
                    if should_run:
                        try:
                            mode_cn = _mode_display_name(mode)
                            SYSTEM_CONFIG["current_status"] = f"正在执行 {mode_cn}..."
                            # Update last_run_time BEFORE execution to prevent loop on error
                            SYSTEM_CONFIG["last_run_time"] = current_timestamp
                            
                            # Recalculate next run time immediately after update
                            SYSTEM_CONFIG["next_run_time"] = current_timestamp + interval_seconds
                            save_config()
                            
                            thread_logger(f">>> 触发定时分析: {mode_cn}，周期{interval_seconds/60:.0f}分钟，回溯{lookback_hours}小时")
                            await asyncio.to_thread(execute_analysis, mode, lookback_hours)
                        except Exception as e:
                            print(f"调度器错误: {e}")
                        finally:
                            SYSTEM_CONFIG["current_status"] = "空闲中"
            else:
                SYSTEM_CONFIG["current_status"] = "已暂停"
            
            # Task 2: Refresh Quotes (Every 3 seconds)
            # Only during trading hours or shortly after
            if now.weekday() < 5 and (9 <= current_hour < 16):
                try:
                    stocks = await asyncio.to_thread(get_stock_quotes)
                except Exception as e:
                    pass
                
            # Task 3: Update Limit Up Pool (Every 30 seconds)
            # [Fix] Disabled to avoid conflict with update_market_pools_task
            # if current_timestamp - last_pool_update_time >= 30:
            #     # Only during trading hours
            #     if is_trading_time():
            #         await asyncio.to_thread(update_limit_up_pool_task)
            #         last_pool_update_time = current_timestamp

            # Task 4: LHB Sync (Daily at configured time)
            sync_time = str((lhb_manager.config or {}).get("sync_time") or "18:00").strip()
            try:
                sync_hour, sync_min = [int(x) for x in sync_time.split(":")]
            except Exception:
                sync_hour, sync_min = 18, 0
            if is_market_open_day() and now.hour == sync_hour and now.minute == sync_min and now.second < 10:
                if lhb_manager.config.get('enabled') and not lhb_manager.is_syncing:
                    if not lhb_manager.has_data_for_today():
                        thread_logger(f"[龙虎榜] 启动定时同步任务 ({sync_hour:02d}:{sync_min:02d})...")
                        loop = asyncio.get_event_loop()
                        loop.run_in_executor(None, lhb_manager.sync_and_preanalyze, thread_logger)
                    else:
                        thread_logger("[龙虎榜] 今日数据已存在，跳过定时任务。")
                    await asyncio.sleep(60)

            await asyncio.sleep(5) # Check every 5 seconds
            
        except Exception as e:
            print(f"调度循环崩溃: {e}")
            await asyncio.sleep(60) # Sleep and retry

def thread_logger(msg):
    """线程安全 logger。"""
    add_runtime_log(msg)
    log_queue.put(msg)


def _mode_display_name(mode: str) -> str:
    value = str(mode or "").strip().lower()
    if value == "after_hours":
        return "盘后复盘"
    if value in {"intraday", "intraday_monitor"}:
        return "盘中突击"
    if value == "none":
        return "暂停"
    return value or "未知模式"


def _is_user_visible_analysis_log(line: str, mode_cn: str = "") -> bool:
    text = _normalize_runtime_log_for_replay(line)
    if not text:
        return False

    blocked = (
        "后台任务由其他 worker 持有",
        "ENABLE_BACKGROUND_TASKS",
        "启动：",
        "系统守护",
        "回放日志",
    )
    if any(key in text for key in blocked):
        return False

    allowed_common = (
        "调用 AI 分析",
        "挖掘目标",
        "开始执行",
        "任务完成",
        "列表已更新",
        "任务已受理",
    )
    if any(key in text for key in allowed_common):
        return True

    mode_text = str(mode_cn or "").strip()
    if mode_text and mode_text in text and "分析" in text:
        return True
    return False


def _replay_recent_analysis_logs(mode_cn: str, limit: int = 2000) -> int:
    try:
        logs = get_runtime_logs(limit=max(20, int(limit)))
    except Exception:
        return 0

    selected = [line for line in logs if _is_user_visible_analysis_log(line, mode_cn=mode_cn)]
    if not selected:
        return 0

    def _parse_line_meta(raw_line: str) -> tuple[Optional[datetime], str]:
        text = str(raw_line or "").strip()
        m = re.match(
            r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*\[([^\]]+)\]\s*",
            text,
        )
        if not m:
            return None, "INFO"
        try:
            parsed_dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            parsed_dt = None
        level = str(m.group(2) or "").strip().upper() or "INFO"
        return parsed_dt, level

    replayed = 0
    replay_items_raw = selected[-80:]
    replay_items = []
    for line in replay_items_raw:
        text = _normalize_runtime_log_for_replay(line)
        if not text:
            continue
        src_dt, src_level = _parse_line_meta(line)
        replay_items.append({
            "src_dt": src_dt,
            "level": src_level,
            "text": text,
        })
    if not replay_items:
        return 0

    # Shift historical timeline to "now": first line => now, following lines keep original spacing.
    replay_base = datetime.now()
    first_src_dt = next((x["src_dt"] for x in replay_items if x["src_dt"] is not None), None)
    last_replay_dt = replay_base
    last_src_dt = first_src_dt or replay_base

    for idx, item in enumerate(replay_items):
        src_dt = item["src_dt"] or last_src_dt
        last_src_dt = src_dt
        if first_src_dt is None:
            replay_dt = last_replay_dt if idx > 0 else replay_base
        else:
            delta_seconds = int((src_dt - first_src_dt).total_seconds())
            if delta_seconds < 0:
                delta_seconds = 0
            replay_dt = replay_base + timedelta(seconds=delta_seconds)
            if idx > 0 and replay_dt <= last_replay_dt:
                replay_dt = last_replay_dt + timedelta(seconds=1)
        last_replay_dt = replay_dt

        level = str(item["level"] or "INFO").strip().upper() or "INFO"
        replay_text = str(item["text"] or "").strip()
        if not replay_text:
            continue

        # Replay only to websocket stream; do not write back into runtime cache to avoid duplicate nesting.
        replay_line = f"[{replay_dt.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {replay_text}"
        try:
            log_queue.put(replay_line)
        except Exception:
            thread_logger(replay_line)
        replayed += 1
    return replayed


def _normalize_runtime_log_for_replay(line: str) -> str:
    text = str(line or "").strip()
    if not text:
        return ""
    # Strip nested runtime prefixes repeatedly (e.g. [10:31:03] [2026-...][INFO] ...).
    for _ in range(4):
        prev = text
        text = re.sub(r"^\[[0-9:\- ]+\]\s*\[[^\]]+\]\s*", "", text).strip()
        text = re.sub(r"^\[[0-9]{2}:[0-9]{2}:[0-9]{2}\]\s*(\[[^\]]+\]\s*)?", "", text).strip()
        if text == prev:
            break
    text = re.sub(r"^\[(INFO|ERROR|WARN|WARNING|DEBUG|TRACE)\]\s*", "", text, flags=re.IGNORECASE).strip()
    return text


def _recent_analysis_log_lines(limit: int = 120) -> list[str]:
    try:
        logs = get_runtime_logs(limit=max(20, int(limit)))
    except Exception:
        return []

    selected = [line for line in logs if _is_user_visible_analysis_log(line)]
    if not selected:
        return []
    return [str(line) for line in selected[-100:]]

# Global Cache Timer
ANALYSIS_REUSE_SECONDS_INTRADAY = 15 * 60
ANALYSIS_REUSE_SECONDS_AFTER_HOURS = 60 * 60
LAST_ANALYSIS_TIME = {
    "mid_day": datetime.min,
    "after_hours": datetime.min
}
ANALYSIS_TRIGGER_LOCKS = {
    "mid_day": asyncio.Lock(),
    "after_hours": asyncio.Lock()
}

@app.post("/api/analyze")
async def run_analysis(
    background_tasks: BackgroundTasks, 
    mode: str = Query("after_hours"), 
    user: models.User = Depends(get_current_user), 
    db: Session = Depends(lambda: next(database.get_db()))
):
    """触发复盘分析"""
    mode_cn = _mode_display_name(mode)
    
    # 1. 权限检查与扣费
    limit_type = 'raid' if mode in ["intraday", "intraday_monitor"] else 'review'
    
    if limit_type == 'raid':
        await check_raid_permission(user, skip_quota=True)
    else:
        await check_review_permission(user, skip_quota=True)
    
    # 2. 复用检查（时段互斥 + 冷却窗口）+ 并发互斥
    cache_key = "mid_day" if limit_type == 'raid' else "after_hours"
    lock = ANALYSIS_TRIGGER_LOCKS[cache_key]
    in_trading_session = bool(is_market_open_day() and is_trading_time())
    force_reuse = (limit_type == 'raid' and not in_trading_session) or (limit_type != 'raid' and in_trading_session)
    cooldown_seconds = ANALYSIS_REUSE_SECONDS_INTRADAY if limit_type == 'raid' else ANALYSIS_REUSE_SECONDS_AFTER_HOURS
    async with lock:
        last_time = LAST_ANALYSIS_TIME.get(cache_key, datetime.min)
        seconds_since_last = int((datetime.now() - last_time).total_seconds())
        if force_reuse or seconds_since_last < cooldown_seconds:
            try:
                refresh_watchlist()
                reload_watchlist_globals()
            except Exception:
                pass
            replayed = _replay_recent_analysis_logs(mode_cn)
            if replayed <= 0:
                thread_logger(f"[分析] {mode_cn}任务已受理，正在准备最新数据...")
            return {
                "status": "success",
                "message": f"{mode_cn}任务已在后台启动"
            }

        # 3. 执行新的分析
        if limit_type == 'raid':
            await check_raid_permission(user)
        else:
            await check_review_permission(user)
        user_service.consume_quota(db, user, limit_type)
        LAST_ANALYSIS_TIME[cache_key] = datetime.now()
    
    thread_logger(f"[分析] {mode_cn}任务已受理，正在准备最新数据...")

    # Run in background to avoid blocking
    background_tasks.add_task(execute_analysis, mode)
    
    return {"status": "success", "message": f"{mode_cn}任务已在后台启动"}

def execute_analysis(mode="after_hours", hours=None):
    try:
        mode_name = _mode_display_name(mode)
        thread_logger(f">>> 开始执行{mode_name}任务 (回溯{hours if hours else '默认'}小时)...")
        
        # Clean watchlist before analysis (remove old/irrelevant)
        clean_watchlist()
        
        generate_watchlist(logger=thread_logger, mode=mode, hours=hours, update_callback=refresh_watchlist)
        refresh_watchlist()
        # Reload globals so /api/stocks returns new data
        reload_watchlist_globals()
        cache_key = "mid_day" if str(mode or "").strip().lower() in {"intraday", "intraday_monitor"} else "after_hours"
        LAST_ANALYSIS_TIME[cache_key] = datetime.now()
        thread_logger(f">>> {mode_name}任务完成，列表已更新 ({len(WATCH_LIST)} 个标的)。")
    except Exception as e:
        thread_logger(f"!!! 分析任务出错: {e}")
        print(f"分析错误: {e}")


def refresh_stock_quotes_cache():
    """
    获取股票行情，使用统一的 DataProvider
    """
    if not WATCH_LIST:
        _set_stock_quotes_cache([])
        return []
        
    try:
        # Fetch raw quotes
        raw_stocks = data_provider.fetch_quotes(WATCH_LIST)
        
        def _norm_code(value: str) -> str:
            return _normalize_market_code(value)

        # Build quick maps for enrichment: /api/stocks should inherit seats/flow value from pool scanners.
        limit_up_map = {}
        for s in (limit_up_pool_data or []):
            raw_code = s.get("code")
            if not raw_code:
                continue
            limit_up_map[str(raw_code)] = s
            norm_code = _norm_code(raw_code)
            if norm_code:
                limit_up_map[norm_code] = s

        intraday_map = {}
        for s in (intraday_pool_data or []):
            raw_code = s.get("code")
            if not raw_code:
                continue
            intraday_map[str(raw_code)] = s
            norm_code = _norm_code(raw_code)
            if norm_code:
                intraday_map[norm_code] = s
        market_map = {}
        try:
            ensure_market_circ_map_cache(allow_network=False)
            market_map = _get_market_circ_map_cache()
        except Exception:
            market_map = {}
        
        # Enrich with strategy info
        enriched_stocks = []
        for stock in raw_stocks:
            raw_code = stock.get("code", "")
            norm_code = _norm_code(raw_code)
            code = norm_code or raw_code
            if norm_code:
                stock["code"] = norm_code
            
            stock['is_favorite'] = False
            ai_strategy = "Neutral"
            
            # 1. Check AI Watchlist for strategy & info
            if code in watchlist_map:
                ai_info = watchlist_map[code]
                ai_strategy = ai_info.get("strategy_type", "Neutral")
                
                # Use AI info as base
                stock['initial_score'] = ai_info.get("initial_score", 0)
                stock['concept'] = ai_info.get("concept", stock.get('concept', '-'))
                # 兼容 reason 和 news_summary
                ai_curr_reason = ai_info.get("reason", ai_info.get("news_summary", ""))
                stock['reason'] = ai_curr_reason if ai_curr_reason else stock.get('reason', '')
                stock['news_summary'] = stock['reason'] # 确保前端能读取到详细逻辑
                
                stock['seal_rate'] = ai_info.get("seal_rate", 0)
                stock['broken_rate'] = ai_info.get("broken_rate", 0)
                stock['next_day_premium'] = ai_info.get("next_day_premium", 0)
                stock['limit_up_days'] = ai_info.get("limit_up_days", 0)
                stock['added_time'] = ai_info.get("added_time", 0)

                # Check for "Resurrection" (Weak to Strong)
                if ai_strategy == "Discarded":
                    # Determine limit threshold (10% or 20%)
                    clean_code = code.replace('sz', '').replace('sh', '').replace('bj', '')
                    is_20cm = clean_code.startswith('30') or clean_code.startswith('68')
                    limit_threshold = 19.5 if is_20cm else 9.5
                    
                    current_change = stock.get('change_percent', 0)
                    
                    if current_change >= limit_threshold:
                        ai_strategy = "LimitUp" # Promote to LimitUp view
                        # Prepend reason if not already there
                        if "[弱转强]" not in stock['reason']:
                            stock['reason'] = f"[弱转强] {stock['reason']}"

            # 2. Check Favorites
            if code in favorites_map:
                fav_info = favorites_map[code]
                stock['is_favorite'] = True
                
                # If NOT in AI list, use Favorite info
                if code not in watchlist_map:
                    stock['concept'] = fav_info.get("concept", stock.get('concept', '-'))
                    stock['reason'] = fav_info.get("reason", "手动添加")
                    stock['initial_score'] = fav_info.get("initial_score", 0)
                    stock['added_time'] = fav_info.get("added_time", 0)
                    # Keep other metrics 0 or default
            
            # Set strategy to AI strategy (so it appears in AI columns)
            # Frontend will handle 'is_favorite' for Manual column
            stock['strategy'] = ai_strategy

            # Enrich likely seats and circulation value from intraday / limit-up pools.
            seat_src = (
                intraday_map.get(code)
                or intraday_map.get(raw_code)
                or limit_up_map.get(code)
                or limit_up_map.get(raw_code)
            )
            if seat_src:
                if stock.get("strategy") == "LimitUp" and seat_src.get("likely_seats"):
                    stock["likely_seats"] = seat_src.get("likely_seats")
                elif stock.get("strategy") != "LimitUp":
                    stock.pop("likely_seats", None)
                if (not stock.get("circulation_value")) and seat_src.get("circulation_value"):
                    stock["circulation_value"] = seat_src.get("circulation_value")
                if not stock.get("concept") and seat_src.get("concept"):
                    stock["concept"] = seat_src.get("concept")
            elif stock.get("strategy") != "LimitUp":
                stock.pop("likely_seats", None)

            # Final fallback for circulation value from all-market snapshot.
            if not stock.get("circulation_value"):
                digits = "".join(filter(str.isdigit, code))
                circ_mv = (
                    market_map.get(code, 0)
                    or market_map.get(raw_code, 0)
                    or market_map.get(digits, 0)
                )
                if circ_mv:
                    stock["circulation_value"] = circ_mv
            
            enriched_stocks.append(stock)
            
        _set_stock_quotes_cache(enriched_stocks)
        return enriched_stocks
    except Exception as e:
        print(f"获取行情失败: {e}")
        return []

def get_stock_quotes():
    """Return cached quotes only (no network)."""
    return _get_stock_quotes_cache()


def ensure_stock_quotes_cache(max_age_sec: int = max(30, REALTIME_CACHE_INTERVAL_SEC * 2)):
    """
    Multi-worker safety: if current worker cache is empty/stale, refresh on demand.
    """
    now_ts = time.time()
    with cache_lock:
        has_rows = bool(stock_quotes_cache)
        cache_age = (now_ts - stock_quotes_cache_ts) if stock_quotes_cache_ts > 0 else float("inf")
    if has_rows and cache_age <= max_age_sec:
        return

    with stock_quotes_refresh_guard:
        now_ts = time.time()
        with cache_lock:
            has_rows = bool(stock_quotes_cache)
            cache_age = (now_ts - stock_quotes_cache_ts) if stock_quotes_cache_ts > 0 else float("inf")
        if has_rows and cache_age <= max_age_sec:
            return
        if not WATCH_LIST:
            reload_watchlist_globals()
        refresh_stock_quotes_cache()


def _project_rows(rows, selected_fields: set) -> list:
    if not isinstance(rows, list):
        return []
    output = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {}
        for key in selected_fields:
            if key in row:
                item[key] = row.get(key)
        if "code" not in item and "code" in row:
            item["code"] = row.get("code")
        output.append(item)
    return output


def _resolve_selected_fields(fields_text: str, default_fields: tuple) -> set:
    text = str(fields_text or "").strip()
    if not text:
        return set(default_fields)
    picked = {str(x).strip() for x in text.split(",") if str(x).strip()}
    if "code" not in picked:
        picked.add("code")
    return picked

@app.post("/api/watchlist/remove")
async def remove_from_watchlist(request: Request, authorized: bool = Depends(admin.verify_admin)):
    """从自选列表中移除股票"""
    global watchlist_data, watchlist_map, WATCH_LIST, favorites_data, favorites_map
    try:
        data = await request.json()
        code = data.get("code")
        list_type = data.get("type", "all") # 'favorite', 'ai', or 'all' (default for backward compat)
        
        removed = False
        if code:
            # Remove from favorites
            if (list_type == 'favorite' or list_type == 'all') and code in favorites_map:
                favorites_data = [item for item in favorites_data if item['code'] != code]
                save_favorites(favorites_data)
                removed = True
                
            # Remove from watchlist
            if (list_type == 'ai' or list_type == 'all') and code in watchlist_map:
                watchlist_data = [item for item in watchlist_data if item['code'] != code]
                save_watchlist(watchlist_data)
                removed = True
                
            if removed:
                reload_watchlist_globals()
                return {"status": "success", "message": f"Removed {code}"}
                
        return {"status": "error", "message": "Stock not found"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/stocks")
async def api_stocks(
    request: Request,
    lite: bool = Query(False),
    fields: str = Query(""),
    user: models.User = Depends(check_data_permission),
):
    await asyncio.to_thread(ensure_stock_quotes_cache)
    payload = get_stock_quotes()
    if lite or fields:
        selected = _resolve_selected_fields(
            fields,
            (
                "code",
                "name",
                "current",
                "change_percent",
                "strategy",
                "is_favorite",
                "added_time",
                "concept",
                "turnover",
                "circulation_value",
                "time",
                "likely_seats",
                "seal_rate",
                "broken_rate",
                "next_day_premium",
                "limit_up_days",
            ),
        )
        payload = _project_rows(payload, selected)
    etag = _json_etag(payload)
    if _is_not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=payload, headers={"ETag": etag, "Cache-Control": "private, max-age=1"})

@app.get("/api/indices")
async def api_indices(
    request: Request,
    lite: bool = Query(False),
    fields: str = Query(""),
    user: models.User = Depends(check_data_permission),
):
    """快速获取大盘指数"""
    await asyncio.to_thread(ensure_indices_cache)
    payload = get_indices_cache()
    if lite or fields:
        selected = _resolve_selected_fields(
            fields,
            (
                "name",
                "current",
                "change",
                "amount",
                "time",
            ),
        )
        payload = _project_rows(payload, selected)
    etag = _json_etag(payload)
    if _is_not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=payload, headers={"ETag": etag, "Cache-Control": "private, max-age=3"})

@app.get("/api/limit_up_pool")
async def api_limit_up_pool(
    request: Request,
    lite: bool = Query(False),
    fields: str = Query(""),
    user: models.User = Depends(check_data_permission),
):
    payload = {
        "limit_up": limit_up_pool_data,
        "broken": broken_limit_pool_data
    }
    if lite or fields:
        selected = _resolve_selected_fields(
            fields,
            (
                "code",
                "name",
                "current",
                "change_percent",
                "turnover",
                "circulation_value",
                "concept",
                "reason",
                "time",
                "likely_seats",
                "seal_rate",
                "broken_rate",
                "next_day_premium",
                "limit_up_days",
            ),
        )
        payload = {
            "limit_up": _project_rows(payload.get("limit_up", []), selected),
            "broken": _project_rows(payload.get("broken", []), selected),
        }
    etag = _json_etag(payload)
    if _is_not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=payload, headers={"ETag": etag, "Cache-Control": "private, max-age=2"})

@app.get("/api/intraday_pool")
async def api_intraday_pool(user: models.User = Depends(check_data_permission)):
    """直接获取盘中打板扫描结果（优先返回缓存）"""
    return intraday_pool_data or []

@app.get("/api/market_sentiment")
async def api_market_sentiment(request: Request, user: models.User = Depends(check_data_permission)):
    """获取大盘情绪数据"""
    await asyncio.to_thread(ensure_market_sentiment_cache)
    payload = get_market_sentiment_cache()
    etag = _json_etag(payload)
    if _is_not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=payload, headers={"ETag": etag, "Cache-Control": "private, max-age=3"})

class StockAnalysisRequest(BaseModel):
    code: str
    name: str
    current: float
    change_percent: float
    concept: str = ""
    turnover: Optional[float] = None
    circulation_value: Optional[float] = None
    metrics: dict = {}
    promptType: str = "default"
    force: bool = False # Force re-analysis
    apiKey: Optional[str] = None # Optional API Key for standalone mode


def _needs_analysis_market_backfill(stock_data: dict) -> bool:
    return (
        _safe_float_number(stock_data.get("current")) <= 0
        or _safe_float_number(stock_data.get("turnover")) <= 0
        or _safe_float_number(stock_data.get("circulation_value")) <= 0
    )


def _apply_quote_row_to_analysis_stock(stock_data: dict, row: dict) -> None:
    if not isinstance(row, dict):
        return
    row_current = _safe_float_number(row.get("current"))
    row_change = _safe_float_number(row.get("change_percent"))
    row_turnover = _safe_float_number(row.get("turnover"))
    row_circ = _safe_float_number(row.get("circulation_value"))

    if _safe_float_number(stock_data.get("current")) <= 0 and row_current > 0:
        stock_data["current"] = row_current
    if _safe_float_number(stock_data.get("change_percent")) == 0 and row_change != 0:
        stock_data["change_percent"] = row_change
    if _safe_float_number(stock_data.get("turnover")) <= 0 and row_turnover > 0:
        stock_data["turnover"] = row_turnover
    if _safe_float_number(stock_data.get("circulation_value")) <= 0 and row_circ > 0:
        stock_data["circulation_value"] = row_circ

    if not str(stock_data.get("name", "")).strip():
        stock_data["name"] = str(row.get("name", "")).strip()
    if not str(stock_data.get("concept", "")).strip() and str(row.get("concept", "")).strip():
        stock_data["concept"] = str(row.get("concept", "")).strip()


def _find_quote_row_by_code(raw_code: str) -> Optional[dict]:
    req_text = str(raw_code or "").strip().lower()
    req_norm = normalize_stock_code(req_text)
    req_digits = "".join(filter(str.isdigit, req_text))
    req_norm_digits = "".join(filter(str.isdigit, req_norm))

    for row in _get_stock_quotes_cache():
        row_code = normalize_stock_code(str(row.get("code", "")))
        row_digits = "".join(filter(str.isdigit, row_code))
        if req_norm and row_code == req_norm:
            return row
        if req_text and row_code == req_text:
            return row
        if req_norm_digits and row_digits == req_norm_digits:
            return row
        if req_digits and row_digits == req_digits:
            return row
    return None


def _hydrate_analysis_stock_data(stock_data: dict, raw_code: str) -> None:
    req_text = str(raw_code or stock_data.get("code") or "").strip()
    req_norm = normalize_stock_code(req_text)
    req_digits = "".join(filter(str.isdigit, req_text))
    req_norm_digits = "".join(filter(str.isdigit, req_norm))
    stock_data["code"] = req_norm or req_text

    row = _find_quote_row_by_code(req_text)
    if row:
        _apply_quote_row_to_analysis_stock(stock_data, row)

    if _needs_analysis_market_backfill(stock_data):
        try:
            ensure_stock_quotes_cache()
        except Exception:
            pass
        row = _find_quote_row_by_code(req_text)
        if row:
            _apply_quote_row_to_analysis_stock(stock_data, row)

    if _needs_analysis_market_backfill(stock_data):
        query_code = req_norm or req_text
        if query_code:
            try:
                fetched = data_provider.fetch_quotes([query_code]) or []
                if fetched:
                    _apply_quote_row_to_analysis_stock(stock_data, fetched[0])
            except Exception:
                pass

    if _safe_float_number(stock_data.get("circulation_value")) <= 0:
        try:
            ensure_market_circ_map_cache(allow_network=False)
            market_map = _get_market_circ_map_cache() or {}
            circ_mv = _safe_float_number(
                market_map.get(req_norm, 0)
                or market_map.get(req_text, 0)
                or market_map.get(req_norm_digits, 0)
                or market_map.get(req_digits, 0)
            )
            if circ_mv > 0:
                stock_data["circulation_value"] = circ_mv
        except Exception:
            pass

@app.post("/api/analyze_stock")
async def api_analyze_stock(
    request: StockAnalysisRequest,
    http_request: Request,
    user: models.User = Depends(get_current_user),
    db = Depends(lambda: next(database.get_db())),
):
    """
    调用 AI 分析单个股票（支持缓存）
    """
    await check_ai_permission(user)
    stock_data = request.dict()
    api_key = stock_data.get('apiKey')
    code = stock_data.get('code')
    await asyncio.to_thread(_hydrate_analysis_stock_data, stock_data, code)
    code = stock_data.get("code")
    force = stock_data.get('force', False)
    prompt_type = request.promptType

    def _consume_and_log(cached: Optional[bool], real_call: Optional[bool]):
        user_service.consume_quota(db, user, 'ai')
        _log_ai_quota_event(
            http_request,
            user,
            feature="个股AI分析",
            source="single_stock",
            cached=cached,
            real_call=real_call,
            extra={
                "code": str(code or ""),
                "prompt_type": str(prompt_type or ""),
            },
        )

    # Construct composite cache key
    cache_key = f"{code}_{prompt_type}"

    # Fast path: return cache if valid.
    cached_content = _get_valid_analysis_content(cache_key, prompt_type, force=force)
    if cached_content is not None:
        _consume_and_log(cached=True, real_call=False)
        return {"status": "success", "analysis": cached_content}

    # Single-flight guard for high concurrency: one cache key => one live AI request.
    analysis_lock = _get_analysis_lock(cache_key)
    async with analysis_lock:
        cached_content = _get_valid_analysis_content(cache_key, prompt_type, force=force)
        if cached_content is not None:
            _consume_and_log(cached=True, real_call=False)
            return {"status": "success", "analysis": cached_content}

        _consume_and_log(cached=False, real_call=True)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: analyze_single_stock(stock_data, prompt_type=prompt_type, api_key=api_key),
        )

        if result and not result.startswith("分析失败"):
            ANALYSIS_CACHE[cache_key] = {
                "content": result,
                "timestamp": time.time()
            }
            save_analysis_cache()

        return {"status": "success", "analysis": result}

# --- LHB API ---
class LHBConfigRequest(BaseModel):
    enabled: bool
    days: int
    min_amount: int
    sync_time: Optional[str] = None

@app.get("/api/lhb/config")
async def get_lhb_config(authorized: bool = Depends(admin.verify_admin)):
    lhb_manager.load_config()
    raw_cfg = dict(lhb_manager.config or {})
    try:
        dates = lhb_manager.get_available_dates() or []
        latest_trade_date = dates[0] if dates else ""
    except Exception:
        latest_trade_date = ""
    status = lhb_manager.get_today_sync_status()
    try:
        days = int(raw_cfg.get("days", 0) or 0)
    except Exception:
        days = 0
    try:
        min_amount = int(raw_cfg.get("min_amount", 0) or 0)
    except Exception:
        min_amount = 0
    return {
        "enabled": bool(raw_cfg.get("enabled", False)),
        "days": days,
        "min_amount": min_amount,
        "sync_time": str(raw_cfg.get("sync_time", "18:00") or "18:00"),
        "last_update_time": str(raw_cfg.get("last_update", "") or ""),
        "latest_trade_date": latest_trade_date,
        "today_sync_status": status,
    }

@app.post("/api/lhb/config")
async def update_lhb_config(config: LHBConfigRequest, authorized: bool = Depends(admin.verify_admin)):
    lhb_manager.update_settings(config.enabled, config.days, config.min_amount, config.sync_time)
    payload = await get_lhb_config()
    return {"status": "ok", "config": payload}

@app.post("/api/lhb/sync")
async def sync_lhb_data(background_tasks: BackgroundTasks, days: Optional[int] = Query(None), authorized: bool = Depends(admin.verify_admin)):
    """Trigger LHB sync in background"""
    if lhb_manager.is_syncing:
        return {"status": "error", "message": "同步任务正在进行中"}
        
    background_tasks.add_task(lhb_manager.sync_and_preanalyze, logger=thread_logger, force_days=days)
    return {"status": "ok", "message": "龙虎榜同步已启动"}

@app.get("/api/lhb/status")
async def get_lhb_status(user: models.User = Depends(check_data_permission)):
    now = datetime.now()
    sync_time = lhb_manager._get_sync_time()
    sync_dt = lhb_manager._get_sync_datetime_for_date(now.date())
    before_sync_time = now < sync_dt
    today_has_data = lhb_manager.has_data_for_today()
    today_status = {
        "today": now.strftime("%Y-%m-%d"),
        "sync_time": sync_time,
        "today_has_data": bool(today_has_data),
        "before_sync_time": bool(before_sync_time),
        "message": f"今日龙虎榜数据预计 {sync_time} 后同步，请稍后查看。" if (before_sync_time and not today_has_data) else "",
    }
    return {
        "is_syncing": lhb_manager.is_syncing,
        "sync_time": today_status.get("sync_time", "18:00"),
        "today": today_status.get("today"),
        "today_has_data": bool(today_status.get("today_has_data")),
        "before_sync_time": bool(today_status.get("before_sync_time")),
        "message": today_status.get("message", ""),
    }


def _resolve_lhb_default_date(status: dict, dates: list) -> str:
    dates_list = [str(x or "").strip() for x in (dates or []) if str(x or "").strip()]
    today = str((status or {}).get("today") or "").strip()
    before_sync_time = bool((status or {}).get("before_sync_time"))
    today_has_data = bool((status or {}).get("today_has_data"))

    if today_has_data and today:
        return today
    if before_sync_time:
        for d in dates_list:
            if d != today:
                return d
    if today and today in dates_list:
        return today
    if dates_list:
        return dates_list[0]
    return today


@app.get("/api/lhb/bootstrap")
async def get_lhb_bootstrap(
    date: Optional[str] = Query(None),
    user: models.User = Depends(check_data_permission),
):
    status = lhb_manager.get_today_sync_status()
    dates = lhb_manager.get_available_dates() or []
    today = str(status.get("today") or "")
    now = datetime.now()
    if is_market_open_day() and now.hour >= 15 and today and today not in dates:
        dates = [today] + dates

    requested_date = str(date or "").strip()
    selected_date = requested_date or _resolve_lhb_default_date(status, dates)

    history = []
    if selected_date:
        history = lhb_manager.get_daily_data(selected_date) or []

    return {
        "status": {
            "is_syncing": lhb_manager.is_syncing,
            "sync_time": str(status.get("sync_time") or "18:00"),
            "today": today,
            "today_has_data": bool(status.get("today_has_data")),
            "before_sync_time": bool(status.get("before_sync_time")),
            "message": str(status.get("message") or ""),
        },
        "dates": dates,
        "selected_date": selected_date,
        "history": history,
    }

@app.get("/api/lhb/dates")
async def get_lhb_dates(user: models.User = Depends(check_data_permission)):
    dates = lhb_manager.get_available_dates() or []
    status = lhb_manager.get_today_sync_status()
    today = str(status.get("today") or "")
    now = datetime.now()
    if is_market_open_day() and now.hour >= 15 and today and today not in dates:
        dates = [today] + dates
    return dates

@app.get("/api/lhb/history")
async def get_lhb_history(date: str, user: models.User = Depends(check_data_permission)):
    date_str = str(date or "").strip()
    data = lhb_manager.get_daily_data(date_str)
    status = lhb_manager.get_today_sync_status()
    if (
        date_str
        and date_str == str(status.get("today") or "")
        and not data
        and bool(status.get("before_sync_time"))
    ):
        raise HTTPException(status_code=425, detail=status.get("message") or "今日龙虎榜尚未同步完成，请稍后再来查看。")
    return data

class LHBAnalyzeRequest(BaseModel):
    date: str
    force: bool = False

@app.post("/api/lhb/analyze")
@app.post("/api/lhb/analyze_daily")
async def analyze_lhb_daily_api(
    req: LHBAnalyzeRequest,
    http_request: Request,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Run in thread pool
    # 深度复盘仍需高级权限，但消耗 AI 次数
    await check_review_permission(user, skip_quota=True)
    await check_ai_permission(user)
    cache_key = f"lhb_daily_analysis_{req.date}"
    cached_result = None
    if not req.force:
        cached_result = ai_cache.get(cache_key)
    user_service.consume_quota(db, user, 'ai')
    _log_ai_quota_event(
        http_request,
        user,
        feature="龙虎榜AI复盘",
        source="lhb_daily_review",
        cached=bool(cached_result),
        real_call=False if cached_result else True,
        extra={
            "date": str(req.date or ""),
            "force": bool(req.force),
        },
    )

    if cached_result:
        return {"status": "ok", "result": cached_result, "analysis": cached_result}
    
    loop = asyncio.get_event_loop()
    # Fetch data first
    data = lhb_manager.get_daily_data(req.date)
    result = await loop.run_in_executor(None, lambda: analyze_daily_lhb(req.date, data, force_update=req.force))
    return {"status": "ok", "result": result, "analysis": result}

@app.get("/api/lhb/analysis")
async def get_lhb_analysis_api(date: str, user: models.User = Depends(get_current_user)):
    """获取已有的 AI 复盘结果（如有）"""
    await check_review_permission(user)
    cache_key = f"lhb_daily_analysis_{date}"
    cached = ai_cache.get(cache_key)
    return {"status": "ok", "analysis": cached}

@app.get("/api/data/backup")
async def download_data_backup(authorized: bool = Depends(admin.verify_admin)):
    """
    Pack 'data' directory into a zip file and return for download
    """
    try:
        # Create a zip file in a temporary location or memory?
        # shutil.make_archive creates a file on disk.
        # We can create it in parent dir, stream it, then delete it.
        
        base_name = BASE_DIR / "backup_data"
        root_dir = BASE_DIR / "data"
        
        if not root_dir.exists():
            return {"status": "error", "message": "Data directory not found"}
            
        # This creates backup_data.zip
        zip_path = shutil.make_archive(str(base_name), 'zip', str(root_dir))
        
        filename = f"sniper_data_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        
        def iterfile():
            with open(zip_path, mode="rb") as file_like:
                yield from file_like
            # Clean up after streaming?
            # StreamingResponse background task can handle cleanup
            os.remove(zip_path)

        return StreamingResponse(
            iterfile(),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Trigger reload for metrics update

class AnalyzeRequest(BaseModel):
    code: str
    name: str
    turnover: Optional[float] = None
    circulation_value: Optional[float] = None
    promptType: str = "normal"
    force: bool = False
    kline_data: Optional[List] = None

@app.post("/api/lhb/fetch")
async def fetch_lhb_data(background_tasks: BackgroundTasks, authorized: bool = Depends(admin.verify_admin)):
    """手动触发龙虎榜数据抓取"""
    if lhb_manager.is_syncing:
        return {"status": "error", "message": "同步任务正在进行中，请稍后再试"}
    
    # [Modified] Use thread_logger to broadcast logs to WebSocket
    # Force sync only 1 day (Today/Latest) for manual trigger
    background_tasks.add_task(lhb_manager.sync_and_preanalyze, logger=thread_logger, force_days=1)
    return {"status": "success"}

@app.post("/api/analyze/stock")
@app.post("/api/stock/analyze")
async def analyze_stock_manual(
    req: AnalyzeRequest,
    http_request: Request,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """手动触发个股 AI 分析"""
    normalized_code = normalize_stock_code(req.code) or str(req.code or "").strip()
    cache_key = f"stock_analysis_{normalized_code}_{req.promptType}"
    cached_hit = False
    if not req.force:
        try:
            last_ts = int(ai_cache.get_timestamp(cache_key) or 0)
            if last_ts > 0:
                elapsed = int(time.time()) - last_ts
                if elapsed < 600 and ai_cache.get(cache_key):
                    cached_hit = True
        except Exception:
            cached_hit = False

    # 扣除次数
    await check_ai_permission(user)
    user_service.consume_quota(db, user, 'ai')
    _log_ai_quota_event(
        http_request,
        user,
        feature="个股手动分析",
        source="single_stock",
        cached=cached_hit,
        real_call=False if cached_hit else True,
        extra={
            "code": str(req.code or ""),
            "prompt_type": str(req.promptType or ""),
            "force": bool(req.force),
        },
    )
    
    stock_data = {
        "code": normalized_code,
        "name": req.name,
        "promptType": req.promptType,
        "current": 0,
        "change_percent": 0,
        "turnover": req.turnover,
        "circulation_value": req.circulation_value,
        "kline_data": req.kline_data
    }
    
    await asyncio.to_thread(_hydrate_analysis_stock_data, stock_data, normalized_code)

    result = analyze_single_stock(stock_data, force_update=req.force)
    return {"status": "success", "result": result}

@app.get("/api/stock/kline")
async def get_stock_kline(code: str, type: str = "1min", user: models.User = Depends(check_data_permission)):
    """获取个股 K 线数据"""
    try:
        clean_code = "".join(filter(str.isdigit, code))
        if type == "1min":
            today_str = datetime.now().strftime('%Y-%m-%d')
            # Probe previous trade dates whenever the market is not in active
            # intraday session (weekends/holidays, pre-open, lunch break, post-close).
            allow_non_trading_probe = not (is_market_open_day() and is_trading_time())
            probe_dates = _probe_trade_dates_for_intraday() if allow_non_trading_probe else []
            today_df = lhb_manager.get_kline_1min(
                clean_code,
                today_str,
                KLINE_MIN_REFRESH_SEC,
                False,
            )
            today_rows = _normalize_intraday_kline_rows(today_df)
            today_complete = False
            try:
                today_complete = bool(lhb_manager._is_intraday_cache_complete(today_df, today_str, True))
            except Exception:
                today_complete = False
            if today_rows and today_complete:
                return {"status": "success", "data": today_rows}

            # Cache exists but incomplete (e.g. half-day): force one network补拉.
            if today_rows and not today_complete:
                repaired_df = lhb_manager.get_kline_1min(
                    clean_code,
                    today_str,
                    KLINE_MIN_REFRESH_SEC,
                    True,
                )
                repaired_rows = _normalize_intraday_kline_rows(repaired_df)
                if repaired_rows:
                    return {"status": "success", "data": repaired_rows}

            # 个股分时图兜底：history 主体 + latest(lt<=5) 增量，再回退新浪。
            if allow_non_trading_probe and probe_dates:
                network_probe_budget = min(4, len(probe_dates))
                for probe_date in probe_dates:
                    allow_network_probe = network_probe_budget > 0
                    if allow_network_probe:
                        network_probe_budget -= 1
                    probe_df = lhb_manager.get_kline_1min(
                        clean_code,
                        probe_date,
                        KLINE_MIN_REFRESH_SEC,
                        allow_network_probe,
                    )
                    probe_rows = _normalize_intraday_kline_rows(probe_df)
                    if probe_rows:
                        probe_only = [row for row in probe_rows if str(row.get("date", "")).startswith(probe_date)]
                        return {"status": "success", "data": (probe_only or probe_rows)}

            try:
                fallback_df = await asyncio.to_thread(data_provider.fetch_intraday_data, clean_code)
                fallback_rows = _normalize_intraday_kline_rows(fallback_df)
                if fallback_rows:
                    if allow_non_trading_probe and probe_dates:
                        for probe_date in probe_dates:
                            probe_only = [row for row in fallback_rows if str(row.get("date", "")).startswith(probe_date)]
                            if probe_only:
                                return {"status": "success", "data": probe_only}
                    today_only = [row for row in fallback_rows if str(row.get("date", "")).startswith(today_str)]
                    if today_only:
                        fallback_rows = today_only
                    return {"status": "success", "data": fallback_rows}
            except Exception:
                pass

            # 最后兜底：返回近几日已有缓存，避免完全空白。
            fallback_dates = list(probe_dates)
            if not fallback_dates:
                for offset in range(1, 8):
                    target_dt = datetime.now() - timedelta(days=offset)
                    if target_dt.weekday() >= 5:
                        continue
                    fallback_dates.append(target_dt.strftime('%Y-%m-%d'))
                    if len(fallback_dates) >= 4:
                        break
            for date_str in fallback_dates:
                df = lhb_manager.get_kline_1min(
                    clean_code,
                    date_str,
                    KLINE_MIN_REFRESH_SEC,
                    False,
                )
                rows = _normalize_intraday_kline_rows(df)
                if rows:
                    rows_on_date = [row for row in rows if str(row.get("date", "")).startswith(date_str)]
                    return {"status": "success", "data": (rows_on_date or rows)}
            msg = "分时缓存暂无数据，请稍后重试"
            try:
                biying_cfg = data_provider._get_biying_config()
                if not data_provider._biying_enabled(biying_cfg):
                    msg = "分时缓存暂无数据：必盈数据源未启用，且默认公网源暂不可用"
                elif lhb_manager.is_kline_network_paused():
                    remain = lhb_manager.get_kline_pause_remaining_seconds()
                    msg = f"分时缓存暂无数据：上游暂不可用，约{remain}s后重试"
            except Exception:
                pass
            return {"status": "success", "data": [], "message": msg}
        elif type == "day":
            rows = get_day_kline_from_cache(clean_code)
            if not rows:
                try:
                    # User explicitly opened day-K with empty cache: allow a throttled
                    # one-shot refresh regardless of current trading clock.
                    await asyncio.to_thread(refresh_day_kline_cache_for_code, clean_code, True)
                    rows = get_day_kline_from_cache(clean_code)
                except Exception:
                    rows = []
            if rows:
                return {"status": "success", "data": rows}
            msg = "日K缓存暂无数据，请稍后重试"
            try:
                if not data_provider._biying_enabled(data_provider._get_biying_config()):
                    msg = "日K缓存暂无数据：必盈数据源未启用，且默认公网源暂不可用"
            except Exception:
                pass
            return {"status": "success", "data": [], "message": msg}
                
    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {"status": "success", "data": [], "message": "暂无K线数据"}

@app.get("/api/stock/ai_markers")
async def get_ai_markers(code: str, type: str = None, user: models.User = Depends(check_data_permission)):
    """获取个股的 AI 分析历史标记"""
    # Determine priority based on type
    keys = []
    if type == 'day':
        keys.append(f"stock_analysis_{code}_day_trading_signal")
    elif type == '1min' or type == 'min':
        keys.append(f"stock_analysis_{code}_min_trading_signal")
    else:
        # Only use fallbacks if no type specified
        keys.extend([
            f"stock_analysis_{code}_min_trading_signal", 
            f"stock_analysis_{code}_day_trading_signal", 
            f"stock_analysis_{code}_trading_signal", 
            f"stock_analysis_{code}_normal"
        ])
    
    # Remove duplicates while preserving order
    unique_keys = []
    seen = set()
    for k in keys:
        if k not in seen:
            unique_keys.append(k)
            seen.add(k)
    
    for key in unique_keys:
        data = ai_cache.get(key)
        if data:
            # Check expiry logic for retrieval too
            # Although ai_cache might not store timestamp in 'get', we can check if we have timestamp in cache
            # But ai_cache implementation details are in core/ai_cache.py. 
            # Assuming simple retrieval for now.
            
            # If it's a string (JSON string or plain text), try to parse it
            if isinstance(data, str):
                try:
                    # Check if it looks like JSON
                    if data.strip().startswith('{'):
                        parsed = json.loads(data)
                        return {"status": "success", "markers": [{"date": datetime.now().strftime('%Y-%m-%d'), "data": parsed}]}
                except:
                    pass
            
            return {"status": "success", "markers": [{"date": datetime.now().strftime('%Y-%m-%d'), "data": data}]}
            
    return {"status": "success", "markers": []}


def _render_public_frontend_entry(filename: str) -> HTMLResponse:
    if DISABLE_PUBLIC_FRONTEND:
        return JSONResponse({"detail": "Public frontend disabled"}, status_code=404)
    html_path = FRONTEND_DIR / str(filename or "").strip()
    injected = _render_frontend_html_with_runtime_vars(
        html_path,
        auth_api_prefix=get_auth_api_prefix(),
    )
    if injected is not None:
        return injected
    raise HTTPException(status_code=404, detail="Frontend entry not found")


@app.get("/", include_in_schema=False)
async def serve_public_index():
    return _render_public_frontend_entry("index.html")


@app.get("/index.html", include_in_schema=False)
async def serve_public_index_html():
    return _render_public_frontend_entry("index.html")


@app.get("/lhb.html", include_in_schema=False)
async def serve_public_lhb_html():
    return _render_public_frontend_entry("lhb.html")


@app.get("/help.html", include_in_schema=False)
async def serve_public_help_html():
    return _render_public_frontend_entry("help.html")


# --- Static Files Deployment ---
# Serve frontend from root URL
# Must be last to not override API routes
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")

