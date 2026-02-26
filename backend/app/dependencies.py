from fastapi import Header, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.db import database, models
from app.core import user_service
from datetime import datetime

class QuotaLimitExceeded(HTTPException):
    def __init__(self, detail="Quota exceeded"):
        super().__init__(status_code=403, detail=detail)

class UpgradeRequired(HTTPException):
    def __init__(self, detail="Upgrade required"):
        super().__init__(status_code=403, detail=detail)

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def get_current_user(x_device_id: str = Header(..., alias="X-Device-ID"), db: Session = Depends(get_db)):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing Device ID")
    return user_service.get_or_create_user(db, x_device_id)

async def check_ai_permission(user: models.User = Depends(get_current_user)):
    """Check if user can use AI analysis"""
    # 1. Check if trial expired (10 min hard limit for Trial)
    if user.expires_at and user.expires_at < datetime.utcnow():
        raise UpgradeRequired(detail="License expired")

    # 2. Check Daily Quota
    if not user_service.check_quota(user, 'ai'):
         raise QuotaLimitExceeded(detail=f"Daily AI analysis limit reached for {user.version} version")
    
    return user

async def check_raid_permission(user: models.User = Depends(get_current_user), skip_quota: bool = False):
    """Check if user can use Mid-day Raid"""
    if user.version in ['trial', 'basic']:
        raise UpgradeRequired(detail="Raid feature requires Advanced version or above")
    
    if user.expires_at and user.expires_at < datetime.utcnow():
        raise UpgradeRequired(detail="License expired")
        
    if not skip_quota and not user_service.check_quota(user, 'raid'):
         raise QuotaLimitExceeded(detail="Daily Raid limit reached")
    return user

async def check_review_permission(user: models.User = Depends(get_current_user), skip_quota: bool = False):
    """Check if user can use Post-market Review"""
    if user.version in ['trial', 'basic']:
        raise UpgradeRequired(detail="Review feature requires Advanced version or above")
        
    if user.expires_at and user.expires_at < datetime.utcnow():
        raise UpgradeRequired(detail="License expired")
        
    if not skip_quota and not user_service.check_quota(user, 'review'):
         raise QuotaLimitExceeded(detail="Daily Review limit reached")
    return user


async def check_data_permission(user: models.User = Depends(get_current_user)):
    """
    Generic data-access permission:
    - Guest/trial users are allowed while not expired.
    - Expired users are denied.
    """
    if user.expires_at and user.expires_at < datetime.utcnow():
        raise UpgradeRequired(detail="License expired")
    return user
