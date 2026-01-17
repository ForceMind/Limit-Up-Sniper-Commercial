from fastapi import APIRouter, Depends, HTTPException, Header, Body
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from app.db import models, schemas, database

router = APIRouter()

def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/activate", response_model=schemas.LicenseInfo)
async def activate_license(data: schemas.LicenseActivate, db: Session = Depends(get_db)):
    """激活卡密并绑定设备"""
    db_license = db.query(models.License).filter(models.License.key == data.key).first()
    
    if not db_license:
        raise HTTPException(status_code=404, detail="无效的卡密")
    
    if not db_license.is_active:
         raise HTTPException(status_code=403, detail="卡密已被封禁")

    # 如果已经绑定了其他设备
    if db_license.device_id and db_license.device_id != data.device_id:
        raise HTTPException(status_code=403, detail="该卡密已绑定其他设备")

    # 首次激活逻辑
    if not db_license.activated_at:
        db_license.activated_at = datetime.now()
        db_license.device_id = data.device_id
        
        # 计算过期时间 (如果原来expires_at为空，说明是基于激活时间计算)
        # 这里为了简化，假设 key生成的时候不带expires_at，激活时才算. 
        # 但如果生成的时候就有expires_at (比如固定日期结束)，则不修改
        if not db_license.expires_at:
             # 这里需要读取卡密的有效期配置，为了演示简单，我们假设所有新卡激活给30天
             # 实际商业版应该在生成License时字段里存 duration
             db_license.expires_at = datetime.now() + timedelta(days=30) 
        
        db.commit()
    
    # 如果已经激活，检查是否过期
    if db_license.expires_at and db_license.expires_at < datetime.now():
        raise HTTPException(status_code=403, detail="卡密已过期")
        
    # 确保设备ID一致 (防盗用)
    if db_license.device_id != data.device_id:
        # 重复绑定防护 (Update logic if you want to allow re-binding)
        raise HTTPException(status_code=403, detail="设备指纹不匹配")

    return {
        "key": db_license.key,
        "type": db_license.type,
        "remaining_usage": str(db_license.total_usage - db_license.used_usage) if db_license.total_usage != -1 else "Unlimited",
        "expires_at": db_license.expires_at.strftime("%Y-%m-%d %H:%M:%S") if db_license.expires_at else "Permanent",
        "status": "active"
    }

@router.get("/status")
async def check_status(x_device_id: str = Header(None), db: Session = Depends(get_db)):
    """检查当前设备的权限状态"""
    if not x_device_id:
         raise HTTPException(status_code=400, detail="Missing Device ID")
         
    db_license = db.query(models.License).filter(models.License.device_id == x_device_id).filter(models.License.is_active == True).first()
    
    if not db_license:
        # 没有绑定卡密
        return {"status": "unauthorized", "message": "未激活"}
        
    # 检查过期
    if db_license.expires_at and db_license.expires_at < datetime.now():
        return {"status": "expired", "message": "已过期"}
        
    return {
        "status": "active",
        "type": db_license.type, 
        "expiry": db_license.expires_at.strftime("%Y-%m-%d") if db_license.expires_at else "永久"
    }
