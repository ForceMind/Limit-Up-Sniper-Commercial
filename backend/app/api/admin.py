from fastapi import APIRouter, Depends, HTTPException, Header, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db import models, schemas, database
from typing import List, Optional, Dict, Any, Tuple
import secrets
import os
import json
import hashlib
import re
from pathlib import Path
from app.core.config_manager import SYSTEM_CONFIG, save_config
from app.core.lhb_manager import lhb_manager
from app.core import user_service, purchase_manager, account_store
from app.core import watchlist_stats
from app.core.ai_cache import ai_cache
from app.core.runtime_logs import get_runtime_logs, add_runtime_log
from app.core.ws_hub import ws_hub
from app.core.operation_log import log_user_operation, get_recent_user_operations
from datetime import datetime, timedelta
from pydantic import BaseModel

router = APIRouter()

# --- Security Configuration ---
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
ADMIN_SECRET_FILE = DATA_DIR / "admin_token.txt"
ADMIN_CREDENTIALS_FILE = DATA_DIR / "admin_credentials.json"
ADMIN_SESSIONS_FILE = DATA_DIR / "admin_sessions.json"
ADMIN_PANEL_PATH_FILE = DATA_DIR / "admin_panel_path.json"
USER_ACCOUNTS_FILE = DATA_DIR / "user_accounts.json"
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_ATTEMPTS = 5
SESSION_EXPIRE_HOURS = 24
failed_attempts: Dict[str, List[float]] = {}


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_user_accounts() -> Dict[str, dict]:
    return account_store.load_accounts()


def _save_user_accounts(data: Dict[str, dict]):
    account_store.save_accounts(data)


def _normalize_admin_panel_path(raw_path: str) -> str:
    path = (raw_path or "").strip()
    if not path:
        return "/admin"
    if not path.startswith("/"):
        path = "/" + path
    parts = [p for p in path.split("/") if p]
    path = "/" + "/".join(parts)
    if path in {"", "/"}:
        raise ValueError("后台地址不能为根路径")
    if path.startswith("/api"):
        raise ValueError("后台地址不能以 /api 开头")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]+", path):
        raise ValueError("后台地址只允许字母、数字、/、_、-")
    return path


def get_admin_panel_path() -> str:
    data = _load_json(ADMIN_PANEL_PATH_FILE, {})
    if isinstance(data, dict):
        try:
            return _normalize_admin_panel_path(data.get("path", "/admin"))
        except ValueError:
            return "/admin"
    return "/admin"


def _save_admin_panel_path(path: str):
    normalized = _normalize_admin_panel_path(path)
    _save_json(
        ADMIN_PANEL_PATH_FILE,
        {
            "path": normalized,
            "updated_at": datetime.utcnow().isoformat(),
        },
    )


def _generate_random_password(length: int = 10) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    size = max(8, min(int(length or 10), 24))
    return "".join(secrets.choice(alphabet) for _ in range(size))


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def _default_admin_password() -> str:
    # Migrate from legacy token file if available
    if ADMIN_SECRET_FILE.exists():
        try:
            token = ADMIN_SECRET_FILE.read_text(encoding="utf-8").strip()
            if token and len(token) >= 6:
                return token
        except Exception:
            pass
    return "admin123456"


def _load_admin_credentials() -> Dict[str, str]:
    cred = _load_json(ADMIN_CREDENTIALS_FILE, {})
    if isinstance(cred, dict) and cred.get("username") and cred.get("salt") and cred.get("password_hash"):
        return cred

    username = "admin"
    plain_pwd = _default_admin_password()
    salt = os.urandom(8).hex()
    cred = {
        "username": username,
        "salt": salt,
        "password_hash": _hash_password(plain_pwd, salt),
        "updated_at": datetime.utcnow().isoformat(),
    }
    _save_json(ADMIN_CREDENTIALS_FILE, cred)
    return cred


def _verify_admin_password(password: str, cred: Dict[str, str]) -> bool:
    return _hash_password(password or "", cred.get("salt", "")) == cred.get("password_hash", "")


def _load_sessions() -> Dict[str, dict]:
    sessions = _load_json(ADMIN_SESSIONS_FILE, {})
    return sessions if isinstance(sessions, dict) else {}


def _save_sessions(sessions: Dict[str, dict]):
    _save_json(ADMIN_SESSIONS_FILE, sessions)


def _cleanup_sessions(sessions: Dict[str, dict]) -> Dict[str, dict]:
    now = datetime.utcnow()
    cleaned = {}
    for token, info in sessions.items():
        try:
            expires_at = datetime.fromisoformat(info.get("expires_at", ""))
            if expires_at > now:
                cleaned[token] = info
        except Exception:
            continue
    if cleaned != sessions:
        _save_sessions(cleaned)
    return cleaned


async def verify_admin(x_admin_token: str = Header(..., alias="X-Admin-Token")):
    sessions = _cleanup_sessions(_load_sessions())
    if not x_admin_token or x_admin_token not in sessions:
        raise HTTPException(status_code=403, detail="Admin authorization failed")
    return True


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Auth / Account ---
class AdminLoginSchema(BaseModel):
    username: str
    password: str


class UpdatePasswordSchema(BaseModel):
    old_password: str
    new_password: str


class UpdateAdminPanelPathSchema(BaseModel):
    path: str


@router.post("/login")
async def admin_login(data: AdminLoginSchema, request_ip: str = Header(None, alias="X-Forwarded-For")):
    client_ip = (request_ip or "local").split(",")[0].strip()
    now_ts = datetime.utcnow().timestamp()

    # IP rate limit
    attempts = [t for t in failed_attempts.get(client_ip, []) if now_ts - t < RATE_LIMIT_WINDOW]
    failed_attempts[client_ip] = attempts
    if len(attempts) >= RATE_LIMIT_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many failed attempts, try later")

    cred = _load_admin_credentials()
    username = (data.username or "").strip()
    password = (data.password or "").strip()

    if username != cred.get("username") or not _verify_admin_password(password, cred):
        attempts.append(now_ts)
        failed_attempts[client_ip] = attempts
        add_runtime_log(f"[ADMIN] Login failed from ip={client_ip}, username={username}")
        log_user_operation(
            "admin_login",
            status="failed",
            actor="admin",
            method="POST",
            path="/api/admin/login",
            username=username,
            ip=client_ip,
            detail="invalid_username_or_password",
        )
        raise HTTPException(status_code=403, detail="用户名或密码错误")

    # Success, reset failed attempts
    failed_attempts[client_ip] = []

    token = secrets.token_urlsafe(24)
    created_at = datetime.utcnow()
    expires_at = created_at + timedelta(hours=SESSION_EXPIRE_HOURS)
    sessions = _cleanup_sessions(_load_sessions())
    sessions[token] = {
        "username": cred.get("username"),
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "ip": client_ip,
    }
    _save_sessions(sessions)
    add_runtime_log(f"[ADMIN] Login success: ip={client_ip}, username={cred.get('username')}")
    log_user_operation(
        "admin_login",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/login",
        username=cred.get("username", ""),
        ip=client_ip,
        detail="login_success",
    )

    return {
        "status": "success",
        "token": token,
        "username": cred.get("username"),
        "expires_at": expires_at,
    }


@router.post("/logout")
async def admin_logout(x_admin_token: str = Header(..., alias="X-Admin-Token")):
    sessions = _cleanup_sessions(_load_sessions())
    session = sessions.get(x_admin_token, {}) if isinstance(sessions, dict) else {}
    if x_admin_token in sessions:
        sessions.pop(x_admin_token, None)
        _save_sessions(sessions)
    log_user_operation(
        "admin_logout",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/logout",
        username=str(session.get("username", "")),
        ip=str(session.get("ip", "")),
        detail="logout_success",
    )
    return {"status": "success"}


@router.post("/update_password")
async def update_admin_password(
    data: UpdatePasswordSchema,
    authorized: bool = Depends(verify_admin),
):
    if len((data.new_password or "").strip()) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    cred = _load_admin_credentials()
    old_password = (data.old_password or "").strip()
    if old_password and not _verify_admin_password(old_password, cred):
        raise HTTPException(status_code=403, detail="Old password is incorrect")

    salt = os.urandom(8).hex()
    cred["salt"] = salt
    cred["password_hash"] = _hash_password(data.new_password.strip(), salt)
    cred["updated_at"] = datetime.utcnow().isoformat()
    _save_json(ADMIN_CREDENTIALS_FILE, cred)

    # Force all sessions to re-login after password change
    _save_sessions({})
    log_user_operation(
        "update_admin_password",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/update_password",
        username=cred.get("username", ""),
        detail="admin_password_updated",
    )
    return {"status": "success", "message": "Admin password updated successfully"}


@router.get("/panel_path")
async def get_admin_panel_path_api(authorized: bool = Depends(verify_admin)):
    return {
        "status": "success",
        "path": get_admin_panel_path(),
    }


@router.post("/panel_path")
async def update_admin_panel_path_api(
    payload: UpdateAdminPanelPathSchema,
    authorized: bool = Depends(verify_admin),
):
    try:
        _save_admin_panel_path(payload.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    final_path = get_admin_panel_path()
    add_runtime_log(f"[ADMIN] Updated admin panel path: {final_path}")
    log_user_operation(
        "update_admin_panel_path",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/panel_path",
        detail=final_path,
    )
    return {"status": "success", "path": final_path}


# --- User / Order Management ---
@router.get("/users")
async def list_users(
    skip: int = 0,
    limit: int = 100,
    account_type: str = "all",
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin),
):
    users = db.query(models.User).order_by(models.User.created_at.desc()).all()
    accounts = _load_user_accounts()
    device_to_username: Dict[str, str] = {}
    for username, account in accounts.items():
        if not isinstance(account, dict):
            continue
        did = str(account.get("device_id", "")).strip()
        if did:
            device_to_username[did] = username

    filter_mode = (account_type or "all").strip().lower()
    if filter_mode not in {"all", "guest", "registered"}:
        raise HTTPException(status_code=400, detail="Invalid account_type, must be all/guest/registered")

    res: List[Dict[str, Any]] = []
    for u in users:
        quotas = user_service.get_user_quota(u.version)
        username = device_to_username.get(u.device_id, "")
        is_registered = bool(username)
        if filter_mode == "guest" and is_registered:
            continue
        if filter_mode == "registered" and not is_registered:
            continue
        account = accounts.get(username, {}) if is_registered else {}
        res.append({
            "id": u.id,
            "device_id": u.device_id,
            "username": username,
            "is_registered": is_registered,
            "account_type": "registered" if is_registered else "guest",
            "is_banned": bool(account.get("is_banned", False)) if isinstance(account, dict) else False,
            "banned_reason": str(account.get("banned_reason", "")).strip() if isinstance(account, dict) else "",
            "version": u.version,
            "expires_at": u.expires_at,
            "created_at": u.created_at,
            "daily_ai_count": u.daily_ai_count,
            "daily_raid_count": u.daily_raid_count,
            "daily_review_count": u.daily_review_count,
            "remaining_ai": quotas['ai'] - u.daily_ai_count,
            "remaining_raid": quotas['raid'] - u.daily_raid_count,
            "remaining_review": quotas['review'] - u.daily_review_count,
            "is_expired": (u.expires_at and u.expires_at < datetime.utcnow())
        })
    safe_skip = max(0, int(skip or 0))
    safe_limit = max(1, min(int(limit or 100), 1000))
    return res[safe_skip:safe_skip + safe_limit]


class ResetUserPasswordSchema(BaseModel):
    user_id: Optional[int] = None
    username: Optional[str] = None
    device_id: Optional[str] = None


class SetUserBanSchema(BaseModel):
    user_id: Optional[int] = None
    username: Optional[str] = None
    device_id: Optional[str] = None
    banned: bool = True
    reason: Optional[str] = None


class SetUserMembershipSchema(BaseModel):
    user_id: int
    version: str
    days: int = 30


def _resolve_target_username(
    payload_username: Optional[str],
    payload_device_id: Optional[str],
    payload_user_id: Optional[int],
    db: Session,
    accounts: Dict[str, dict],
) -> Tuple[str, str]:
    target_username = (payload_username or "").strip()
    target_device_id = (payload_device_id or "").strip()

    if not target_device_id and payload_user_id:
        user = db.query(models.User).filter(models.User.id == payload_user_id).first()
        if user:
            target_device_id = str(user.device_id or "").strip()

    if not target_username and target_device_id:
        target_username = account_store.get_username_by_device_id(target_device_id, accounts=accounts)

    if target_username and not target_device_id:
        account = accounts.get(target_username, {})
        if isinstance(account, dict):
            target_device_id = str(account.get("device_id", "")).strip()

    return target_username, target_device_id


@router.post("/users/reset_password")
async def reset_user_password(
    payload: ResetUserPasswordSchema,
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin),
):
    accounts = _load_user_accounts()
    if not accounts:
        raise HTTPException(status_code=404, detail="No registered accounts found")

    target_username = (payload.username or "").strip()
    target_device_id = (payload.device_id or "").strip()

    if not target_device_id and payload.user_id:
        user = db.query(models.User).filter(models.User.id == payload.user_id).first()
        if user:
            target_device_id = user.device_id

    if not target_username and target_device_id:
        for username, account in accounts.items():
            if isinstance(account, dict) and str(account.get("device_id", "")).strip() == target_device_id:
                target_username = username
                break

    if not target_username or target_username not in accounts:
        raise HTTPException(status_code=404, detail="Registered account not found for this user")

    account = accounts.get(target_username) or {}
    if not isinstance(account, dict):
        account = {}

    new_password = _generate_random_password()
    salt = os.urandom(8).hex()
    account["salt"] = salt
    account["password_hash"] = _hash_password(new_password, salt)
    account["password_updated_at"] = datetime.utcnow().isoformat()
    accounts[target_username] = account
    _save_user_accounts(accounts)

    device_id = str(account.get("device_id", "")).strip()
    add_runtime_log(f"[ADMIN] Reset user password: username={target_username}, device={device_id}")
    log_user_operation(
        "reset_user_password",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/users/reset_password",
        username=target_username,
        device_id=device_id,
        detail="random_password_generated",
    )
    return {
        "status": "success",
        "username": target_username,
        "device_id": device_id,
        "new_password": new_password,
    }


@router.post("/users/ban")
async def set_user_ban_status(
    payload: SetUserBanSchema,
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin),
):
    accounts = _load_user_accounts()
    if not accounts:
        raise HTTPException(status_code=404, detail="No registered accounts found")

    target_username, target_device_id = _resolve_target_username(
        payload.username,
        payload.device_id,
        payload.user_id,
        db,
        accounts,
    )
    if not target_username or target_username not in accounts:
        raise HTTPException(status_code=404, detail="Registered account not found for this user")

    reason = (payload.reason or "").strip()
    account_store.set_account_ban_status(
        target_username,
        banned=bool(payload.banned),
        reason=reason,
        accounts=accounts,
    )

    add_runtime_log(
        f"[ADMIN] User ban status changed: username={target_username}, device={target_device_id}, banned={bool(payload.banned)}"
    )
    log_user_operation(
        "set_user_ban",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/users/ban",
        username=target_username,
        device_id=target_device_id,
        detail=f"banned={bool(payload.banned)}, reason={reason}",
    )
    return {
        "status": "success",
        "username": target_username,
        "device_id": target_device_id,
        "is_banned": bool(payload.banned),
        "reason": reason,
    }


@router.post("/users/add_time")
async def add_time_to_user(
    action: schemas.AdminAddTime,
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin)
):
    user = db.query(models.User).filter(models.User.id == action.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not account_store.get_username_by_device_id(user.device_id):
        raise HTTPException(status_code=400, detail="该用户为游客账号，请先注册后再操作")

    now = datetime.utcnow()
    if user.expires_at and user.expires_at > now:
        user.expires_at += timedelta(minutes=action.minutes)
    else:
        user.expires_at = now + timedelta(minutes=action.minutes)

    db.commit()
    log_user_operation(
        "add_user_time",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/users/add_time",
        device_id=user.device_id,
        detail=f"user_id={user.id}, minutes={action.minutes}",
    )
    return {"message": "success", "new_expires_at": user.expires_at}


@router.post("/users/set_membership")
async def set_user_membership(
    payload: SetUserMembershipSchema,
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin),
):
    user = db.query(models.User).filter(models.User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not account_store.get_username_by_device_id(user.device_id):
        raise HTTPException(status_code=400, detail="该用户为游客账号，请先注册后再操作")

    version = (payload.version or "").strip().lower()
    allowed_versions = {"trial", "basic", "advanced", "flagship"}
    if version not in allowed_versions:
        raise HTTPException(status_code=400, detail="Invalid version")

    days = int(payload.days or 0)
    days = max(0, min(days, 3650))
    now = datetime.utcnow()
    user.version = version
    user.expires_at = now + timedelta(days=days) if days > 0 else now
    db.commit()
    db.refresh(user)

    add_runtime_log(
        f"[ADMIN] Membership updated: user_id={user.id}, device={user.device_id}, version={version}, days={days}"
    )
    log_user_operation(
        "set_user_membership",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/users/set_membership",
        device_id=user.device_id,
        detail=f"user_id={user.id}, version={version}, days={days}",
    )
    return {
        "status": "success",
        "user_id": user.id,
        "version": user.version,
        "expires_at": user.expires_at,
    }


@router.get("/orders", response_model=List[schemas.OrderInfo])
async def list_orders(status: str = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db), authorized: bool = Depends(verify_admin)):
    q = db.query(models.PurchaseOrder)
    if status:
        q = q.filter(models.PurchaseOrder.status == status)
    orders = q.order_by(models.PurchaseOrder.created_at.desc()).offset(skip).limit(limit).all()
    return orders


@router.get("/orders/stats")
async def order_stats(db: Session = Depends(get_db), authorized: bool = Depends(verify_admin)):
    rows = (
        db.query(
            models.PurchaseOrder.status,
            func.count(models.PurchaseOrder.id),
            func.coalesce(func.sum(models.PurchaseOrder.amount), 0.0),
        )
        .group_by(models.PurchaseOrder.status)
        .all()
    )

    stats_by_status: Dict[str, Dict[str, float]] = {}
    total_orders = 0
    total_amount = 0.0
    for status, count, amount in rows:
        c = int(count or 0)
        a = float(amount or 0.0)
        stats_by_status[str(status)] = {
            "count": c,
            "amount": round(a, 2),
        }
        total_orders += c
        total_amount += a

    completed_amount = float(stats_by_status.get("completed", {}).get("amount", 0.0))
    waiting_amount = float(stats_by_status.get("waiting_verification", {}).get("amount", 0.0))
    pending_amount = float(stats_by_status.get("pending", {}).get("amount", 0.0))
    rejected_amount = float(stats_by_status.get("rejected", {}).get("amount", 0.0))

    return {
        "total_orders": total_orders,
        "total_amount": round(total_amount, 2),
        "completed_amount": round(completed_amount, 2),
        "waiting_amount": round(waiting_amount, 2),
        "pending_amount": round(pending_amount, 2),
        "rejected_amount": round(rejected_amount, 2),
        "by_status": stats_by_status,
    }


@router.post("/orders/approve")
async def approve_order(
    action: schemas.AdminOrderAction,
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin)
):
    order = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.order_code == action.order_code).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if action.action == "reject":
        order.status = "rejected"
        db.commit()
        account_store.update_order_invite_status(order.order_code, "rejected", reason="order_rejected")
        add_runtime_log(f"[ORDER] Rejected order={order.order_code}")
        log_user_operation(
            "order_reject",
            status="success",
            actor="admin",
            method="POST",
            path="/api/admin/orders/approve",
            device_id=order.user.device_id if order.user else "",
            detail=f"order_code={order.order_code}",
        )
        await ws_hub.push_device_event(order.user.device_id, {
            "event": "membership_rejected",
            "order_code": order.order_code,
            "status": "rejected",
            "message": "订单审核未通过，请联系管理员或重新提交。",
        })
        return {"status": "rejected"}

    if order.status == "completed":
        return {"status": "already_completed"}

    user = order.user
    now = datetime.utcnow()

    base_prices = {
        "trial": 0,
        "basic": 58,
        "advanced": 128,
        "flagship": 298,
    }

    current_value_remaining = 0
    if user.expires_at and user.expires_at > now and user.version != "trial":
        remaining_minutes = (user.expires_at - now).total_seconds() / 60
        price_per_month = base_prices.get(user.version, 0)
        price_per_minute = price_per_month / (30 * 24 * 60)
        current_value_remaining = remaining_minutes * price_per_minute

    new_duration_days = order.duration_days
    target_price_per_month = base_prices.get(order.target_version, 0)
    target_price_per_minute = target_price_per_month / (30 * 24 * 60)

    converted_minutes = 0
    if target_price_per_minute > 0:
        converted_minutes = current_value_remaining / target_price_per_minute

    total_new_minutes = (new_duration_days * 24 * 60) + converted_minutes

    # 续费加送：同版本续费即可生效（不区分是否已到期）
    is_same_version_renewal = user.version != "trial" and (user.version == order.target_version)
    bonus_days = purchase_manager.get_renewal_bonus_days(order.duration_days) if is_same_version_renewal else 0
    bonus_minutes = bonus_days * 24 * 60

    user.version = order.target_version
    user.expires_at = now + timedelta(minutes=total_new_minutes + bonus_minutes)

    order.status = "completed"
    db.commit()
    referral_reward_info = None
    reward_record = account_store.claim_order_invite_reward(order.order_code)
    if reward_record:
        inviter_device_id = str(reward_record.get("inviter_device_id", "")).strip()
        if inviter_device_id:
            reward_days = int(reward_record.get("reward_days", 30) or 30)
            reward_days = max(1, min(reward_days, 365))
            inviter_user = user_service.get_or_create_user(db, inviter_device_id)
            inviter_base = now
            if inviter_user.expires_at and inviter_user.expires_at > now:
                inviter_base = inviter_user.expires_at
            inviter_user.expires_at = inviter_base + timedelta(days=reward_days)
            if inviter_user.version == "trial":
                inviter_user.version = "basic"
            db.commit()
            referral_reward_info = {
                "inviter_device_id": inviter_device_id,
                "reward_days": reward_days,
                "bonus_token": str(reward_record.get("bonus_token", "")).strip(),
                "invite_code": str(reward_record.get("invite_code", "")).strip(),
                "inviter_username": str(reward_record.get("inviter_username", "")).strip(),
            }
            add_runtime_log(
                f"[ORDER] Referral rewarded: order={order.order_code}, inviter_device={inviter_device_id}, reward_days={reward_days}"
            )
            await ws_hub.push_device_event(inviter_device_id, {
                "event": "invite_reward_credited",
                "order_code": order.order_code,
                "reward_days": reward_days,
                "bonus_token": referral_reward_info["bonus_token"],
                "message": f"你的邀请码已生效，已获赠 {reward_days} 天会员权益。",
            })
        else:
            account_store.update_order_invite_status(order.order_code, "invalid", reason="missing_inviter_device")

    add_runtime_log(
        f"[ORDER] Approved order={order.order_code}, device={user.device_id}, version={user.version}, bonus_days={bonus_days}"
    )
    log_user_operation(
        "order_approve",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/orders/approve",
        device_id=user.device_id,
        detail=f"order_code={order.order_code}, version={user.version}, bonus_days={bonus_days}",
    )
    await ws_hub.push_device_event(user.device_id, {
        "event": "membership_approved",
        "order_code": order.order_code,
        "status": "completed",
        "version": user.version,
        "expires_at": user.expires_at.isoformat() if user.expires_at else None,
        "bonus_days": int(bonus_days),
        "referral_bonus_token": referral_reward_info["bonus_token"] if referral_reward_info else "",
        "referral_bonus_days": int(referral_reward_info["reward_days"]) if referral_reward_info else 0,
        "message": "会员审批已通过，权益已生效。",
    })

    return {
        "status": "success",
        "new_expiry": user.expires_at,
        "bonus_days": bonus_days,
        "referral_reward": referral_reward_info,
    }


# --- System Configuration ---
class AdminConfigUpdate(BaseModel):
    auto_analysis_enabled: bool
    use_smart_schedule: bool
    fixed_interval_minutes: int
    schedule_plan: Optional[List[dict]] = None
    lhb_enabled: Optional[bool] = None
    lhb_days: Optional[int] = None
    lhb_min_amount: Optional[int] = None
    email_config: Optional[dict] = None
    api_keys: Optional[dict] = None
    community_config: Optional[dict] = None
    referral_config: Optional[dict] = None
    pricing_config: Optional[dict] = None


@router.get("/watchlist_stats")
async def get_watchlist_stats(authorized: bool = Depends(verify_admin)):
    return {
        "status": "success",
        "data": watchlist_stats.list_favorite_stats()
    }


@router.get("/config")
async def get_admin_config(authorized: bool = Depends(verify_admin)):
    config = SYSTEM_CONFIG.copy()

    if 'api_keys' not in config:
        config['api_keys'] = {}

    if not config['api_keys'].get('deepseek'):
        config['api_keys']['deepseek'] = os.getenv('DEEPSEEK_API_KEY', '')

    config['lhb_enabled'] = lhb_manager.config['enabled']
    config['lhb_days'] = lhb_manager.config['days']
    config['lhb_min_amount'] = lhb_manager.config['min_amount']

    if 'email_config' not in config:
        config['email_config'] = {
            "enabled": False,
            "smtp_server": "",
            "smtp_port": 465,
            "smtp_user": "",
            "smtp_password": "",
            "recipient_email": ""
        }
    if 'api_keys' not in config:
        config['api_keys'] = {
            "deepseek": "",
            "aliyun": "",
            "other": ""
        }
    if 'community_config' not in config:
        config['community_config'] = {
            "qq_group_number": "",
            "qq_group_link": "",
            "welcome_text": "欢迎加入技术交流群，获取版本更新与使用答疑。",
        }
    if 'referral_config' not in config:
        config['referral_config'] = {
            "enabled": True,
            "reward_days": 30,
            "share_base_url": "",
            "share_template": "我在用涨停狙击手，注册链接：{invite_link}，邀请码：{invite_code}。注册后在充值页填写邀请码，可获得赠送权益。",
        }

    config['pricing_config'] = purchase_manager.PRICING_CONFIG
    return config


@router.post("/config")
async def update_admin_config(config: AdminConfigUpdate, authorized: bool = Depends(verify_admin)):
    if config.auto_analysis_enabled is not None:
        SYSTEM_CONFIG["auto_analysis_enabled"] = config.auto_analysis_enabled
    if config.use_smart_schedule is not None:
        SYSTEM_CONFIG["use_smart_schedule"] = config.use_smart_schedule
    if config.fixed_interval_minutes is not None:
        SYSTEM_CONFIG["fixed_interval_minutes"] = config.fixed_interval_minutes
    if config.schedule_plan:
        SYSTEM_CONFIG["schedule_plan"] = config.schedule_plan

    if config.email_config is not None:
        SYSTEM_CONFIG["email_config"] = config.email_config

    if config.api_keys is not None:
        SYSTEM_CONFIG["api_keys"] = config.api_keys

    if config.community_config is not None:
        SYSTEM_CONFIG["community_config"] = config.community_config

    if config.referral_config is not None:
        SYSTEM_CONFIG["referral_config"] = config.referral_config

    if config.pricing_config is not None:
        SYSTEM_CONFIG["pricing_config"] = config.pricing_config
        purchase_manager.update_pricing(config.pricing_config)

    save_config()
    log_user_operation(
        "update_admin_config",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/config",
        detail="system_config_updated",
    )

    if config.lhb_enabled is not None:
        lhb_manager.update_settings(config.lhb_enabled, config.lhb_days, config.lhb_min_amount)

    return {"status": "success"}


# --- Logs & Monitor ---
def _tail_file_lines(path: Path, max_lines: int) -> List[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = [x.rstrip("\n") for x in f.readlines()]
        return all_lines[-max_lines:]
    except Exception:
        return []


def _safe_float(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _safe_int(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _ai_usage_from_entry(entry: Dict[str, Any]) -> Dict[str, int]:
    meta = entry.get("meta") or {}
    usage = meta.get("usage") if isinstance(meta, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = max(0, _safe_int(usage.get("prompt_tokens", 0)))
    completion_tokens = max(0, _safe_int(usage.get("completion_tokens", 0)))
    total_tokens = max(0, _safe_int(usage.get("total_tokens", prompt_tokens + completion_tokens)))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _preview_data(data: Any, max_chars: int = 180) -> str:
    try:
        if isinstance(data, (dict, list)):
            raw = json.dumps(data, ensure_ascii=False)
        else:
            raw = str(data)
    except Exception:
        raw = str(data)
    if len(raw) <= max_chars:
        return raw
    return raw[:max_chars] + "..."


@router.get("/logs/system")
async def get_system_logs(lines: int = 200, authorized: bool = Depends(verify_admin)):
    safe_lines = max(20, min(int(lines or 200), 2000))
    file_logs = _tail_file_lines(BASE_DIR / "app.log", safe_lines)
    runtime_logs = get_runtime_logs(limit=safe_lines)

    merged = (file_logs + runtime_logs)[-safe_lines:]
    if not merged:
        merged = ["No system logs yet."]
    return {"logs": merged}


@router.get("/logs/login")
async def get_login_logs(authorized: bool = Depends(verify_admin)):
    logs = get_recent_user_operations(limit=1000)
    return [
        x for x in logs
        if str(x.get("action", "")).endswith("login") or "/login" in str(x.get("path", ""))
    ][:300]


@router.get("/logs/user_ops")
async def get_user_operation_logs(limit: int = 300, authorized: bool = Depends(verify_admin)):
    safe_limit = max(50, min(int(limit or 300), 2000))
    return {"logs": get_recent_user_operations(limit=safe_limit)}


@router.get("/monitor/ai_cache")
async def get_ai_cache_stats(limit: int = 100, authorized: bool = Depends(verify_admin)):
    safe_limit = max(20, min(int(limit or 100), 500))
    all_items = []
    for key, entry in ai_cache.cache.items():
        if not isinstance(entry, dict):
            continue
        usage = _ai_usage_from_entry(entry)
        prompt_tokens = usage["prompt_tokens"]
        completion_tokens = usage["completion_tokens"]
        input_cost = (prompt_tokens / 1_000_000.0) * 2.0
        output_cost = (completion_tokens / 1_000_000.0) * 3.0
        all_items.append({
            "key": key,
            "timestamp": _safe_int(entry.get("timestamp", 0)),
            "usage": usage,
            "cost_cny": round(input_cost + output_cost, 6),
            "preview": _preview_data(entry.get("data")),
        })

    all_items.sort(key=lambda x: x["timestamp"], reverse=True)
    recent_items = all_items[:safe_limit]

    total_input_tokens = sum(x["usage"]["prompt_tokens"] for x in all_items)
    total_output_tokens = sum(x["usage"]["completion_tokens"] for x in all_items)
    total_cost = (total_input_tokens / 1_000_000.0) * 2.0 + (total_output_tokens / 1_000_000.0) * 3.0

    return {
        "total_keys": len(ai_cache.cache),
        "visible_keys": len(recent_items),
        "pricing": {
            "input_per_million_cny": 2.0,
            "output_per_million_cny": 3.0,
        },
        "totals": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "total_cost_cny": round(total_cost, 6),
        },
        "items": recent_items,
    }


@router.get("/monitor/ai_cache/item")
async def get_ai_cache_item(key: str, authorized: bool = Depends(verify_admin)):
    cache_key = (key or "").strip()
    if not cache_key:
        raise HTTPException(status_code=400, detail="Missing key")
    entry = ai_cache.get_entry(cache_key)
    if not entry:
        raise HTTPException(status_code=404, detail="Cache key not found")

    usage = _ai_usage_from_entry(entry)
    prompt_tokens = usage["prompt_tokens"]
    completion_tokens = usage["completion_tokens"]
    total_cost = (prompt_tokens / 1_000_000.0) * 2.0 + (completion_tokens / 1_000_000.0) * 3.0

    return {
        "key": cache_key,
        "timestamp": _safe_int(entry.get("timestamp", 0)),
        "meta": entry.get("meta", {}),
        "usage": usage,
        "cost_cny": round(total_cost, 6),
        "data": entry.get("data"),
    }


# --- LHB Admin ---
class AdminLHBRangeRequest(BaseModel):
    start_date: str
    end_date: str


class AdminLHBSettingsRequest(BaseModel):
    enabled: bool
    days: int
    min_amount: int


@router.get("/lhb/overview")
async def get_lhb_overview(
    start_date: str = "",
    end_date: str = "",
    authorized: bool = Depends(verify_admin),
):
    lhb_manager.load_config()
    s = (start_date or "").strip()
    e = (end_date or "").strip()
    if not s or not e:
        today = datetime.utcnow().date()
        s = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        e = today.strftime("%Y-%m-%d")
    return {
        "status": "success",
        "data": lhb_manager.get_summary(start_date=s, end_date=e),
        "range": {"start_date": s, "end_date": e},
    }


@router.post("/lhb/settings")
async def update_lhb_settings(
    payload: AdminLHBSettingsRequest,
    authorized: bool = Depends(verify_admin),
):
    lhb_manager.update_settings(payload.enabled, payload.days, payload.min_amount)
    add_runtime_log(
        f"[LHB] Updated settings: enabled={payload.enabled}, days={payload.days}, min_amount={payload.min_amount}"
    )
    return {"status": "success", "config": lhb_manager.config}


@router.get("/lhb/date_data")
async def get_lhb_date_data(date: str, authorized: bool = Depends(verify_admin)):
    date_str = (date or "").strip()
    if not date_str:
        raise HTTPException(status_code=400, detail="Missing date")
    return {
        "status": "success",
        "date": date_str,
        "data": lhb_manager.get_daily_data(date_str),
    }


@router.post("/lhb/sync_missing")
async def sync_lhb_missing(
    payload: AdminLHBRangeRequest,
    background_tasks: BackgroundTasks,
    authorized: bool = Depends(verify_admin),
):
    start_date = (payload.start_date or "").strip()
    end_date = (payload.end_date or "").strip()
    if not start_date or not end_date:
        raise HTTPException(status_code=400, detail="Missing date range")

    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="Date format must be YYYY-MM-DD")

    missing_dates = lhb_manager.get_missing_dates(start_date, end_date)
    if not missing_dates:
        return {
            "status": "no_missing",
            "message": "No missing LHB dates in selected range.",
            "missing_dates": [],
        }

    if lhb_manager.is_syncing:
        return {
            "status": "busy",
            "message": "LHB sync is already running.",
            "missing_dates": missing_dates,
        }

    add_runtime_log(
        f"[LHB] Start missing-date sync: {start_date} ~ {end_date}, count={len(missing_dates)}"
    )
    background_tasks.add_task(
        lhb_manager.fetch_and_update_data,
        add_runtime_log,
        None,
        missing_dates,
    )
    return {
        "status": "started",
        "message": "Missing-date sync started.",
        "missing_dates": missing_dates,
    }
