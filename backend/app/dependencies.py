from fastapi import Header, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.db import database, models
from datetime import datetime

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def verify_license(x_device_id: str = Header(..., alias="X-Device-ID"), db: Session = Depends(get_db)):
    """
    Dependency to verify license for protected endpoints.
    Requires 'X-Device-ID' header.
    """
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing Device Fingerprint")

    # 查找该设备绑定的有效License
    license_obj = db.query(models.License).filter(
        models.License.device_id == x_device_id,
        models.License.is_active == True
    ).first()

    if not license_obj:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="设备未激活，请购买卡密"
        )

    # 检查过期
    if license_obj.expires_at and license_obj.expires_at < datetime.now():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="订阅已过期，请续费"
        )
    
    # 检查次数限制 (如果有)
    if license_obj.total_usage != -1:
        if license_obj.used_usage >= license_obj.total_usage:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="使用次数已耗尽"
            )

    return license_obj

async def deduct_usage(license_obj: models.License, db: Session):
    """
    Helper to deduct usage after successful operation.
    Only if total_usage != -1
    """
    if license_obj.total_usage != -1:
        license_obj.used_usage += 1
        db.commit()
