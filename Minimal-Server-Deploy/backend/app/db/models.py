from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, Date
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base
import datetime

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, unique=True, index=True, nullable=False)
    version = Column(String, default="trial") # trial, basic, advanced, flagship
    
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    # Daily Quotas
    last_reset_date = Column(DateTime, default=datetime.datetime.utcnow)
    
    daily_ai_count = Column(Integer, default=0)
    daily_raid_count = Column(Integer, default=0)
    daily_review_count = Column(Integer, default=0)

    orders = relationship("PurchaseOrder", back_populates="user")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    
    order_code = Column(String, unique=True, index=True, nullable=False) # 12-char mixed
    amount = Column(Float, nullable=False)
    
    target_version = Column(String, nullable=False) # basic, advanced, flagship
    duration_days = Column(Integer, nullable=False)
    
    status = Column(String, default="pending") # pending (user copied), waiting_verification (user confirmed), completed, cancelled
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, onupdate=datetime.datetime.utcnow)

    user = relationship("User", back_populates="orders")


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
