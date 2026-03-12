from fastapi import APIRouter, Depends, HTTPException, Header, Body, Request
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import base64
import json
import os

from app.db import schemas, database, models
from app.core import user_service, account_store
from app.core.config_manager import SYSTEM_CONFIG
from app.core.runtime_logs import add_runtime_log
from app.core.operation_log import log_user_operation
from app.core.stock_utils import is_market_open_day, is_trading_time

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
ACCOUNTS_FILE = DATA_DIR / "user_accounts.json"
TRIAL_FP_FILE = DATA_DIR / "trial_fingerprints.json"
AUTH_ACCESS_LIMITS_FILE = DATA_DIR / "auth_access_limits.json"

PAID_VERSIONS = {"basic", "advanced", "flagship"}
GUEST_PREFIXES = ("guestv2_", "guest_", "visitor_")
GUEST_TRIAL_MINUTES = 10
REGISTER_TRIAL_DAYS = 3
REGISTER_PER_IP_LIMIT = 3
GUEST_PER_IP_LIMIT = 3
SERVER_VERSION = os.getenv("APP_VERSION", "v3.1.0")


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data):
    _ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_accounts():
    return account_store.load_accounts()


def _save_accounts(data):
    account_store.save_accounts(data)


def _load_trial_fingerprints():
    return _load_json(TRIAL_FP_FILE, {})


def _save_trial_fingerprints(data):
    _save_json(TRIAL_FP_FILE, data)


def _client_ip_from_request(request: Request) -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return str(request.client.host).strip()
    return ""


def _normalize_fingerprint(raw: str) -> str:
    return str(raw or "").strip()[:128]


def _dedupe_str_list(raw) -> list[str]:
    result = []
    seen = set()
    if isinstance(raw, list):
        for item in raw:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
    return result


def _load_auth_access_limits() -> dict:
    raw = _load_json(AUTH_ACCESS_LIMITS_FILE, {})
    if not isinstance(raw, dict):
        raw = {}

    def _normalize_map_of_lists(key: str) -> dict:
        src = raw.get(key, {})
        dst = {}
        if isinstance(src, dict):
            for k, v in src.items():
                kk = str(k or "").strip()
                if not kk:
                    continue
                vv = _dedupe_str_list(v)
                if vv:
                    dst[kk] = vv
        return dst

    fp_guest_src = raw.get("fingerprint_guest_device", {})
    fp_guest_dst = {}
    if isinstance(fp_guest_src, dict):
        for k, v in fp_guest_src.items():
            kk = str(k or "").strip()
            vv = str(v or "").strip()
            if kk and vv:
                fp_guest_dst[kk] = vv

    return {
        "ip_registered_users": _normalize_map_of_lists("ip_registered_users"),
        "ip_guest_devices": _normalize_map_of_lists("ip_guest_devices"),
        "fingerprint_registered_users": _normalize_map_of_lists("fingerprint_registered_users"),
        "fingerprint_guest_device": fp_guest_dst,
    }


def _save_auth_access_limits(data: dict):
    payload = data if isinstance(data, dict) else {}
    _save_json(AUTH_ACCESS_LIMITS_FILE, payload)


def _append_unique(target: list[str], value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return target
    if text not in target:
        target.append(text)
    return target


def _has_registered_hint(limits: dict, client_ip: str, fingerprint_id: str) -> bool:
    if fingerprint_id:
        fp_users = (limits.get("fingerprint_registered_users") or {}).get(fingerprint_id, [])
        if isinstance(fp_users, list) and len(fp_users) > 0:
            return True
    if client_ip:
        ip_users = (limits.get("ip_registered_users") or {}).get(client_ip, [])
        if isinstance(ip_users, list) and len(ip_users) > 0:
            return True
    return False


def _guest_limit_detail(must_login: bool) -> str:
    if must_login:
        return "当前IP/设备游客已达上限，你已注册过账号，请先登录"
    return "当前IP/设备游客已达上限，请先注册账号"


def _remove_guest_device_from_limits(
    limits: dict,
    *,
    device_id: str = "",
    client_ip: str = "",
    fingerprint_id: str = "",
):
    if not isinstance(limits, dict):
        return
    did = str(device_id or "").strip()
    ip_guest_devices = limits.setdefault("ip_guest_devices", {})
    if isinstance(ip_guest_devices, dict):
        for ip_key, arr in list(ip_guest_devices.items()):
            cleaned = [x for x in _dedupe_str_list(arr) if (not did) or x != did]
            if cleaned:
                ip_guest_devices[ip_key] = cleaned
            else:
                ip_guest_devices.pop(ip_key, None)
        if client_ip and client_ip in ip_guest_devices:
            cleaned = [x for x in _dedupe_str_list(ip_guest_devices.get(client_ip, [])) if (not did) or x != did]
            if cleaned:
                ip_guest_devices[client_ip] = cleaned
            else:
                ip_guest_devices.pop(client_ip, None)

    fp_guest_device = limits.setdefault("fingerprint_guest_device", {})
    if isinstance(fp_guest_device, dict):
        if fingerprint_id:
            bound = str(fp_guest_device.get(fingerprint_id, "") or "").strip()
            if (not did) or (bound == did):
                fp_guest_device.pop(fingerprint_id, None)
        if did:
            for fp, bound in list(fp_guest_device.items()):
                if str(bound or "").strip() == did:
                    fp_guest_device.pop(fp, None)

def _hash_password(password: str, salt: str) -> str:
    return account_store.hash_password(password, salt)


def _decode_b64_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.b64decode(padded.encode("utf-8"), validate=False).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _read_auth_field(payload: dict, plain_key: str, encoded_key: str) -> str:
    if not isinstance(payload, dict):
        return ""
    encoded = _decode_b64_text(payload.get(encoded_key))
    if encoded:
        return encoded.strip()
    return str(payload.get(plain_key) or "").strip()


def _make_device_id(username: str) -> str:
    return account_store.make_device_id(username)


def _is_guest_device(device_id: str) -> bool:
    return any(device_id.startswith(prefix) for prefix in GUEST_PREFIXES)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _can_apply_trial(account: dict, user: models.User) -> bool:
    if not account or not user:
        return False
    if account.get("trial_applied"):
        return False
    if user.version in PAID_VERSIONS:
        return False
    return True


def _community_config() -> dict:
    cfg = SYSTEM_CONFIG.get("community_config", {})
    return cfg if isinstance(cfg, dict) else {}


def _referral_config() -> dict:
    cfg = SYSTEM_CONFIG.get("referral_config", {})
    return cfg if isinstance(cfg, dict) else {}


def _client_bootstrap_payload() -> dict:
    community = _community_config()
    return {
        "system_config": {
            "auto_analysis_enabled": bool(SYSTEM_CONFIG.get("auto_analysis_enabled", True)),
        },
        "community_config": {
            "qq_group_number": str(community.get("qq_group_number", "") or "").strip(),
            "qq_group_link": str(community.get("qq_group_link", "") or "").strip(),
            "welcome_text": str(community.get("welcome_text", "") or "").strip(),
        },
        "server_time_utc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }


def _market_status_payload() -> dict:
    return {
        "status": "success",
        "is_trading_time": bool(is_trading_time()),
        "is_market_open_day": bool(is_market_open_day()),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "server_version": SERVER_VERSION,
    }


def _build_account_meta_payload(device_id: str, db: Session, user: Optional[models.User] = None) -> dict:
    username, matched = account_store.get_account_by_device_id(device_id)
    if not matched or not username:
        return {
            "is_registered": False,
            "username": "",
            "can_apply_trial": False,
            "invite_code": "",
        }

    invite_code = account_store.ensure_invite_code_for_username(username)
    if invite_code:
        matched = account_store.get_account_by_username(username) or matched
    current_user = user if user is not None else user_service.get_or_create_user(db, device_id)
    return {
        "is_registered": True,
        "username": username,
        "invite_code": invite_code,
        "can_apply_trial": _can_apply_trial(matched, current_user),
        "user": _build_user_payload(current_user),
    }


def _default_share_template() -> str:
    return "我在用涨停狙击手，注册链接：{invite_link}，邀请码：{invite_code}。注册后在充值页填写邀请码，可获得赠送权益。"


def _fill_share_template(template: str, invite_code: str, invite_link: str) -> str:
    text = (template or _default_share_template()).strip() or _default_share_template()
    return text.replace("{invite_code}", invite_code).replace("{invite_link}", invite_link)


def _build_invite_link(request: Request, invite_code: str) -> str:
    cfg = _referral_config()
    base_url = str(cfg.get("share_base_url", "")).strip()
    if not base_url:
        origin = (request.headers.get("origin") or "").strip()
        if not origin:
            host = (request.headers.get("host") or "").strip()
            if host:
                origin = f"{request.url.scheme}://{host}"
        base_url = origin or "/"

    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}invite={invite_code}"


def _build_invite_info_payload(
    request: Request,
    username: str,
    account: Optional[dict],
    *,
    strict: bool = True,
) -> dict:
    if not username or not isinstance(account, dict):
        if strict:
            raise HTTPException(status_code=403, detail="请先注册并登录后再使用邀请功能")
        return {
            "enabled": False,
            "invite_code": "",
            "invite_link": "",
            "reward_days": 0,
            "share_text": "",
        }

    invite_code = account_store.ensure_invite_code_for_username(username)

    referral_cfg = _referral_config()
    try:
        reward_days = int(referral_cfg.get("reward_days", 30) or 30)
    except Exception:
        reward_days = 30
    reward_days = max(1, min(reward_days, 365))
    invite_link = _build_invite_link(request, invite_code)
    share_text = _fill_share_template(
        str(referral_cfg.get("share_template", "")),
        invite_code,
        invite_link,
    )

    return {
        "enabled": bool(referral_cfg.get("enabled", True)),
        "invite_code": invite_code,
        "invite_link": invite_link,
        "reward_days": reward_days,
        "share_text": share_text,
    }


def _build_user_payload(user):
    quotas = user_service.get_user_quota(user.version)
    expires_at = _as_utc(user.expires_at)
    created_at = _as_utc(user.created_at)
    now_utc = datetime.now(timezone.utc)
    is_expired = bool(expires_at and expires_at < now_utc)
    return {
        "id": user.id,
        "device_id": user.device_id,
        "version": user.version,
        "expires_at": expires_at,
        "created_at": created_at,
        "daily_ai_count": user.daily_ai_count,
        "daily_raid_count": user.daily_raid_count,
        "daily_review_count": user.daily_review_count,
        "remaining_ai": quotas["ai"] - user.daily_ai_count,
        "remaining_raid": quotas["raid"] - user.daily_raid_count,
        "remaining_review": quotas["review"] - user.daily_review_count,
        "is_expired": is_expired,
    }


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/login", response_model=schemas.UserInfo)
async def login(data: schemas.UserCreate, request: Request, db: Session = Depends(get_db)):
    device_id = (data.device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="缺少设备标识")

    account_store.ensure_device_not_banned(device_id)
    client_ip = _client_ip_from_request(request)
    fingerprint_id = _normalize_fingerprint(request.headers.get("X-Device-Fingerprint"))
    created = db.query(models.User).filter(models.User.device_id == device_id).first() is None

    if created and _is_guest_device(device_id):
        limits = _load_auth_access_limits()
        ip_guest_devices = limits.get("ip_guest_devices", {})
        fp_guest_device = limits.get("fingerprint_guest_device", {})

        if fingerprint_id:
            bound_guest = str(fp_guest_device.get(fingerprint_id, "") or "").strip()
            if bound_guest and bound_guest != device_id:
                must_login = _has_registered_hint(limits, client_ip, fingerprint_id)
                raise HTTPException(status_code=403, detail=_guest_limit_detail(must_login))

        if client_ip:
            guest_devices = _dedupe_str_list(ip_guest_devices.get(client_ip, []))
            if device_id not in guest_devices and len(guest_devices) >= GUEST_PER_IP_LIMIT:
                must_login = _has_registered_hint(limits, client_ip, fingerprint_id)
                raise HTTPException(status_code=403, detail=_guest_limit_detail(must_login))

    user = user_service.get_or_create_user(db, device_id)

    # 新游客首次进入：自动开通10分钟浏览权限
    if created and _is_guest_device(device_id):
        user.version = "trial"
        user.expires_at = datetime.utcnow() + timedelta(minutes=GUEST_TRIAL_MINUTES)
        user.daily_ai_count = 0
        user.daily_raid_count = 0
        user.daily_review_count = 0
        db.commit()
        db.refresh(user)

        limits = _load_auth_access_limits()
        if client_ip:
            ip_guest_devices = limits.setdefault("ip_guest_devices", {})
            ip_guest_devices[client_ip] = _append_unique(_dedupe_str_list(ip_guest_devices.get(client_ip, [])), device_id)
        if fingerprint_id:
            fp_guest_device = limits.setdefault("fingerprint_guest_device", {})
            fp_guest_device[fingerprint_id] = device_id
        _save_auth_access_limits(limits)

    return _build_user_payload(user)


@router.post("/register")
async def register(request: Request, data: dict = Body(...), db: Session = Depends(get_db)):
    username = _read_auth_field(data, "username", "username_b64")
    password = _read_auth_field(data, "password", "password_b64")
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入用户名和密码")
    if len(username) < 3 or len(password) < 6:
        raise HTTPException(status_code=400, detail="用户名至少3位，密码至少6位")

    client_ip = _client_ip_from_request(request)
    fingerprint_id = _normalize_fingerprint(request.headers.get("X-Device-Fingerprint"))
    limits = _load_auth_access_limits()
    if client_ip:
        current_users = _dedupe_str_list((limits.get("ip_registered_users") or {}).get(client_ip, []))
        if username not in current_users and len(current_users) >= REGISTER_PER_IP_LIMIT:
            raise HTTPException(status_code=403, detail="当前IP注册账号数量已达上限，请使用已有账号登录")

    existing_account = account_store.get_account_by_username(username)
    if existing_account:
        raise HTTPException(status_code=400, detail="用户名已存在")

    accounts = _load_accounts()
    source_device_id = str(request.headers.get("X-Device-ID") or "").strip()
    source_is_guest = _is_guest_device(source_device_id)

    salt = os.urandom(8).hex()
    password_hash = _hash_password(password, salt)
    device_id = _make_device_id(username)
    accounts[username] = {
        "username": username,
        "salt": salt,
        "password_hash": password_hash,
        "device_id": device_id,
        "created_at": datetime.utcnow().isoformat(),
        "trial_applied": False,
        "is_banned": False,
    }
    invite_code = account_store.ensure_account_invite_code(username, accounts)
    _save_accounts(accounts)

    if client_ip or fingerprint_id or source_is_guest:
        ip_registered_users = limits.setdefault("ip_registered_users", {})
        fingerprint_registered_users = limits.setdefault("fingerprint_registered_users", {})
        if client_ip:
            ip_registered_users[client_ip] = _append_unique(
                _dedupe_str_list(ip_registered_users.get(client_ip, [])),
                username,
            )
        if fingerprint_id:
            fingerprint_registered_users[fingerprint_id] = _append_unique(
                _dedupe_str_list(fingerprint_registered_users.get(fingerprint_id, [])),
                username,
            )

        if source_is_guest:
            _remove_guest_device_from_limits(
                limits,
                device_id=source_device_id,
                client_ip=client_ip,
                fingerprint_id=fingerprint_id,
            )
        _save_auth_access_limits(limits)

    # 注册后默认未开通高级权限（需申请体验或购买）
    guest_user = None
    if source_is_guest:
        guest_user = db.query(models.User).filter(models.User.device_id == source_device_id).first()

    user = db.query(models.User).filter(models.User.device_id == device_id).first()
    if guest_user and (user is None or guest_user.id == user.id):
        guest_user.device_id = device_id
        user = guest_user
    else:
        user = user or user_service.get_or_create_user(db, device_id)
        if guest_user and guest_user.id != user.id:
            try:
                db.delete(guest_user)
            except Exception:
                pass

    user.version = "trial"
    user.expires_at = datetime.utcnow()
    user.daily_ai_count = 0
    user.daily_raid_count = 0
    user.daily_review_count = 0
    db.commit()
    db.refresh(user)
    add_runtime_log(f"[认证] 注册成功: username={username}, device={device_id}")
    log_user_operation(
        "user_register",
        status="success",
        actor="user",
        method="POST",
        path="/api/auth/register",
        username=username,
        device_id=device_id,
        detail="register_success",
    )

    return {
        "token": device_id,
        "username": username,
        "invite_code": invite_code,
        "can_apply_trial": _can_apply_trial(accounts[username], user),
        "user": _build_user_payload(user),
        "qq_group_number": str(_community_config().get("qq_group_number", "")).strip(),
        "qq_group_link": str(_community_config().get("qq_group_link", "")).strip(),
        "qq_group_welcome_text": str(_community_config().get("welcome_text", "")).strip(),
    }


@router.post("/login_user")
async def login_user(data: dict = Body(...), db: Session = Depends(get_db)):
    username = _read_auth_field(data, "username", "username_b64")
    password = _read_auth_field(data, "password", "password_b64")
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入用户名和密码")

    account = account_store.get_account_by_username(username)
    if not account:
        log_user_operation(
            "user_login",
            status="failed",
            actor="user",
            method="POST",
            path="/api/auth/login_user",
            username=username,
            detail="account_not_found",
        )
        raise HTTPException(status_code=404, detail="用户不存在，请先注册")

    account_store.ensure_device_not_banned(str(account.get("device_id", "")).strip())

    if _hash_password(password, account.get("salt", "")) != account.get("password_hash"):
        log_user_operation(
            "user_login",
            status="failed",
            actor="user",
            method="POST",
            path="/api/auth/login_user",
            username=username,
            device_id=account.get("device_id", ""),
            detail="password_incorrect",
        )
        raise HTTPException(status_code=401, detail="密码错误")

    device_id = account["device_id"]
    user = user_service.get_or_create_user(db, device_id)
    add_runtime_log(f"[认证] 登录成功: username={username}, device={device_id}")
    log_user_operation(
        "user_login",
        status="success",
        actor="user",
        method="POST",
        path="/api/auth/login_user",
        username=username,
        device_id=device_id,
        detail="login_success",
    )

    return {
        "token": device_id,
        "username": username,
        "can_apply_trial": _can_apply_trial(account, user),
        "user": _build_user_payload(user),
    }


@router.get("/account_meta")
async def account_meta(x_device_id: str = Header(None), db: Session = Depends(get_db)):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing Device ID")

    account_store.ensure_device_not_banned(x_device_id)
    return _build_account_meta_payload(x_device_id, db)


@router.get("/bootstrap")
async def bootstrap(request: Request, x_device_id: str = Header(None, alias="X-Device-ID"), db: Session = Depends(get_db)):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing Device ID")

    account_store.ensure_device_not_banned(x_device_id)
    user = user_service.get_or_create_user(db, x_device_id)
    account_meta = _build_account_meta_payload(x_device_id, db, user=user)
    payload = _client_bootstrap_payload()
    payload["user"] = _build_user_payload(user)
    payload["account_meta"] = account_meta
    payload["market_status"] = _market_status_payload()
    payload["server_version"] = SERVER_VERSION
    if account_meta.get("is_registered"):
        username = str(account_meta.get("username") or "").strip()
        account = account_store.get_account_by_username(username)
        payload["invite_info"] = _build_invite_info_payload(request, username, account, strict=False)
    else:
        payload["invite_info"] = _build_invite_info_payload(request, "", {}, strict=False)
    return payload


@router.get("/invite_info")
async def invite_info(request: Request, x_device_id: str = Header(None, alias="X-Device-ID")):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing Device ID")

    account_store.ensure_device_not_banned(x_device_id)
    username, account = account_store.get_account_by_device_id(x_device_id)
    return _build_invite_info_payload(request, username, account, strict=True)


@router.post("/apply_trial")
async def apply_trial(
    data: dict = Body(...),
    x_device_id: str = Header(None),
    db: Session = Depends(get_db),
):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="请先登录后再申请体验")

    account_store.ensure_device_not_banned(x_device_id)

    fingerprint_id = (data.get("fingerprint_id") or "").strip()
    if not fingerprint_id:
        raise HTTPException(status_code=400, detail="缺少设备标识")

    account_key, matched_account = account_store.get_account_by_device_id(x_device_id)

    if not account_key or not matched_account:
        raise HTTPException(status_code=403, detail="请先注册并登录后再申请体验")

    accounts = _load_accounts()
    if account_key not in accounts:
        accounts[account_key] = matched_account

    user = user_service.get_or_create_user(db, x_device_id)
    if user.version in PAID_VERSIONS:
        raise HTTPException(status_code=400, detail="会员账号无需申请3天体验")

    account = account_store.get_account_by_username(account_key) or accounts[account_key]
    if bool(account.get("trial_applied")):
        raise HTTPException(status_code=400, detail="当前账号已申请过3天体验")

    fp_used = _load_trial_fingerprints()
    if fp_used.get(fingerprint_id):
        raise HTTPException(status_code=400, detail="当前设备已使用过3天体验资格")

    fp_used[fingerprint_id] = {
        "username": account_key,
        "applied_at": datetime.utcnow().isoformat(),
    }
    _save_trial_fingerprints(fp_used)

    account_store.update_account_fields(
        account_key,
        {
            "trial_applied": True,
            "trial_applied_at": datetime.utcnow().isoformat(),
        },
    )

    user.version = "trial"
    user.expires_at = datetime.utcnow() + timedelta(days=REGISTER_TRIAL_DAYS)
    user.daily_ai_count = 0
    user.daily_raid_count = 0
    user.daily_review_count = 0
    db.commit()
    db.refresh(user)
    add_runtime_log(f"[认证] 试用已开通: username={account_key}, device={x_device_id}")
    log_user_operation(
        "apply_trial",
        status="success",
        actor="user",
        method="POST",
        path="/api/auth/apply_trial",
        username=account_key,
        device_id=x_device_id,
        detail=f"trial_{REGISTER_TRIAL_DAYS}d_applied",
    )

    return {
        "status": "success",
        "message": "体验申请已自动通过",
        "can_apply_trial": False,
        "user": _build_user_payload(user),
    }


@router.get("/status")
async def check_status(x_device_id: str = Header(None), db: Session = Depends(get_db)):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing Device ID")
    account_store.ensure_device_not_banned(x_device_id)

    user = user_service.get_or_create_user(db, x_device_id)

    status = "active"
    if user.expires_at and user.expires_at < datetime.utcnow():
        status = "expired"

    return {
        "status": status,
        "type": user.version,
        "expiry": user.expires_at.strftime("%Y-%m-%d %H:%M") if user.expires_at else "永久",
    }

