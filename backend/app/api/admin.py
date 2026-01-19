from fastapi import APIRouter, Depends, HTTPException, Header, Body
from sqlalchemy.orm import Session
from app.db import models, schemas, database
from typing import List, Optional
import secrets
from app.core.config_manager import SYSTEM_CONFIG, save_config
from app.core.lhb_manager import lhb_manager
from pydantic import BaseModel

router = APIRouter()

# 简单的管理员认证
# 在实际生产中，应使用更安全的认证方式
ADMIN_TOKEN = "admin-secret-8888" 

async def verify_admin(x_admin_token: str = Header(..., alias="X-Admin-Token")):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Admin authorization failed")
    return True

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- License Management ---

@router.get("/licenses", response_model=List[schemas.LicenseInfo])
async def list_licenses(skip: int = 0, limit: int = 100, db: Session = Depends(get_db), authorized: bool = Depends(verify_admin)):
    licenses = db.query(models.License).offset(skip).limit(limit).all()
    result = []
    for l in licenses:
        status = "expired" if l.expires_at and l.expires_at < l.activated_at else "active" # Simple check, refine later
        # Logic fix:
        display_status = "active"
        if not l.is_active:
             display_status = "banned"
        elif l.expires_at and l.expires_at < database.datetime.datetime.now():
             display_status = "expired"
        elif l.total_usage != -1 and l.used_usage >= l.total_usage:
             display_status = "exhausted"
        elif not l.activated_at:
             display_status = "unused"

        result.append({
            "key": l.key,
            "type": l.type,
            "remaining_usage": str(l.total_usage - l.used_usage) if l.total_usage != -1 else "Unlimited",
            "expires_at": l.expires_at.strftime("%Y-%m-%d") if l.expires_at else None,
            "status": display_status,
            "device_id": l.device_id,
            "remark": l.remark
        })
    return result # Custom schema might be needed if LicenseInfo doesn't match exactly

@router.post("/licenses/generate")
async def generate_license(
    type: str = "standard", 
    days: int = 30, 
    limit: int = -1, 
    remark: str = None,
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_admin)
):
    key = f"SNIPER-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
    license_obj = models.License(
        key=key,
        type=type,
        total_usage=limit,
        remark=remark
        # expires_at is set on activation
    )
    db.add(license_obj)
    db.commit()
    return {"status": "success", "key": key}

@router.post("/licenses/ban")
async def ban_license(key: str = Body(..., embed=True), db: Session = Depends(get_db), authorized: bool = Depends(verify_admin)):
    l = db.query(models.License).filter(models.License.key == key).first()
    if not l:
        raise HTTPException(status_code=404, detail="Key not found")
    l.is_active = False
    db.commit()
    return {"status": "success"}

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

@router.get("/config")
async def get_admin_config(authorized: bool = Depends(verify_admin)):
    # Combine system config and lhb config
    config = SYSTEM_CONFIG.copy()
    config['lhb_enabled'] = lhb_manager.config['enabled']
    config['lhb_days'] = lhb_manager.config['days']
    config['lhb_min_amount'] = lhb_manager.config['min_amount']
    return config

@router.post("/config")
async def update_admin_config(config: AdminConfigUpdate, authorized: bool = Depends(verify_admin)):
    # System Config
    SYSTEM_CONFIG["auto_analysis_enabled"] = config.auto_analysis_enabled
    SYSTEM_CONFIG["use_smart_schedule"] = config.use_smart_schedule
    SYSTEM_CONFIG["fixed_interval_minutes"] = config.fixed_interval_minutes
    if config.schedule_plan:
        SYSTEM_CONFIG["schedule_plan"] = config.schedule_plan
    
    save_config()
    
    # LHB Config
    if config.lhb_enabled is not None:
         lhb_manager.update_settings(config.lhb_enabled, config.lhb_days, config.lhb_min_amount)
    
    return {"status": "success"}

