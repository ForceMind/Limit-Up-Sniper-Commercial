from pydantic import BaseModel
from typing import Optional
from datetime import datetime

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
