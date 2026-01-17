from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float
from sqlalchemy.sql import func
from .database import Base
import datetime

class License(Base):
    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True, nullable=False) # 卡密字符串
    type = Column(String, default="standard") # standard, vip, trial
    
    # 权限限制
    total_usage = Column(Integer, default=-1) # -1 表示无限
    used_usage = Column(Integer, default=0)
    
    # 时间限制
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    activated_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True) # 过期时间
    
    # 绑定信息
    device_id = Column(String, index=True, nullable=True) # 绑定的浏览器指纹
    is_active = Column(Boolean, default=True) # 管理员强制封禁开关
    
    remark = Column(String, nullable=True)

class AccessLog(Base):
    __tablename__ = "access_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True)
    endpoint = Column(String)
    limit_type = Column(String) # ai_analysis, intraday, etc.
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    status = Column(String) # allow, deny
