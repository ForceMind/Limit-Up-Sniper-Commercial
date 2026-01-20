from fastapi import APIRouter, Depends, HTTPException, Header, Body
from sqlalchemy.orm import Session
from datetime import datetime
from app.db import models, schemas, database
from app.core import user_service

router = APIRouter()

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/login", response_model=schemas.UserInfo)
async def login(data: schemas.UserCreate, db: Session = Depends(get_db)):
    """Device ID login, creates user if not exists (Trial)"""
    user = user_service.get_or_create_user(db, data.device_id)
    
    # Calculate expiry
    is_expired = False
    if user.expires_at and user.expires_at < datetime.utcnow():
        is_expired = True

    quotas = user_service.get_user_quota(user.version)
    
    return {
        "id": user.id,
        "device_id": user.device_id,
        "version": user.version,
        "expires_at": user.expires_at,
        "created_at": user.created_at,
        "daily_ai_count": user.daily_ai_count,
        "daily_raid_count": user.daily_raid_count,
        "daily_review_count": user.daily_review_count,
        "remaining_ai": quotas['ai'] - user.daily_ai_count,
        "remaining_raid": quotas['raid'] - user.daily_raid_count,
        "remaining_review": quotas['review'] - user.daily_review_count,
        "is_expired": is_expired
    }

@router.get("/status")
async def check_status(x_device_id: str = Header(None), db: Session = Depends(get_db)):
    """检查当前设备的权限状态 - Compatible with legacy calls but redirected to new logic"""
    if not x_device_id:
         raise HTTPException(status_code=400, detail="Missing Device ID")
    
    user = user_service.get_or_create_user(db, x_device_id)
    
    status = "active"
    if user.expires_at and user.expires_at < datetime.utcnow():
        status = "expired"
        
    return {
        "status": status,
        "type": user.version, 
        "expiry": user.expires_at.strftime("%Y-%m-%d %H:%M") if user.expires_at else "永久"
    }
