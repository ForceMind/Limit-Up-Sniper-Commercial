from datetime import datetime
from sqlalchemy.orm import Session
from app.db import models

# Quota Configurations
QUOTAS = {
    "trial":     {"ai": 1,    "raid": 0,  "review": 0},
    "basic":     {"ai": 5,    "raid": 0,  "review": 0},
    "advanced":  {"ai": 10,   "raid": 3,  "review": 1},
    "flagship":  {"ai": 1000, "raid": 50, "review": 5},
}

def get_user_quota(version: str):
    return QUOTAS.get(version, QUOTAS["trial"])

def check_and_reset_daily_stats(user: models.User, db: Session):
    now = datetime.utcnow()
    # If last reset was on a different day
    if user.last_reset_date.date() < now.date():
        user.daily_ai_count = 0
        user.daily_raid_count = 0
        user.daily_review_count = 0
        user.last_reset_date = now
        db.commit()
        db.refresh(user)

def get_or_create_user(db: Session, device_id: str) -> models.User:
    user = db.query(models.User).filter(models.User.device_id == device_id).first()
    if not user:
        # Create user with trial locked by default (must apply trial explicitly)
        user = models.User(
            device_id=device_id,
            version="trial",
            expires_at=datetime.utcnow()
        )

        db.add(user)
        db.commit()
        db.refresh(user)
    
    # Check reset
    check_and_reset_daily_stats(user, db)
    return user

def check_quota(user: models.User, limit_type: str) -> bool:
    """
    limit_type: 'ai', 'raid', 'review'
    Returns True if user has quota.
    """
    # 1. Check Expiry
    if user.expires_at and user.expires_at < datetime.utcnow():
        return False
        
    # 2. Check Version Quota
    limits = get_user_quota(user.version)
    max_limit = limits.get(limit_type, 0)
    
    current_usage = 0
    if limit_type == 'ai':
        current_usage = user.daily_ai_count
    elif limit_type == 'raid':
        current_usage = user.daily_raid_count
    elif limit_type == 'review':
        current_usage = user.daily_review_count
        
    return current_usage < max_limit

def consume_quota(db: Session, user: models.User, limit_type: str):
    if limit_type == 'ai':
        user.daily_ai_count += 1
    elif limit_type == 'raid':
        user.daily_raid_count += 1
    elif limit_type == 'review':
        user.daily_review_count += 1
    db.commit()
