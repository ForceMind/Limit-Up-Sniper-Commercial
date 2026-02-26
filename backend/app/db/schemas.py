from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# --- User Schemas ---

class UserBase(BaseModel):
    device_id: str

class UserCreate(UserBase):
    pass

class UserInfo(UserBase):
    id: int
    version: str
    expires_at: Optional[datetime] = None
    created_at: datetime
    
    daily_ai_count: int
    daily_raid_count: int
    daily_review_count: int
    
    # Computed fields for frontend convenience
    remaining_ai: int
    remaining_raid: int
    remaining_review: int
    is_expired: bool
    
    class Config:
        orm_mode = True

# --- Order Schemas ---

class OrderCreate(BaseModel):
    target_version: str
    duration_months: int # 3 days is special case, handled via enum or logic
    # Or just use an enum ID for the pricing plan

class PricingOption(BaseModel):
    id: str # e.g. "basic_1m", "advanced_3m"
    version: str
    label: str
    duration_days: int
    price: float
    original_price: Optional[float] = None

class OrderResponse(BaseModel):
    order_code: str
    amount: float
    status: str
    invite_bonus_token: Optional[str] = None
    invite_bonus_message: Optional[str] = None

class OrderInfo(BaseModel):
    id: int
    order_code: str
    target_version: str
    amount: float
    duration_days: int
    status: str
    created_at: datetime
    
    class Config:
        orm_mode = True

class AdminOrderAction(BaseModel):
    order_code: str
    action: str # approve, reject

class LicenseBase(BaseModel):
    key: str

class LicenseActivate(LicenseBase):
    device_id: str

class LicenseCheck(BaseModel):
    device_id: str

class LicenseInfo(BaseModel):
    key: str
    type: str
    remaining_usage: str # "Unlimited" or number
    expires_at: Optional[str] = None
    status: str # "active", "expired", "exhausted"

class LicenseCreate(BaseModel):
    key: str
    type: str = "standard"
    days: int = 30
    usage_limit: int = -1
    remark: Optional[str] = None

class AdminAddTime(BaseModel):
    user_id: int
    minutes: int

