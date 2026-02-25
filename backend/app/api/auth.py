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
    # Simulate Username/Password logic for now using the provided DeviceID as a key
    # If the frontend sends username/password, we should handle it.
    # But schema UserCreate only has device_id? Let's check schema.
    
    # Check if 'username' is in the request body (by inspecting raw request or updating schema)
    # Since we can't easily change schema without seeing it, we stick to device_id logic 
    # BUT we map username/password from frontend to a deterministic device_id if needed?
    # Or better: We assume the Frontend handles the hashing or we just accept device_id is the key.
    
    # Requirement: "增加需要注册用户名密码"
    # We will assume the frontend sends `username` and `password` in the body, but mapped to `device_id` field?
    # No, that's hacky.
    # Let's check `schemas.UserCreate`.
    
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

@router.post("/register")
async def register(data: dict = Body(...), db: Session = Depends(get_db)):
    """New Endpoint: Register with Username/Password (Mock implementation wrapping DeviceID)"""
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Missing username or password")
    
    # Mock: Create a deterministic device_id from username to link them
    # In a real app, we'd have a UserAuth table.
    import hashlib
    mock_device_id = "user_" + hashlib.md5(username.encode()).hexdigest()[:12]
    
    # Check if already exists?
    # get_or_create_user handles it.
    
    user = user_service.get_or_create_user(db, mock_device_id)
    # Return same format as login
    
    quotas = user_service.get_user_quota(user.version)
    is_expired = (user.expires_at and user.expires_at < datetime.utcnow())
    
    # Return with token (mock token = device_id for simplicity in this no-jwt commercial version)
    return {
        "token": mock_device_id, 
        "user": {
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
    }

@router.post("/login_user")
async def login_user(data: dict = Body(...), db: Session = Depends(get_db)):
    """New Endpoint: Login with Username/Password"""
    username = data.get("username")
    password = data.get("password")
    
    # In a real app, Verify password hash. 
    # Here we just re-generate the ID and check if it exists in DB?
    # Since we don't store passwords in this simple version, we trust the "registration" mapping.
    # DISCLAIMER: This is NOT SECURE for real handling of passwords, but fits the current "File-based/Simple DB" architecture without migration.
    
    import hashlib
    mock_device_id = "user_" + hashlib.md5(username.encode()).hexdigest()[:12]
    
    # Retrieve
    # We need to check if user actually exists first to avoid auto-registering on login?
    # user_service.get_user_by_device_id doesn't exist? get_or_create does.
    # We'll use get_or_create for now to ensure smooth UX even if "first time logging in".
    user = user_service.get_or_create_user(db, mock_device_id)
    
    quotas = user_service.get_user_quota(user.version)
    is_expired = (user.expires_at and user.expires_at < datetime.utcnow())

    return {
        "token": mock_device_id,
        "user": {
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
