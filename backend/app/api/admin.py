from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from app.db import models, schemas, database
from typing import List, Optional, Dict
import secrets
import os
import json
import hashlib
from pathlib import Path
from app.core.config_manager import SYSTEM_CONFIG, save_config
from app.core.lhb_manager import lhb_manager
from app.core import user_service, purchase_manager
from app.core import watchlist_stats
from datetime import datetime, timedelta
from pydantic import BaseModel

router = APIRouter()

# --- Security Configuration ---
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
ADMIN_SECRET_FILE = DATA_DIR / "admin_token.txt"
ADMIN_CREDENTIALS_FILE = DATA_DIR / "admin_credentials.json"
ADMIN_SESSIONS_FILE = DATA_DIR / "admin_sessions.json"
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

    return {
        "status": "success",
        "token": token,
        "username": cred.get("username"),
        "expires_at": expires_at,
    }


@router.post("/logout")
async def admin_logout(x_admin_token: str = Header(..., alias="X-Admin-Token")):
    sessions = _cleanup_sessions(_load_sessions())
    if x_admin_token in sessions:
        sessions.pop(x_admin_token, None)
        _save_sessions(sessions)
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
    return {"status": "success", "message": "Admin password updated successfully"}


# --- User / Order Management ---
@router.get("/users", response_model=List[schemas.UserInfo])
async def list_users(skip: int = 0, limit: int = 100, db: Session = Depends(get_db), authorized: bool = Depends(verify_admin)):
    users = db.query(models.User).offset(skip).limit(limit).all()
    res = []
    for u in users:
        quotas = user_service.get_user_quota(u.version)
        res.append({
            "id": u.id,
            "device_id": u.device_id,
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
    return res


@router.post("/users/add_time")
async def add_time_to_user(
    action: schemas.AdminAddTime,
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin)
):
    user = db.query(models.User).filter(models.User.id == action.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.utcnow()
    if user.expires_at and user.expires_at > now:
        user.expires_at += timedelta(minutes=action.minutes)
    else:
        user.expires_at = now + timedelta(minutes=action.minutes)

    db.commit()
    return {"message": "success", "new_expires_at": user.expires_at}


@router.get("/orders", response_model=List[schemas.OrderInfo])
async def list_orders(status: str = None, skip: int = 0, limit: int = 100, db: Session = Depends(get_db), authorized: bool = Depends(verify_admin)):
    q = db.query(models.PurchaseOrder)
    if status:
        q = q.filter(models.PurchaseOrder.status == status)
    orders = q.order_by(models.PurchaseOrder.created_at.desc()).offset(skip).limit(limit).all()
    return orders


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

    return {
        "status": "success",
        "new_expiry": user.expires_at,
        "bonus_days": bonus_days,
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

    if config.pricing_config is not None:
        SYSTEM_CONFIG["pricing_config"] = config.pricing_config
        purchase_manager.update_pricing(config.pricing_config)

    save_config()

    if config.lhb_enabled is not None:
        lhb_manager.update_settings(config.lhb_enabled, config.lhb_days, config.lhb_min_amount)

    return {"status": "success"}


# --- Logs & Monitor ---
@router.get("/logs/system")
async def get_system_logs(lines: int = 100, authorized: bool = Depends(verify_admin)):
    log_file = BASE_DIR / "app.log"
    if not log_file.exists():
        return {"logs": ["Log file not found."]}

    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
            return {"logs": all_lines[-lines:]}
    except Exception as e:
        return {"logs": [f"Error reading logs: {str(e)}"]}


@router.get("/logs/login")
async def get_login_logs(authorized: bool = Depends(verify_admin)):
    logs = []
    for ip, times in failed_attempts.items():
        for t in times:
            logs.append({
                "ip": ip,
                "time": datetime.fromtimestamp(t),
                "status": "Failed"
            })
    logs.sort(key=lambda x: x["time"], reverse=True)
    return logs


@router.get("/monitor/ai_cache")
async def get_ai_cache_stats(authorized: bool = Depends(verify_admin)):
    from app.core.ai_cache import ai_cache
    return {
        "total_keys": len(ai_cache.cache),
        "keys": list(ai_cache.cache.keys())[-50:]
    }
