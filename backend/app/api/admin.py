from fastapi import APIRouter, Depends, HTTPException, Header, Body, Request
from sqlalchemy.orm import Session
from app.db import models, schemas, database
from typing import List, Optional, Dict, Any
import secrets
import os
import time
import json
from pathlib import Path
from app.core.config_manager import SYSTEM_CONFIG, save_config
from app.core.lhb_manager import lhb_manager
from app.core import user_service
from app.core import watchlist_stats
from datetime import datetime, timedelta
from pydantic import BaseModel

router = APIRouter()

# --- Security Configuration ---
# Calculate path relative to this file: .../backend/app/api/admin.py -> .../backend/data (or project root/data)
# Based on project structure: backend/app/api/.. -> backend/app -> backend -> data is sibling of app?
# workspace info says: backend/data is where things are.
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data" 
ADMIN_SECRET_FILE = DATA_DIR / "admin_token.txt" 
ADMIN_CREDENTIALS_FILE = DATA_DIR / "admin_credentials.json" # Support custom username/password
RATE_LIMIT_WINDOW = 60 # seconds
RATE_LIMIT_MAX_ATTEMPTS = 50
failed_attempts: Dict[str, List[float]] = {}

def get_admin_token():
    # Priority 1: Environment Variable (For Docker/Cloud)
    env_token = os.getenv("ADMIN_TOKEN")
    if env_token:
        return env_token

    # Priority 2: Credentials File (Username/Password style, but here we simplify to just using a token or checking against it)
    # Actually, if the user wants "Username and Password", we should modify verify_admin to check Basic Auth or Header
    # But to keep compatible with frontend 'X-Admin-Token', let's stick to token for now, 
    # OR allow the token to be "username:password" encoded or just a raw string the user sets.
    
    # If admin requests custom username/pass, they can just set the content of admin_token.txt to "myuser:mypass" 
    # and use that as the token string in the frontend login box.
    
    if ADMIN_SECRET_FILE.exists():
        with open(ADMIN_SECRET_FILE, "r") as f:
            token = f.read().strip()
            if token:
                return token
    
    # Generate new
    new_token = secrets.token_urlsafe(16) 
    # Make sure env exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ADMIN_SECRET_FILE, "w") as f:
        f.write(new_token)
    print(f"[SECURITY] Admin Token Generated: {new_token}")
    print(f"[SECURITY] Saved to: {ADMIN_SECRET_FILE}")
    print(f"[SECURITY] To use custom auth, edit backend/data/admin_token.txt")
    return new_token

ADMIN_TOKEN = get_admin_token()

async def verify_admin(x_admin_token: str = Header(..., alias="X-Admin-Token")):
    global ADMIN_TOKEN
    
    # Reload token from file every time to allow hot-change without restart
    if ADMIN_SECRET_FILE.exists():
         try:
             with open(ADMIN_SECRET_FILE, "r", encoding="utf-8") as f:
                file_token = f.read().strip()
                # Update global token if file changed
                if file_token and file_token != ADMIN_TOKEN:
                    ADMIN_TOKEN = file_token
         except Exception:
             pass

    # Simple token check
    # Debug: verify match
    if x_admin_token != ADMIN_TOKEN:
        print(f"[AUTH ERROR] Token Mismatch. File/Memory: {ADMIN_TOKEN} vs Header: {x_admin_token}")
        raise HTTPException(status_code=403, detail="Admin authorization failed")
    
    return True

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- License Management ---

class UpdatePasswordSchema(BaseModel):
    old_password: str
    new_password: str

@router.post("/update_password")
async def update_admin_password(
    data: UpdatePasswordSchema,
    authorized: bool = Depends(verify_admin)
):
    global ADMIN_TOKEN
    
    # Verify logic is handled by verify_admin, but double check current just in case (though Redundant)
    # Actually, verify_admin checks the header against current token.
    # We update the file and memory.
    
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        
    try:
        # Save to file
        with open(ADMIN_SECRET_FILE, "w") as f:
            f.write(data.new_password)
            
        # Update memory
        ADMIN_TOKEN = data.new_password
        
        return {"status": "success", "message": "Admin password/token updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update password: {str(e)}")

@router.get("/users", response_model=List[schemas.UserInfo])
async def list_users(skip: int = 0, limit: int = 100, db: Session = Depends(get_db), authorized: bool = Depends(verify_admin)):
    users = db.query(models.User).offset(skip).limit(limit).all()
    # Need to map to schema, but schema expects some computed fields
    # We can rely on Pydantic's from_orm but we need the computed properties in the model or helper
    # For simplicity, let's just return what matches or do a manual map if schema validation fails
    # Quick fix: Add ignore_error or just let Pydantic handle it if we modify Schema to be loose
    # Better: Update User Service to help
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
    
    # Logic for upgrade/renewal
    now = datetime.utcnow()
    
    # Price per minute calculation (Very rough approx base on 1 month price)
    # Basic 1m: 58. Advanced 1m: 128. Flagship 1m: 298.
    base_prices = {
        "trial": 0,
        "basic": 58,
        "advanced": 128,
        "flagship": 298
    }
    
    current_value_remaining = 0
    
    # Calculate remaining value if not expired
    if user.expires_at and user.expires_at > now and user.version != "trial":
        remaining_minutes = (user.expires_at - now).total_seconds() / 60
        price_per_month = base_prices.get(user.version, 0)
        price_per_minute = price_per_month / (30 * 24 * 60)
        current_value_remaining = remaining_minutes * price_per_minute
        
    # New Duration
    new_duration_days = order.duration_days
    
    # If upgrading/downgrading, convert remaining value to new time?
    # Requirement: "提示把剩余时长按照分钟转为新的版本时长" (Convert remaining duration to new version duration by minutes - likely by value)
    
    target_price_per_month = base_prices.get(order.target_version, 0)
    target_price_per_minute = target_price_per_month / (30 * 24 * 60)
    
    converted_minutes = 0
    if target_price_per_minute > 0:
        converted_minutes = current_value_remaining / target_price_per_minute
        
    total_new_minutes = (new_duration_days * 24 * 60) + converted_minutes
    
    user.version = order.target_version
    user.expires_at = now + timedelta(minutes=total_new_minutes)
    
    order.status = "completed"
    db.commit()
    
    return {"status": "success", "new_expiry": user.expires_at}

# --- System Configuration ---
# 迁移原 System Config

class AdminConfigUpdate(BaseModel):
    auto_analysis_enabled: bool
    use_smart_schedule: bool
    fixed_interval_minutes: int
    schedule_plan: Optional[List[dict]] = None
    # LHB settings
    lhb_enabled: Optional[bool] = None
    lhb_days: Optional[int] = None
    lhb_min_amount: Optional[int] = None
    # Email settings
    email_config: Optional[dict] = None
    # API Keys
    api_keys: Optional[dict] = None
    # Pricing Config
    pricing_config: Optional[dict] = None


@router.get("/watchlist_stats")
async def get_watchlist_stats(authorized: bool = Depends(verify_admin)):
    return {
        "status": "success",
        "data": watchlist_stats.list_favorite_stats()
    }

@router.get("/config")
async def get_admin_config(authorized: bool = Depends(verify_admin)):
    # Combine system config and lhb config
    config = SYSTEM_CONFIG.copy()
    
    # Fill in environment variables for API keys if configured but empty in config
    if 'api_keys' not in config:
        config['api_keys'] = {}
        
    if not config['api_keys'].get('deepseek'):
        config['api_keys']['deepseek'] = os.getenv('DEEPSEEK_API_KEY', '')

    config['lhb_enabled'] = lhb_manager.config['enabled']
    config['lhb_days'] = lhb_manager.config['days']
    config['lhb_min_amount'] = lhb_manager.config['min_amount']
    # ensure email_config is present locally if loaded from disk but not default
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
    
    # Pricing Config
    from app.core import purchase_manager
    config['pricing_config'] = purchase_manager.PRICING_CONFIG
    
    return config

@router.post("/config")
async def update_admin_config(config: AdminConfigUpdate, authorized: bool = Depends(verify_admin)):
    # System Config
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
        from app.core import purchase_manager
        purchase_manager.update_pricing(config.pricing_config)
    
    save_config()
    
    # LHB Config
    if config.lhb_enabled is not None:
         lhb_manager.update_settings(config.lhb_enabled, config.lhb_days, config.lhb_min_amount)
    
    return {"status": "success"}

# --- Logs & Monitor ---

@router.get("/logs/system")
async def get_system_logs(lines: int = 100, authorized: bool = Depends(verify_admin)):
    # Try to read stdout log if captured, or specific app.log
    # Assuming 'app.log' in root or data
    log_file = BASE_DIR / "app.log"
    if not log_file.exists():
        # Fallback to check if we can return failed_attempts as a log for now or empty
        return {"logs": ["Log file not found."]}
    
    try:
        # Read last N lines
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
            return {"logs": all_lines[-lines:]}
    except Exception as e:
        return {"logs": [f"Error reading logs: {str(e)}"]}

@router.get("/logs/login")
async def get_login_logs(authorized: bool = Depends(verify_admin)):
    # Return failed attempts from memory
    logs = []
    for ip, times in failed_attempts.items():
        for t in times:
            logs.append({
                "ip": ip,
                "time": datetime.fromtimestamp(t),
                "status": "Failed"
            })
    # Sort by time desc
    logs.sort(key=lambda x: x["time"], reverse=True)
    return logs

@router.get("/monitor/ai_cache")
async def get_ai_cache_stats(authorized: bool = Depends(verify_admin)):
    from app.core.ai_cache import ai_cache
    return {
        "total_keys": len(ai_cache.cache),
        "keys": list(ai_cache.cache.keys())[-50:] # Limit to last 50 keys to verify
    }

