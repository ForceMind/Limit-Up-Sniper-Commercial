from fastapi import APIRouter, Depends, HTTPException, Header, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from app.db import models, schemas, database
from typing import List, Optional, Dict, Any, Tuple
import secrets
import os
import json
import time
import hashlib
import base64
import re
import shutil
import tempfile
import zipfile
import subprocess
from pathlib import Path
from app.core.config_manager import SYSTEM_CONFIG, save_config
from app.core.lhb_manager import lhb_manager
from app.core import user_service, purchase_manager, account_store
from app.core import watchlist_stats
from app.core.ai_cache import ai_cache
from app.core.data_provider import data_provider
from app.core.runtime_logs import get_runtime_logs, add_runtime_log
from app.core.ws_hub import ws_hub
from app.core.operation_log import log_user_operation, get_recent_user_operations
from app.core.news_admin_store import (
    build_news_item_id,
    load_news_analysis_records,
    load_news_history,
    save_news_analysis_records,
    save_news_history,
)
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel
from zoneinfo import ZoneInfo
from collections import deque

router = APIRouter()

# --- Security Configuration ---
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
ADMIN_SECRET_FILE = DATA_DIR / "admin_token.txt"
ADMIN_CREDENTIALS_FILE = DATA_DIR / "admin_credentials.json"
ADMIN_SESSIONS_FILE = DATA_DIR / "admin_sessions.json"
ADMIN_PANEL_PATH_FILE = DATA_DIR / "admin_panel_path.json"
USER_ACCOUNTS_FILE = DATA_DIR / "user_accounts.json"
SEAT_MAPPINGS_FILE = DATA_DIR / "seat_mappings.json"
VIP_SEATS_FILE = DATA_DIR / "vip_seats.json"
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_ATTEMPTS = 5
SESSION_EXPIRE_HOURS = 24
failed_attempts: Dict[str, List[float]] = {}
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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


def _decode_transport_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.b64decode(padded.encode("utf-8"), validate=False).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _read_transport_field(plain: Optional[str], encoded: Optional[str]) -> str:
    decoded = _decode_transport_text(encoded)
    if decoded:
        return decoded
    return str(plain or "")


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
    username: Optional[str] = ""
    password: Optional[str] = ""
    username_b64: Optional[str] = ""
    password_b64: Optional[str] = ""


class UpdatePasswordSchema(BaseModel):
    old_password: Optional[str] = ""
    new_password: Optional[str] = ""
    old_password_b64: Optional[str] = ""
    new_password_b64: Optional[str] = ""


class UpdateAdminAccountSchema(BaseModel):
    old_password: Optional[str] = ""
    new_password: Optional[str] = ""
    new_username: Optional[str] = ""
    old_password_b64: Optional[str] = ""
    new_password_b64: Optional[str] = ""
    new_username_b64: Optional[str] = ""


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
    username = _read_transport_field(data.username, data.username_b64).strip()
    password = _read_transport_field(data.password, data.password_b64).strip()

    if username != cred.get("username") or not _verify_admin_password(password, cred):
        attempts.append(now_ts)
        failed_attempts[client_ip] = attempts
        add_runtime_log(f"[后台] 登录失败: ip={client_ip}, username={username}")
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
    add_runtime_log(f"[后台] 登录成功: ip={client_ip}, username={cred.get('username')}")
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
    new_password = _read_transport_field(data.new_password, data.new_password_b64).strip()
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    cred = _load_admin_credentials()
    old_password = _read_transport_field(data.old_password, data.old_password_b64).strip()
    if old_password and not _verify_admin_password(old_password, cred):
        raise HTTPException(status_code=403, detail="Old password is incorrect")

    salt = os.urandom(8).hex()
    cred["salt"] = salt
    cred["password_hash"] = _hash_password(new_password, salt)
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


@router.post("/update_account")
async def update_admin_account(
    data: UpdateAdminAccountSchema,
    authorized: bool = Depends(verify_admin),
):
    cred = _load_admin_credentials()

    old_password = _read_transport_field(data.old_password, data.old_password_b64).strip()
    if old_password and not _verify_admin_password(old_password, cred):
        raise HTTPException(status_code=403, detail="Old password is incorrect")

    new_username = _read_transport_field(data.new_username, data.new_username_b64).strip()
    new_password = _read_transport_field(data.new_password, data.new_password_b64).strip()

    if not new_username and not new_password:
        raise HTTPException(status_code=400, detail="Please provide new_username or new_password")

    if new_username:
        if len(new_username) < 3 or len(new_username) > 32:
            raise HTTPException(status_code=400, detail="Username must be 3-32 characters")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", new_username):
            raise HTTPException(status_code=400, detail="Username allows letters, digits, ., _, -")
        cred["username"] = new_username

    if new_password:
        if len(new_password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        salt = os.urandom(8).hex()
        cred["salt"] = salt
        cred["password_hash"] = _hash_password(new_password, salt)

    cred["updated_at"] = datetime.utcnow().isoformat()
    _save_json(ADMIN_CREDENTIALS_FILE, cred)

    # Force all sessions to re-login after account change.
    _save_sessions({})
    log_user_operation(
        "update_admin_account",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/update_account",
        username=cred.get("username", ""),
        detail="admin_account_updated",
    )
    return {"status": "success", "message": "Admin account updated", "username": cred.get("username", "")}


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
    add_runtime_log(f"[后台] 已更新后台路径: {final_path}")
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
    device_to_username = _device_username_map(accounts)

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
        used_ai = max(0, int(u.daily_ai_count or 0))
        used_raid = max(0, int(u.daily_raid_count or 0))
        used_review = max(0, int(u.daily_review_count or 0))
        quota_ai = max(0, int(quotas.get("ai", 0)))
        quota_raid = max(0, int(quotas.get("raid", 0)))
        quota_review = max(0, int(quotas.get("review", 0))
        )
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
            "daily_ai_count": used_ai,
            "daily_raid_count": used_raid,
            "daily_review_count": used_review,
            "quota_ai": quota_ai,
            "quota_raid": quota_raid,
            "quota_review": quota_review,
            "remaining_ai": max(0, quota_ai - used_ai),
            "remaining_raid": max(0, quota_raid - used_raid),
            "remaining_review": max(0, quota_review - used_review),
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
    add_runtime_log(f"[后台] 重置用户密码: username={target_username}, device={device_id}")
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
    q = db.query(models.PurchaseOrder).options(joinedload(models.PurchaseOrder.user))
    if status:
        q = q.filter(models.PurchaseOrder.status == status)
    orders = q.order_by(models.PurchaseOrder.created_at.desc()).offset(skip).limit(limit).all()
    accounts = _load_user_accounts()
    device_to_username = _device_username_map(accounts)

    result: List[Dict[str, Any]] = []
    for order in orders:
        user_device_id = str(order.user.device_id or "").strip() if order.user else ""
        username = device_to_username.get(user_device_id, "")
        result.append({
            "id": int(order.id),
            "order_code": str(order.order_code),
            "target_version": str(order.target_version),
            "amount": float(order.amount or 0.0),
            "duration_days": int(order.duration_days or 0),
            "status": str(order.status or ""),
            "created_at": order.created_at,
            "user_id": int(order.user_id) if order.user_id is not None else None,
            "user_device_id": user_device_id,
            "username": username,
            "account_type": "registered" if username else "guest",
        })
    return result


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


@router.get("/referrals")
async def list_referrals(
    status: str = "rewarded",
    page: int = 1,
    page_size: int = 50,
    keyword: str = "",
    authorized: bool = Depends(verify_admin),
    db: Session = Depends(get_db),
):
    records = account_store.load_referral_records()
    order_invites = records.get("order_invites", {})
    if not isinstance(order_invites, dict):
        order_invites = {}

    normalized_status = (status or "all").strip().lower()
    if normalized_status not in {"all", "pending", "rewarded", "rejected", "cancelled", "invalid"}:
        raise HTTPException(status_code=400, detail="Invalid status filter")

    keyword_lc = (keyword or "").strip().lower()
    accounts = _load_user_accounts()
    device_to_username = _device_username_map(accounts)

    order_codes = [str(code).strip() for code in order_invites.keys() if str(code).strip()]
    order_map: Dict[str, models.PurchaseOrder] = {}
    if order_codes:
        rows = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.order_code.in_(order_codes)).all()
        order_map = {str(x.order_code): x for x in rows}

    items: List[Dict[str, Any]] = []
    for order_code, info in order_invites.items():
        if not isinstance(info, dict):
            continue
        current_status = str(info.get("status", "")).strip().lower()
        if normalized_status != "all" and current_status != normalized_status:
            continue

        invite_code = str(info.get("invite_code", "")).strip()
        inviter_username = str(info.get("inviter_username", "")).strip()
        inviter_device_id = str(info.get("inviter_device_id", "")).strip()
        invitee_device_id = str(info.get("invitee_device_id", "")).strip()
        invitee_username = device_to_username.get(invitee_device_id, "")
        reason = str(info.get("reason", "")).strip()

        order = order_map.get(str(order_code).strip())
        row = {
            "order_code": str(order_code).strip(),
            "invite_code": invite_code,
            "status": current_status,
            "inviter_username": inviter_username,
            "inviter_device_id": inviter_device_id,
            "invitee_username": invitee_username,
            "invitee_device_id": invitee_device_id,
            "reward_days": int(info.get("reward_days", 0) or 0),
            "bonus_token": str(info.get("bonus_token", "")).strip(),
            "reason": reason,
            "created_at": str(info.get("created_at", "")).strip(),
            "updated_at": str(info.get("updated_at", "")).strip(),
            "rewarded_at": str(info.get("rewarded_at", "")).strip(),
            "order_amount": float(order.amount) if order else 0.0,
            "order_target_version": str(order.target_version) if order else "",
            "order_duration_days": int(order.duration_days) if order else 0,
            "order_status": str(order.status) if order else "",
        }
        if keyword_lc:
            haystack = " ".join([
                row["order_code"],
                row["invite_code"],
                row["inviter_username"],
                row["inviter_device_id"],
                row["invitee_username"],
                row["invitee_device_id"],
                row["reason"],
            ]).lower()
            if keyword_lc not in haystack:
                continue
        items.append(row)

    items.sort(
        key=lambda x: (
            x.get("rewarded_at") or "",
            x.get("created_at") or "",
            x.get("order_code") or "",
        ),
        reverse=True,
    )

    safe_page = max(1, int(page or 1))
    safe_size = max(10, min(int(page_size or 50), 200))
    total = len(items)
    start = (safe_page - 1) * safe_size
    paged = items[start:start + safe_size]

    return {
        "status": "success",
        "total": total,
        "page": safe_page,
        "page_size": safe_size,
        "total_pages": max(1, (total + safe_size - 1) // safe_size),
        "items": paged,
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
        add_runtime_log(f"[订单] 已驳回订单={order.order_code}")
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
    auto_analysis_enabled: Optional[bool] = None
    use_smart_schedule: Optional[bool] = None
    fixed_interval_minutes: Optional[int] = None
    schedule_plan: Optional[List[dict]] = None
    lhb_enabled: Optional[bool] = None
    lhb_days: Optional[int] = None
    lhb_min_amount: Optional[int] = None
    email_config: Optional[dict] = None
    api_keys: Optional[dict] = None
    data_provider_config: Optional[dict] = None
    community_config: Optional[dict] = None
    referral_config: Optional[dict] = None
    pricing_config: Optional[dict] = None


@router.get("/watchlist_stats")
async def get_watchlist_stats(
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin),
):
    stats = watchlist_stats.list_favorite_stats()

    # Fill missing stock names from current watchlist cache when possible.
    watchlist_map: Dict[str, str] = {}
    watchlist_file = DATA_DIR / "watchlist.json"
    if watchlist_file.exists():
        try:
            with open(watchlist_file, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    code = str(row.get("code", "")).strip().lower()
                    name = str(row.get("name", "")).strip()
                    if code and name:
                        watchlist_map[code] = name
                        digits = "".join(filter(str.isdigit, code))
                        if len(digits) == 6:
                            watchlist_map[digits] = name
        except Exception:
            pass

    for item in stats:
        code = str(item.get("code", "")).strip().lower()
        if item.get("name"):
            continue
        fallback_name = watchlist_map.get(code) or watchlist_map.get("".join(filter(str.isdigit, code)))
        if fallback_name:
            item["name"] = fallback_name

    return {
        "status": "success",
        "storage_file": "backend/data/watchlist_stats.json",
        "data": stats,
    }


@router.get("/watchlist_stats/users")
async def get_watchlist_stat_users(
    code: str,
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin),
):
    code_norm = str(code or "").strip().lower()
    if not code_norm:
        raise HTTPException(status_code=400, detail="Missing code")

    user_items = watchlist_stats.list_favorite_users(code_norm)
    if not user_items:
        return {"status": "success", "code": code_norm, "users": []}

    ids: List[int] = []
    for item in user_items:
        try:
            ids.append(int(str(item.get("user_id", "")).strip()))
        except Exception:
            continue
    db_users: Dict[int, models.User] = {}
    if ids:
        rows = db.query(models.User).filter(models.User.id.in_(ids)).all()
        db_users = {int(x.id): x for x in rows}

    accounts = _load_user_accounts()
    device_to_username = _device_username_map(accounts)

    users: List[Dict[str, Any]] = []
    for item in user_items:
        raw_user_id = str(item.get("user_id", "")).strip()
        try:
            uid = int(raw_user_id)
        except Exception:
            uid = -1
        user_row = db_users.get(uid)
        device_id = str(user_row.device_id).strip() if user_row else ""
        username = device_to_username.get(device_id, "")
        users.append(
            {
                "user_id": uid if uid > 0 else raw_user_id,
                "username": username,
                "device_id": device_id,
                "joined_at": str(item.get("joined_at", "")).strip(),
                "account_type": "registered" if username else "guest",
            }
        )

    users.sort(key=lambda x: str(x.get("joined_at", "")), reverse=True)
    return {"status": "success", "code": code_norm, "users": users}


@router.get("/overview/today")
async def get_today_overview(
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin),
):
    now_sh, start_sh, end_sh, start_utc, end_utc = _today_range_utc_naive()

    # Users
    new_users = (
        db.query(models.User)
        .filter(models.User.created_at >= start_utc, models.User.created_at < end_utc)
        .all()
    )
    total_new_users = len(new_users)

    accounts = _load_user_accounts()
    device_to_username = _device_username_map(accounts)
    registered_devices = set(device_to_username.keys())
    new_registered_users = sum(1 for u in new_users if str(u.device_id or "").strip() in registered_devices)
    new_guest_users = max(0, total_new_users - new_registered_users)

    registered_account_today = 0
    for account in accounts.values():
        if not isinstance(account, dict):
            continue
        dt_sh = _as_shanghai_datetime(account.get("created_at"), assume_utc_when_naive=True)
        if dt_sh and start_sh <= dt_sh < end_sh:
            registered_account_today += 1

    # Orders
    today_orders = (
        db.query(models.PurchaseOrder)
        .filter(models.PurchaseOrder.created_at >= start_utc, models.PurchaseOrder.created_at < end_utc)
        .all()
    )
    orders_total = len(today_orders)
    orders_completed = 0
    orders_waiting = 0
    orders_pending = 0
    orders_rejected = 0
    amount_total = 0.0
    amount_completed = 0.0
    for order in today_orders:
        amount = float(order.amount or 0.0)
        amount_total += amount
        status = str(order.status or "").strip().lower()
        if status == "completed":
            orders_completed += 1
            amount_completed += amount
        elif status == "waiting_verification":
            orders_waiting += 1
        elif status == "pending":
            orders_pending += 1
        elif status in {"rejected", "cancelled"}:
            orders_rejected += 1

    # Login and operation summary
    login_success_count = 0
    admin_login_count = 0
    unique_login_devices = set()
    user_op_count = 0
    for row in _iter_user_operation_logs():
        dt_sh = _as_shanghai_datetime(row.get("time"), assume_utc_when_naive=False)
        if not dt_sh:
            continue
        if dt_sh < start_sh:
            break
        if dt_sh >= end_sh:
            continue

        user_op_count += 1
        action = str(row.get("action", "")).strip().lower()
        status = str(row.get("status", "")).strip().lower()
        did = str(row.get("device_id", "")).strip()

        if status == "success" and action == "user_login":
            login_success_count += 1
            if did:
                unique_login_devices.add(did)
        elif status == "success" and action == "admin_login":
            admin_login_count += 1

    # Referral
    referrals_today = 0
    referrals_rewarded_today = 0
    referral_records = account_store.load_referral_records()
    order_invites = referral_records.get("order_invites", {}) if isinstance(referral_records, dict) else {}
    if isinstance(order_invites, dict):
        for info in order_invites.values():
            if not isinstance(info, dict):
                continue
            created_sh = _as_shanghai_datetime(info.get("created_at"), assume_utc_when_naive=True)
            if created_sh and start_sh <= created_sh < end_sh:
                referrals_today += 1
            rewarded_sh = _as_shanghai_datetime(info.get("rewarded_at"), assume_utc_when_naive=True)
            if rewarded_sh and start_sh <= rewarded_sh < end_sh:
                referrals_rewarded_today += 1

    # News / AI
    news_history = load_news_history()
    news_today = 0
    for item in news_history:
        dt_sh = _as_shanghai_datetime(item.get("timestamp"), assume_utc_when_naive=False)
        if not dt_sh:
            continue
        if dt_sh < start_sh:
            break
        if dt_sh >= end_sh:
            continue
        news_today += 1

    analysis_records = load_news_analysis_records()
    ai_analysis_today = 0
    for row in analysis_records:
        dt_sh = _as_shanghai_datetime(row.get("analyzed_at"), assume_utc_when_naive=True)
        if not dt_sh:
            continue
        if start_sh <= dt_sh < end_sh:
            ai_analysis_today += 1

    # API call stats for today
    api_calls_today = 0
    api_failed_today = 0
    api_unique_devices = set()
    ai_api_calls_today = 0
    for row in _iter_user_operation_logs():
        dt_sh = _as_shanghai_datetime(row.get("time"), assume_utc_when_naive=False)
        if not dt_sh:
            continue
        if dt_sh < start_sh:
            break
        if dt_sh >= end_sh:
            continue
        action = str(row.get("action", "")).strip().lower()
        path = str(row.get("path", "")).strip()
        status = str(row.get("status", "")).strip().lower()
        device_id = str(row.get("device_id", "")).strip()

        if action == "api_call" and path.startswith("/api/"):
            api_calls_today += 1
            if status != "success":
                api_failed_today += 1
            if device_id:
                api_unique_devices.add(device_id)

        if path in {"/api/analyze/stock", "/api/stock/analyze", "/api/pools/analyze"} and status == "success":
            ai_api_calls_today += 1

    # AI token usage (from cache entries created today)
    ai_prompt_tokens_today = 0
    ai_completion_tokens_today = 0
    ai_total_tokens_today = 0
    for entry in ai_cache.cache.values():
        if not isinstance(entry, dict):
            continue
        ts = _safe_int(entry.get("timestamp", 0))
        if ts <= 0:
            continue
        dt_sh = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(SHANGHAI_TZ)
        if not (start_sh <= dt_sh < end_sh):
            continue
        usage = _ai_usage_from_entry(entry)
        ai_prompt_tokens_today += int(usage.get("prompt_tokens", 0) or 0)
        ai_completion_tokens_today += int(usage.get("completion_tokens", 0) or 0)
        ai_total_tokens_today += int(usage.get("total_tokens", 0) or 0)

    ai_cost_today = (ai_prompt_tokens_today / 1_000_000.0) * 2.0 + (ai_completion_tokens_today / 1_000_000.0) * 3.0

    # Current quota consumption snapshot from users table
    users_all = db.query(models.User).all()
    ai_quota_used_total = 0
    for u in users_all:
        ai_quota_used_total += max(0, int(u.daily_ai_count or 0))

    # Realtime system metrics
    runtime_metrics = _collect_system_runtime_metrics(start_sh.strftime("%Y-%m-%d"))
    ws_stats = await ws_hub.snapshot_stats()

    # Online by recent activity in last 5 minutes (best-effort)
    online_recent_devices = set()
    now_ts = now_sh.timestamp()
    for row in _iter_user_operation_logs()[:2000]:
        dt_sh = _as_shanghai_datetime(row.get("time"), assume_utc_when_naive=False)
        if not dt_sh:
            continue
        if now_ts - dt_sh.timestamp() > 300:
            break
        did = str(row.get("device_id", "")).strip()
        if did:
            online_recent_devices.add(did)

    return {
        "date": start_sh.strftime("%Y-%m-%d"),
        "server_time": now_sh.strftime("%Y-%m-%d %H:%M:%S"),
        "users": {
            "new_total": total_new_users,
            "new_registered": new_registered_users,
            "new_guest": new_guest_users,
            "registered_accounts": registered_account_today,
        },
        "orders": {
            "total": orders_total,
            "completed": orders_completed,
            "waiting_verification": orders_waiting,
            "pending": orders_pending,
            "rejected_or_cancelled": orders_rejected,
            "amount_total": round(amount_total, 2),
            "amount_completed": round(amount_completed, 2),
        },
        "logins": {
            "user_login_success": login_success_count,
            "admin_login_success": admin_login_count,
            "unique_user_devices": len(unique_login_devices),
        },
        "operations": {
            "user_operations": user_op_count,
        },
        "referrals": {
            "created": referrals_today,
            "rewarded": referrals_rewarded_today,
        },
        "news": {
            "fetched": news_today,
            "ai_analyzed_batches": ai_analysis_today,
        },
        "runtime": {
            "ws": ws_stats,
            "online_recent_devices_5m": len(online_recent_devices),
            **runtime_metrics,
        },
        "api_usage": {
            "api_calls_today": api_calls_today,
            "api_failed_today": api_failed_today,
            "api_unique_devices_today": len(api_unique_devices),
            "ai_calls_today": ai_api_calls_today,
        },
        "ai_usage": {
            "daily_ai_quota_used_total": int(ai_quota_used_total),
            "prompt_tokens_today": int(ai_prompt_tokens_today),
            "completion_tokens_today": int(ai_completion_tokens_today),
            "total_tokens_today": int(ai_total_tokens_today),
            "estimated_cost_cny_today": round(ai_cost_today, 6),
        },
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
    provider_cfg = config.get('data_provider_config')
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}
    try:
        minute_limit = int(provider_cfg.get("biying_minute_limit", 3000) or 3000)
    except Exception:
        minute_limit = 3000
    provider_cfg["biying_enabled"] = bool(provider_cfg.get("biying_enabled", False))
    provider_cfg["biying_license_key"] = str(provider_cfg.get("biying_license_key", "") or "")
    provider_cfg["biying_endpoint"] = str(provider_cfg.get("biying_endpoint", "") or "")
    provider_cfg["biying_cert_path"] = str(provider_cfg.get("biying_cert_path", "") or "")
    provider_cfg["biying_minute_limit"] = max(1, min(minute_limit, 100000))
    provider_cfg.pop("biying_daily_limit", None)
    config['data_provider_config'] = provider_cfg
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
    if config.schedule_plan is not None:
        SYSTEM_CONFIG["schedule_plan"] = config.schedule_plan

    if config.email_config is not None:
        SYSTEM_CONFIG["email_config"] = _merge_dict(SYSTEM_CONFIG.get("email_config"), config.email_config)

    if config.api_keys is not None:
        SYSTEM_CONFIG["api_keys"] = _merge_dict(SYSTEM_CONFIG.get("api_keys"), config.api_keys)

    if config.data_provider_config is not None:
        merged_provider_cfg = _merge_dict(SYSTEM_CONFIG.get("data_provider_config"), config.data_provider_config)
        try:
            merged_provider_cfg["biying_minute_limit"] = max(
                1, min(int(merged_provider_cfg.get("biying_minute_limit", 3000) or 3000), 100000)
            )
        except Exception:
            merged_provider_cfg["biying_minute_limit"] = 3000
        merged_provider_cfg.pop("biying_daily_limit", None)
        SYSTEM_CONFIG["data_provider_config"] = merged_provider_cfg

    if config.community_config is not None:
        SYSTEM_CONFIG["community_config"] = _merge_dict(SYSTEM_CONFIG.get("community_config"), config.community_config)

    if config.referral_config is not None:
        SYSTEM_CONFIG["referral_config"] = _merge_dict(SYSTEM_CONFIG.get("referral_config"), config.referral_config)

    if config.pricing_config is not None:
        SYSTEM_CONFIG["pricing_config"] = _merge_dict(SYSTEM_CONFIG.get("pricing_config"), config.pricing_config)
        purchase_manager.update_pricing(SYSTEM_CONFIG["pricing_config"])

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


@router.get("/data/export")
async def export_data_package(authorized: bool = Depends(verify_admin)):
    if not DATA_DIR.exists():
        raise HTTPException(status_code=404, detail="Data directory not found")

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    temp_zip = Path(tempfile.gettempdir()) / f"sniper_data_export_{ts}_{secrets.token_hex(4)}.zip"

    try:
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in DATA_DIR.rglob("*"):
                if not file_path.is_file():
                    continue
                arc = str(file_path.relative_to(DATA_DIR)).replace("\\", "/")
                zf.write(file_path, arcname=arc)
    except Exception as e:
        if temp_zip.exists():
            try:
                temp_zip.unlink()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

    add_runtime_log(f"[后台] 数据导出包已生成: {temp_zip.name}")
    filename = f"sniper_data_export_{ts}.zip"
    return FileResponse(
        path=str(temp_zip),
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: temp_zip.unlink(missing_ok=True)),
    )


def _restore_source_dir(extract_root: Path) -> Path:
    current = extract_root
    entries = [p for p in current.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        current = entries[0]
    candidate_data_dir = current / "data"
    if candidate_data_dir.exists() and candidate_data_dir.is_dir():
        current = candidate_data_dir
    return current


@router.post("/data/restore")
async def restore_data_package(
    backup_file: UploadFile = File(...),
    authorized: bool = Depends(verify_admin),
):
    filename = str(backup_file.filename or "").strip().lower()
    if not filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip backup file is supported")

    max_size_bytes = 1024 * 1024 * 1024  # 1GB
    payload = await backup_file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty backup file")
    if len(payload) > max_size_bytes:
        raise HTTPException(status_code=400, detail="Backup file is too large (max 1GB)")

    with tempfile.TemporaryDirectory(prefix="sniper_restore_") as tmp:
        tmp_dir = Path(tmp)
        zip_path = tmp_dir / "backup.zip"
        extract_dir = tmp_dir / "extract"
        zip_path.write_bytes(payload)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for name in zf.namelist():
                    p = Path(name)
                    if p.is_absolute() or ".." in p.parts:
                        raise HTTPException(status_code=400, detail="Backup package contains unsafe file path")
                zf.extractall(extract_dir)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid backup zip: {e}")

        source_dir = _restore_source_dir(extract_dir)
        if not source_dir.exists() or not source_dir.is_dir():
            raise HTTPException(status_code=400, detail="Backup content is invalid")

        known_files = {
            "commercial.db",
            "config.json",
            "user_accounts.json",
            "referral_records.json",
            "watchlist.json",
            "favorites.json",
            "lhb_history.csv",
        }
        if not any((source_dir / f).exists() for f in known_files):
            raise HTTPException(status_code=400, detail="Backup content does not look like server data directory")

        backup_root = BASE_DIR / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        snapshot_name = f"data_before_restore_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        snapshot_dir = backup_root / snapshot_name

        if DATA_DIR.exists():
            shutil.copytree(DATA_DIR, snapshot_dir)
        else:
            snapshot_dir.mkdir(parents=True, exist_ok=True)

        restored_files = 0
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for src in source_dir.iterdir():
            dst = DATA_DIR / src.name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                restored_files += sum(1 for p in dst.rglob("*") if p.is_file())
            else:
                shutil.copy2(src, dst)
                restored_files += 1

        lhb_manager.load_config()
        lhb_manager.load_hot_money_map()
        lhb_manager.load_vip_seats()
        add_runtime_log(
            f"[ADMIN] Data restore done from {backup_file.filename}, snapshot={snapshot_name}, files={restored_files}"
        )
        log_user_operation(
            "admin_data_restore",
            status="success",
            actor="admin",
            method="POST",
            path="/api/admin/data/restore",
            detail=f"snapshot={snapshot_name}, files={restored_files}",
        )
        return {
            "status": "success",
            "snapshot": snapshot_name,
            "restored_files": restored_files,
        }


def _safe_data_path(rel_path: str) -> Path:
    raw = str(rel_path or "").strip().replace("\\", "/")
    if not raw:
        raise HTTPException(status_code=400, detail="path is required")
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(status_code=400, detail="invalid path")
    full = (DATA_DIR / p).resolve()
    data_root = DATA_DIR.resolve()
    if data_root not in [full, *full.parents]:
        raise HTTPException(status_code=400, detail="path out of data dir")
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return full


@router.get("/data/files")
async def list_data_files(authorized: bool = Depends(verify_admin)):
    files = []
    hidden_cache_dirs = {"kline_cache", "kline_day_cache"}
    folder_stats: Dict[str, Dict[str, Any]] = {}

    if not DATA_DIR.exists():
        return {"files": files, "folders": []}

    for file_path in DATA_DIR.rglob("*"):
        if not file_path.is_file():
            continue

        rel = file_path.relative_to(DATA_DIR)
        rel_str = str(rel).replace("\\", "/")
        top_dir = rel.parts[0] if rel.parts else ""
        stat = file_path.stat()

        if top_dir in hidden_cache_dirs:
            info = folder_stats.setdefault(
                top_dir,
                {"path": top_dir, "file_count": 0, "total_size": 0, "latest_mtime": 0.0},
            )
            info["file_count"] += 1
            info["total_size"] += int(stat.st_size)
            info["latest_mtime"] = max(float(info["latest_mtime"]), float(stat.st_mtime))
            continue

        files.append(
            {
                "path": rel_str,
                "size": int(stat.st_size),
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )

    files.sort(key=lambda x: x["path"])
    folders = []
    for item in folder_stats.values():
        folders.append(
            {
                "path": item["path"],
                "file_count": int(item["file_count"]),
                "total_size": int(item["total_size"]),
                "modified_at": datetime.fromtimestamp(float(item["latest_mtime"] or 0)).isoformat()
                if float(item["latest_mtime"] or 0) > 0
                else "",
            }
        )
    folders.sort(key=lambda x: x["path"])
    return {"files": files, "folders": folders}


@router.get("/data/file")
async def get_data_file_content(
    path: str,
    max_chars: int = 200000,
    authorized: bool = Depends(verify_admin),
):
    file_path = _safe_data_path(path)
    suffix = file_path.suffix.lower()
    max_chars = max(1000, min(int(max_chars or 200000), 1000000))

    if suffix == ".json":
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "type": "json",
                "path": str(file_path.relative_to(DATA_DIR)).replace("\\", "/"),
                "size": int(file_path.stat().st_size),
                "data": data,
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid json file: {e}")

    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {
            "type": "binary",
            "path": str(file_path.relative_to(DATA_DIR)).replace("\\", "/"),
            "size": int(file_path.stat().st_size),
        }

    truncated = len(text) > max_chars
    return {
        "type": "text",
        "path": str(file_path.relative_to(DATA_DIR)).replace("\\", "/"),
        "size": int(file_path.stat().st_size),
        "truncated": truncated,
        "content": text[:max_chars],
    }


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


def _tail_journal_lines(max_lines: int) -> List[str]:
    safe_lines = max(1, min(int(max_lines or 200), 5000))
    service_name = (os.getenv("SYSTEMD_SERVICE_NAME") or "limit-up-sniper").strip()
    if not service_name:
        return []
    cmd = [
        "journalctl",
        "-u",
        service_name,
        "-n",
        str(safe_lines),
        "--no-pager",
        "-o",
        "short-iso",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return []
        lines = [x.rstrip("\n") for x in proc.stdout.splitlines() if x.strip()]
        return lines[-safe_lines:]
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


def _safe_text(value: Any, max_len: int = 300) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


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


def _merge_dict(base: Any, incoming: Any) -> Dict[str, Any]:
    merged = dict(base) if isinstance(base, dict) else {}
    if isinstance(incoming, dict):
        merged.update(incoming)
    return merged


def _device_username_map(accounts: Dict[str, dict]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for username, account in accounts.items():
        if not isinstance(account, dict):
            continue
        did = str(account.get("device_id", "")).strip()
        if did:
            mapping[did] = username
    return mapping


def _iter_user_operation_logs() -> List[Dict[str, Any]]:
    log_file = DATA_DIR / "user_operation_logs.jsonl"
    if not log_file.exists():
        return []

    items: List[Dict[str, Any]] = []
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    items.append(parsed)
    except Exception:
        return []
    items.reverse()
    return items


def _contains_ci(text: str, keyword: str) -> bool:
    if not keyword:
        return True
    return keyword.lower() in str(text or "").lower()


PROVIDER_TEST_LOG_FILE = DATA_DIR / "provider_test_logs.jsonl"


def _append_provider_test_log(entry: Dict[str, Any]):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(PROVIDER_TEST_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_provider_test_logs(limit: int = 50) -> List[Dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 50), 500))
    if not PROVIDER_TEST_LOG_FILE.exists():
        return []
    lines = deque(maxlen=safe_limit)
    try:
        with open(PROVIDER_TEST_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                text = line.strip()
                if text:
                    lines.append(text)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for raw in reversed(lines):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _parse_any_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None

    # Unix timestamp (seconds or milliseconds)
    if text.isdigit():
        try:
            n = int(text)
            if n > 10**12:
                n = n / 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except Exception:
            return None

    norm = text.replace(" ", "T")
    if norm.endswith("Z"):
        norm = norm[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(norm)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _as_shanghai_datetime(value: Any, assume_utc_when_naive: bool = True) -> Optional[datetime]:
    dt = _parse_any_datetime(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        if assume_utc_when_naive:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ)


def _today_range_utc_naive():
    now_sh = datetime.now(SHANGHAI_TZ)
    start_sh = now_sh.replace(hour=0, minute=0, second=0, microsecond=0)
    end_sh = start_sh + timedelta(days=1)
    start_utc = start_sh.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_sh.astimezone(timezone.utc).replace(tzinfo=None)
    return now_sh, start_sh, end_sh, start_utc, end_utc


def _read_proc_stat_cpu_snapshot() -> Optional[Dict[str, int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as f:
            line = f.readline().strip()
        if not line.startswith("cpu "):
            return None
        parts = line.split()
        nums = [int(x) for x in parts[1:]]
        total = sum(nums)
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        return {"total": int(total), "idle": int(idle)}
    except Exception:
        return None


def _read_proc_meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, val = line.split(":", 1)
                first = val.strip().split(" ")[0].strip()
                if first.isdigit():
                    out[key.strip()] = int(first) * 1024  # kB -> bytes
    except Exception:
        return {}
    return out


def _read_proc_net_dev_bytes() -> Dict[str, int]:
    rx_total = 0
    tx_total = 0
    try:
        with open("/proc/net/dev", "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[2:]
        for line in lines:
            if ":" not in line:
                continue
            iface, payload = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo":
                continue
            cols = payload.split()
            if len(cols) < 16:
                continue
            rx_total += int(float(cols[0] or 0))
            tx_total += int(float(cols[8] or 0))
    except Exception:
        return {"rx_bytes": 0, "tx_bytes": 0, "total_bytes": 0}
    return {
        "rx_bytes": int(rx_total),
        "tx_bytes": int(tx_total),
        "total_bytes": int(rx_total + tx_total),
    }


def _safe_percent(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    try:
        return round((float(part) / float(whole)) * 100.0, 2)
    except Exception:
        return 0.0


SYSTEM_METRICS_BASELINE_FILE = DATA_DIR / "system_metrics_baseline.json"


def _load_metrics_baseline() -> Dict[str, Any]:
    data = _load_json(SYSTEM_METRICS_BASELINE_FILE, {})
    if isinstance(data, dict):
        return data
    return {}


def _save_metrics_baseline(data: Dict[str, Any]):
    _save_json(SYSTEM_METRICS_BASELINE_FILE, data if isinstance(data, dict) else {})


def _ensure_today_network_baseline(date_text: str, current_net: Dict[str, int]) -> Dict[str, Any]:
    baseline = _load_metrics_baseline()
    if str(baseline.get("date", "")).strip() != date_text:
        baseline = {
            "date": date_text,
            "rx_bytes": int(current_net.get("rx_bytes", 0) or 0),
            "tx_bytes": int(current_net.get("tx_bytes", 0) or 0),
            "updated_at": datetime.utcnow().replace(microsecond=0).isoformat(),
        }
        _save_metrics_baseline(baseline)
    return baseline


def _collect_system_runtime_metrics(date_text: str) -> Dict[str, Any]:
    # CPU
    cpu_before = _read_proc_stat_cpu_snapshot()
    time.sleep(0.15)
    cpu_after = _read_proc_stat_cpu_snapshot()
    cpu_usage_percent = 0.0
    if cpu_before and cpu_after:
        delta_total = max(1, int(cpu_after["total"] - cpu_before["total"]))
        delta_idle = max(0, int(cpu_after["idle"] - cpu_before["idle"]))
        busy = max(0, delta_total - delta_idle)
        cpu_usage_percent = _safe_percent(busy, delta_total)

    # Memory
    mem = _read_proc_meminfo()
    mem_total = int(mem.get("MemTotal", 0) or 0)
    mem_available = int(mem.get("MemAvailable", 0) or 0)
    mem_used = max(0, mem_total - mem_available) if mem_total > 0 else 0

    # Network
    net = _read_proc_net_dev_bytes()
    baseline = _ensure_today_network_baseline(date_text, net)
    rx_today = max(0, int(net.get("rx_bytes", 0)) - int(baseline.get("rx_bytes", 0) or 0))
    tx_today = max(0, int(net.get("tx_bytes", 0)) - int(baseline.get("tx_bytes", 0) or 0))

    # Load average
    load1 = 0.0
    load5 = 0.0
    load15 = 0.0
    try:
        la = os.getloadavg()
        load1, load5, load15 = float(la[0]), float(la[1]), float(la[2])
    except Exception:
        pass

    return {
        "cpu": {
            "usage_percent": cpu_usage_percent,
            "load_1m": round(load1, 2),
            "load_5m": round(load5, 2),
            "load_15m": round(load15, 2),
        },
        "memory": {
            "total_bytes": mem_total,
            "available_bytes": mem_available,
            "used_bytes": mem_used,
            "usage_percent": _safe_percent(mem_used, mem_total),
        },
        "network": {
            "rx_bytes_total": int(net.get("rx_bytes", 0)),
            "tx_bytes_total": int(net.get("tx_bytes", 0)),
            "total_bytes_total": int(net.get("total_bytes", 0)),
            "rx_bytes_today": int(rx_today),
            "tx_bytes_today": int(tx_today),
            "total_bytes_today": int(rx_today + tx_today),
        },
    }


@router.get("/logs/system")
async def get_system_logs(lines: int = 200, authorized: bool = Depends(verify_admin)):
    safe_lines = max(20, min(int(lines or 200), 2000))
    journal_logs = _tail_journal_lines(safe_lines)
    file_logs = _tail_file_lines(BASE_DIR / "app.log", safe_lines)
    runtime_logs = get_runtime_logs(limit=safe_lines)

    merged = (journal_logs + file_logs + runtime_logs)[-safe_lines:]
    if not merged:
        merged = ["No system logs yet."]
    return {
        "logs": merged,
        "source": "journalctl+app.log+runtime" if journal_logs else "app.log+runtime",
    }


@router.get("/logs/login")
async def get_login_logs(authorized: bool = Depends(verify_admin)):
    logs = get_recent_user_operations(limit=1000)
    return [
        x for x in logs
        if str(x.get("action", "")).endswith("login") or "/login" in str(x.get("path", ""))
    ][:300]


@router.get("/logs/user_ops")
async def get_user_operation_logs(
    page: int = 1,
    page_size: int = 50,
    actor: str = "",
    status: str = "",
    action: str = "",
    path_keyword: str = "",
    user_keyword: str = "",
    authorized: bool = Depends(verify_admin),
):
    safe_page = max(1, int(page or 1))
    safe_size = max(10, min(int(page_size or 50), 200))

    actor_lc = (actor or "").strip().lower()
    status_lc = (status or "").strip().lower()
    action_lc = (action or "").strip().lower()
    path_kw = (path_keyword or "").strip().lower()
    user_kw = (user_keyword or "").strip().lower()

    if actor_lc and actor_lc not in {"admin", "user", "guest", "system", "anonymous"}:
        raise HTTPException(status_code=400, detail="Invalid actor filter")
    if status_lc and status_lc not in {"success", "failed"}:
        raise HTTPException(status_code=400, detail="Invalid status filter")

    all_items = _iter_user_operation_logs()
    accounts = _load_user_accounts()
    device_to_username = _device_username_map(accounts)

    filtered: List[Dict[str, Any]] = []
    for item in all_items:
        row = dict(item)
        if not isinstance(row, dict):
            continue

        did = str(row.get("device_id", "")).strip()
        if not str(row.get("username", "")).strip() and did:
            resolved = device_to_username.get(did, "")
            if resolved:
                row["username"] = resolved

        if actor_lc and str(row.get("actor", "")).strip().lower() != actor_lc:
            continue
        if status_lc and str(row.get("status", "")).strip().lower() != status_lc:
            continue
        if action_lc and action_lc not in str(row.get("action", "")).strip().lower():
            continue
        if path_kw and path_kw not in str(row.get("path", "")).strip().lower():
            continue
        if user_kw:
            candidate = " ".join([
                str(row.get("username", "")).strip(),
                str(row.get("device_id", "")).strip(),
                str(row.get("device_info", "")).strip(),
                str(row.get("ip", "")).strip(),
            ]).lower()
            if user_kw not in candidate:
                continue

        filtered.append(row)

    total = len(filtered)
    start = (safe_page - 1) * safe_size
    logs = filtered[start:start + safe_size]
    return {
        "logs": logs,
        "total": total,
        "page": safe_page,
        "page_size": safe_size,
        "total_pages": max(1, (total + safe_size - 1) // safe_size),
    }


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


@router.post("/monitor/provider_test/run")
async def run_provider_self_test(authorized: bool = Depends(verify_admin)):
    started = time.time()
    now_iso = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    test_id = secrets.token_hex(6)

    results: List[Dict[str, Any]] = []

    def run_step(name: str, fn):
        t0 = time.time()
        ok = False
        detail = ""
        sample: Any = ""
        try:
            data = fn()
            ok = True
            if isinstance(data, (list, tuple)):
                detail = f"成功，返回 {len(data)} 条"
                if len(data) > 0:
                    sample = _preview_data(data[0], 200)
            elif isinstance(data, dict):
                detail = f"成功，返回 {len(data.keys())} 个字段"
                sample = _preview_data(data, 200)
            elif data is None:
                detail = "成功，返回空(None)"
                sample = ""
            else:
                detail = f"成功，返回类型 {type(data).__name__}"
                sample = _preview_data(data, 200)
        except Exception as e:
            ok = False
            detail = f"失败：{e}"
        elapsed_ms = int((time.time() - t0) * 1000)
        results.append(
            {
                "name": name,
                "ok": bool(ok),
                "elapsed_ms": elapsed_ms,
                "detail": _safe_text(detail, 400),
                "sample": _safe_text(sample, 300),
            }
        )

    # 核心外网数据源
    run_step("新浪指数(fetch_indices)", lambda: data_provider.fetch_indices())
    run_step("新浪分时(fetch_intraday_data)", lambda: data_provider.fetch_intraday_data("sh600519"))
    run_step("新浪历史K(fetch_history_data)", lambda: data_provider.fetch_history_data("sh600519", days=30))
    run_step("个股信息(fetch_stock_info)", lambda: data_provider.fetch_stock_info("sh600519"))
    run_step("涨停池(fetch_limit_up_pool)", lambda: data_provider.fetch_limit_up_pool())
    run_step("炸板池(fetch_broken_limit_pool)", lambda: data_provider.fetch_broken_limit_pool())

    # 必盈通道（可选）
    biying_cfg = data_provider._get_biying_config()
    if data_provider._biying_enabled(biying_cfg):
        run_step("必盈股票列表(_fetch_stock_list_biying)", lambda: data_provider._fetch_stock_list_biying())
        run_step("必盈个股概念(_fetch_stock_info_biying)", lambda: data_provider._fetch_stock_info_biying("000001"))
        run_step("必盈多股实时(_fetch_quotes_biying)", lambda: data_provider._fetch_quotes_biying(["000001", "600519"]))
        run_step("必盈最新分时(_fetch_intraday_data_biying)", lambda: data_provider._fetch_intraday_data_biying("000001"))
        run_step("必盈日K(fetch_day_kline_history)", lambda: data_provider.fetch_day_kline_history("000001", days=90))
    else:
        results.append(
            {
                "name": "必盈通道",
                "ok": True,
                "elapsed_ms": 0,
                "detail": "未启用，已跳过",
                "sample": "",
            }
        )

    # 新闻通道
    try:
        from app.core.news_analyzer import get_cls_news, get_eastmoney_news

        run_step("财联社新闻(get_cls_news)", lambda: get_cls_news(hours=1))
        run_step("东方财富新闻(get_eastmoney_news)", lambda: get_eastmoney_news(hours=1))
    except Exception as e:
        results.append(
            {
                "name": "新闻接口导入",
                "ok": False,
                "elapsed_ms": 0,
                "detail": _safe_text(str(e), 300),
                "sample": "",
            }
        )

    total_ms = int((time.time() - started) * 1000)
    success_count = len([x for x in results if x.get("ok")])
    fail_count = len(results) - success_count

    record = {
        "test_id": test_id,
        "time": now_iso,
        "duration_ms": total_ms,
        "success_count": int(success_count),
        "fail_count": int(fail_count),
        "items": results,
    }
    _append_provider_test_log(record)

    add_runtime_log(
        f"[接口体检] 完成 test_id={test_id}, 成功={success_count}, 失败={fail_count}, 耗时={total_ms}ms"
    )
    log_user_operation(
        "provider_self_test_run",
        status="success" if fail_count == 0 else "failed",
        actor="admin",
        method="POST",
        path="/api/admin/monitor/provider_test/run",
        detail=f"test_id={test_id}, success={success_count}, fail={fail_count}, duration_ms={total_ms}",
    )
    return {"status": "success", "record": record}

@router.get("/monitor/provider_test/logs")
async def get_provider_self_test_logs(limit: int = 30, authorized: bool = Depends(verify_admin)):
    logs = _read_provider_test_logs(limit=limit)
    return {"status": "success", "items": logs}


class AdminNewsDeleteRequest(BaseModel):
    ids: List[str]


def _build_news_analysis_index(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for row in records:
        if not isinstance(row, dict):
            continue
        analyzed_at = str(row.get("analyzed_at", "")).strip()
        mode = str(row.get("mode", "")).strip()
        summary = str(row.get("result_summary", "")).strip()
        record_key = str(row.get("record_key", "")).strip()
        news_ids = row.get("news_ids", [])
        if not isinstance(news_ids, list):
            continue
        for nid in news_ids:
            key = str(nid or "").strip()
            if not key:
                continue
            old = index.get(key)
            if old and str(old.get("analyzed_at", "")) >= analyzed_at:
                continue
            index[key] = {
                "record_key": record_key,
                "analyzed_at": analyzed_at,
                "mode": mode,
                "summary": summary,
            }
    return index


@router.get("/news")
async def get_news_admin_list(
    page: int = 1,
    page_size: int = 50,
    keyword: str = "",
    source: str = "",
    ai_status: str = "all",
    authorized: bool = Depends(verify_admin),
):
    safe_page = max(1, int(page or 1))
    safe_size = max(10, min(int(page_size or 50), 200))
    keyword_lc = str(keyword or "").strip().lower()
    source_lc = str(source or "").strip().lower()
    ai_status_lc = str(ai_status or "all").strip().lower()
    if ai_status_lc not in {"all", "analyzed", "unanalyzed"}:
        raise HTTPException(status_code=400, detail="Invalid ai_status filter")

    news_items = load_news_history()
    analysis_index = _build_news_analysis_index(load_news_analysis_records())

    filtered: List[Dict[str, Any]] = []
    for item in news_items:
        nid = build_news_item_id(item)
        ai_info = analysis_index.get(nid)
        analyzed = bool(ai_info)
        if ai_status_lc == "analyzed" and not analyzed:
            continue
        if ai_status_lc == "unanalyzed" and analyzed:
            continue

        source_text = str(item.get("source", "")).strip()
        if source_lc and source_lc not in source_text.lower():
            continue

        row = {
            "id": nid,
            "timestamp": int(item.get("timestamp", 0) or 0),
            "time_str": str(item.get("time_str", "")).strip(),
            "source": source_text,
            "text": str(item.get("text", "")).strip(),
            "ai_analyzed": analyzed,
            "ai_record_key": ai_info.get("record_key", "") if ai_info else "",
            "ai_analyzed_at": ai_info.get("analyzed_at", "") if ai_info else "",
            "ai_mode": ai_info.get("mode", "") if ai_info else "",
            "ai_summary": ai_info.get("summary", "") if ai_info else "",
        }

        if keyword_lc:
            blob = " ".join(
                [
                    row.get("source", ""),
                    row.get("text", ""),
                    row.get("ai_summary", ""),
                    row.get("ai_mode", ""),
                ]
            ).lower()
            if keyword_lc not in blob:
                continue

        filtered.append(row)

    total = len(filtered)
    start = (safe_page - 1) * safe_size
    page_items = filtered[start:start + safe_size]
    source_options = sorted({str(x.get("source", "")).strip() for x in news_items if str(x.get("source", "")).strip()})
    return {
        "items": page_items,
        "total": total,
        "page": safe_page,
        "page_size": safe_size,
        "total_pages": max(1, (total + safe_size - 1) // safe_size),
        "sources": source_options,
    }


@router.post("/news/delete")
async def delete_news_items(
    payload: AdminNewsDeleteRequest,
    authorized: bool = Depends(verify_admin),
):
    ids = [str(x or "").strip() for x in (payload.ids or []) if str(x or "").strip()]
    ids_set = set(ids)
    if not ids_set:
        raise HTTPException(status_code=400, detail="No ids provided")

    news_items = load_news_history()
    remained = []
    removed = 0
    for item in news_items:
        nid = build_news_item_id(item)
        if nid in ids_set:
            removed += 1
            continue
        remained.append(item)
    save_news_history(remained)

    records = load_news_analysis_records()
    kept_records: List[Dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        news_ids = [str(x or "").strip() for x in (row.get("news_ids") or []) if str(x or "").strip()]
        if not news_ids:
            continue
        news_ids = [x for x in news_ids if x not in ids_set]
        if not news_ids:
            continue
        row["news_ids"] = news_ids
        news_items_row = row.get("news_items")
        if isinstance(news_items_row, list):
            row["news_items"] = [
                x for x in news_items_row
                if isinstance(x, dict) and str(x.get("id", "")).strip() not in ids_set
            ]
        kept_records.append(row)
    save_news_analysis_records(kept_records)

    log_user_operation(
        "delete_news_items",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/news/delete",
        detail=f"removed={removed}",
    )
    return {"status": "success", "removed": removed}


@router.post("/news/clear")
async def clear_news_items(authorized: bool = Depends(verify_admin)):
    save_news_history([])
    save_news_analysis_records([])
    log_user_operation(
        "clear_news_items",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/news/clear",
        detail="all_news_cleared",
    )
    return {"status": "success", "removed": "all"}


# --- LHB Admin ---
def _normalize_lhb_seat_mappings(raw: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            seat_name = str(key or "").strip()
            hot_name = str(val or "").strip()
            if seat_name and hot_name:
                out[seat_name] = hot_name
        return dict(sorted(out.items(), key=lambda kv: kv[0]))

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            seat_name = str(
                item.get("seat_name")
                or item.get("seat")
                or item.get("name")
                or item.get("key")
                or ""
            ).strip()
            hot_name = str(
                item.get("hot_money_name")
                or item.get("hot_money")
                or item.get("value")
                or ""
            ).strip()
            if seat_name and hot_name:
                out[seat_name] = hot_name
    return dict(sorted(out.items(), key=lambda kv: kv[0]))


def _normalize_lhb_vip_seats(raw: Any) -> List[str]:
    items: List[str] = []
    if isinstance(raw, list):
        source = raw
    elif isinstance(raw, tuple):
        source = list(raw)
    elif isinstance(raw, str):
        source = [raw]
    else:
        source = []

    for item in source:
        if isinstance(item, dict):
            seat = str(item.get("seat_name") or item.get("name") or item.get("seat") or "").strip()
        else:
            seat = str(item or "").strip()
        if seat:
            items.append(seat)

    deduped = list(dict.fromkeys(items))
    deduped.sort()
    return deduped


def _load_lhb_seat_mappings() -> Dict[str, str]:
    data = _load_json(SEAT_MAPPINGS_FILE, {})
    return _normalize_lhb_seat_mappings(data)


def _load_lhb_vip_seats() -> List[str]:
    data = _load_json(VIP_SEATS_FILE, [])
    return _normalize_lhb_vip_seats(data)


class AdminLHBSeatConfigUpdate(BaseModel):
    seat_mappings: Optional[Any] = None
    vip_seats: Optional[Any] = None


class AdminLHBRangeRequest(BaseModel):
    start_date: str
    end_date: str


class AdminLHBSettingsRequest(BaseModel):
    enabled: bool
    days: int
    min_amount: int


@router.get("/lhb/seat_configs")
async def get_lhb_seat_configs(authorized: bool = Depends(verify_admin)):
    mappings = _load_lhb_seat_mappings()
    vip_seats = _load_lhb_vip_seats()
    lhb_manager.load_hot_money_map()
    lhb_manager.load_vip_seats()
    if not mappings and isinstance(lhb_manager.hot_money_map, dict):
        mappings = _normalize_lhb_seat_mappings(lhb_manager.hot_money_map)
    if not vip_seats and isinstance(lhb_manager.vip_seats, set):
        vip_seats = _normalize_lhb_vip_seats(sorted(list(lhb_manager.vip_seats)))
    return {
        "status": "success",
        "seat_mappings": [{"seat_name": k, "hot_money_name": v} for k, v in mappings.items()],
        "vip_seats": vip_seats,
    }


@router.post("/lhb/seat_configs")
async def update_lhb_seat_configs(
    payload: AdminLHBSeatConfigUpdate,
    authorized: bool = Depends(verify_admin),
):
    if payload.seat_mappings is None and payload.vip_seats is None:
        raise HTTPException(status_code=400, detail="至少提供 seat_mappings 或 vip_seats")

    mappings = _load_lhb_seat_mappings()
    vip_seats = _load_lhb_vip_seats()

    if payload.seat_mappings is not None:
        mappings = _normalize_lhb_seat_mappings(payload.seat_mappings)
        _save_json(SEAT_MAPPINGS_FILE, mappings)

    if payload.vip_seats is not None:
        vip_seats = _normalize_lhb_vip_seats(payload.vip_seats)
        _save_json(VIP_SEATS_FILE, vip_seats)

    lhb_manager.load_hot_money_map()
    lhb_manager.load_vip_seats()
    add_runtime_log(
        f"[龙虎榜] 席位配置已更新: 映射={len(mappings)} 条, VIP席位={len(vip_seats)} 条"
    )
    log_user_operation(
        "update_lhb_seat_configs",
        status="success",
        actor="admin",
        method="POST",
        path="/api/admin/lhb/seat_configs",
        detail=f"seat_mappings={len(mappings)}, vip_seats={len(vip_seats)}",
    )
    return {
        "status": "success",
        "seat_mappings": [{"seat_name": k, "hot_money_name": v} for k, v in mappings.items()],
        "vip_seats": vip_seats,
    }


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
        f"[龙虎榜] 已更新配置: 启用={payload.enabled}, 天数={payload.days}, 最小金额={payload.min_amount}"
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
            "message": "所选区间内无缺失的龙虎榜交易日。",
            "missing_dates": [],
        }

    if lhb_manager.is_syncing:
        return {
            "status": "busy",
            "message": "龙虎榜补数任务正在执行中。",
            "missing_dates": missing_dates,
        }

    add_runtime_log(
        f"[龙虎榜] 启动缺失交易日补数: {start_date} ~ {end_date}, 共{len(missing_dates)}天"
    )
    background_tasks.add_task(
        lhb_manager.fetch_and_update_data,
        add_runtime_log,
        None,
        missing_dates,
    )
    return {
        "status": "started",
        "message": "龙虎榜缺失交易日补数已启动。",
        "missing_dates": missing_dates,
    }

