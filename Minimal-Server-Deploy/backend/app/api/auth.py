from fastapi import APIRouter, Depends, HTTPException, Header, Body, Request
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import json
import os

from app.db import schemas, database, models
from app.core import user_service, account_store
from app.core.config_manager import SYSTEM_CONFIG
from app.core.runtime_logs import add_runtime_log
from app.core.operation_log import log_user_operation

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
ACCOUNTS_FILE = DATA_DIR / "user_accounts.json"
TRIAL_FP_FILE = DATA_DIR / "trial_fingerprints.json"

PAID_VERSIONS = {"basic", "advanced", "flagship"}
GUEST_PREFIXES = ("guestv2_", "guest_", "visitor_")
GUEST_TRIAL_MINUTES = 10


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


def _hash_password(password: str, salt: str) -> str:
    return account_store.hash_password(password, salt)


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
async def login(data: schemas.UserCreate, db: Session = Depends(get_db)):
    device_id = (data.device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="缺少设备标识")

    account_store.ensure_device_not_banned(device_id)
    created = db.query(models.User).filter(models.User.device_id == device_id).first() is None
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

    return _build_user_payload(user)


@router.post("/register")
async def register(data: dict = Body(...), db: Session = Depends(get_db)):
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入用户名和密码")
    if len(username) < 3 or len(password) < 6:
        raise HTTPException(status_code=400, detail="用户名至少3位，密码至少6位")

    accounts = _load_accounts()
    if username in accounts:
        raise HTTPException(status_code=400, detail="用户名已存在")

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

    # 注册后默认未开通高级权限（需申请体验或购买）
    user = user_service.get_or_create_user(db, device_id)
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
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入用户名和密码")

    accounts = _load_accounts()
    account = accounts.get(username)
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

    account_store.ensure_device_not_banned(str(account.get("device_id", "")).strip(), accounts=accounts)

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
    accounts = _load_accounts()
    username, matched = account_store.get_account_by_device_id(x_device_id, accounts=accounts)

    if not matched or not username:
        return {
            "is_registered": False,
            "username": "",
            "can_apply_trial": False,
        }

    invite_code = account_store.ensure_account_invite_code(username, accounts)
    _save_accounts(accounts)
    user = user_service.get_or_create_user(db, x_device_id)
    return {
        "is_registered": True,
        "username": username,
        "invite_code": invite_code,
        "can_apply_trial": _can_apply_trial(matched, user),
        "user": _build_user_payload(user),
    }


@router.get("/invite_info")
async def invite_info(request: Request, x_device_id: str = Header(None, alias="X-Device-ID")):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing Device ID")

    account_store.ensure_device_not_banned(x_device_id)
    accounts = _load_accounts()
    username, account = account_store.get_account_by_device_id(x_device_id, accounts=accounts)
    if not username or not account:
        raise HTTPException(status_code=403, detail="请先注册账号后再使用邀请功能")

    invite_code = account_store.ensure_account_invite_code(username, accounts)
    _save_accounts(accounts)

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

    accounts = _load_accounts()
    account_key = None
    for username, account in accounts.items():
        if account.get("device_id") == x_device_id:
            account_key = username
            break

    if not account_key:
        raise HTTPException(status_code=403, detail="请先注册并登录后再申请体验")

    user = user_service.get_or_create_user(db, x_device_id)
    if user.version in PAID_VERSIONS:
        raise HTTPException(status_code=400, detail="会员账号不支持申请10分钟体验")

    account = accounts[account_key]
    if account.get("trial_applied"):
        raise HTTPException(status_code=400, detail="当前账号已申请过10分钟体验")

    fp_used = _load_trial_fingerprints()
    if fp_used.get(fingerprint_id):
        raise HTTPException(status_code=400, detail="当前设备已使用过体验资格")

    fp_used[fingerprint_id] = {
        "username": account_key,
        "applied_at": datetime.utcnow().isoformat(),
    }
    _save_trial_fingerprints(fp_used)

    account["trial_applied"] = True
    account["trial_applied_at"] = datetime.utcnow().isoformat()
    accounts[account_key] = account
    _save_accounts(accounts)

    user.version = "trial"
    user.expires_at = datetime.utcnow() + timedelta(minutes=10)
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
        detail="trial_10min_applied",
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
