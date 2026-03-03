import copy
import random
from datetime import datetime
from typing import Dict, Iterable, Optional, Any

# Pricing Configuration
# Versions: basic, advanced, flagship
# Durations: 3days, 1month, 3months, 6months, 12months

PRICING_CONFIG = {
    "basic": {
        "3d": {"days": 3, "price": 9.9, "label": "3天体验", "bonus_days": 0},
        "1m": {"days": 30, "price": 58, "label": "1个月", "bonus_days": 3},
        "3m": {"days": 90, "price": 158, "label": "3个月", "bonus_days": 10},
        "6m": {"days": 180, "price": 298, "label": "6个月", "bonus_days": 20},
        "12m": {"days": 365, "price": 518, "label": "12个月", "bonus_days": 30},
    },
    "advanced": {
        "3d": {"days": 3, "price": 29.9, "label": "3天体验", "bonus_days": 0},
        "1m": {"days": 30, "price": 128, "label": "1个月", "bonus_days": 3},
        "3m": {"days": 90, "price": 348, "label": "3个月", "bonus_days": 10},
        "6m": {"days": 180, "price": 648, "label": "6个月", "bonus_days": 20},
        "12m": {"days": 365, "price": 1088, "label": "12个月", "bonus_days": 30},
    },
    "flagship": {
        "3d": {"days": 3, "price": 59.9, "label": "3天体验", "bonus_days": 0},
        "1m": {"days": 30, "price": 298, "label": "1个月", "bonus_days": 3},
        "3m": {"days": 90, "price": 798, "label": "3个月", "bonus_days": 10},
        "6m": {"days": 180, "price": 1498, "label": "6个月", "bonus_days": 20},
        "12m": {"days": 365, "price": 2688, "label": "12个月", "bonus_days": 30},
    },
}

RENEWAL_BONUS_BY_DAYS = {
    30: 3,
    90: 10,
    180: 20,
    365: 30,
}

VERSION_ORDER = {
    "trial": 0,
    "basic": 1,
    "advanced": 2,
    "flagship": 3,
}

VERSION_MONTHLY_PRICES = {
    "trial": 0,
    "basic": 58,
    "advanced": 128,
    "flagship": 298,
}


# Update config from SYSTEM_CONFIG on module load if possible or provide update method
def update_pricing(new_config):
    global PRICING_CONFIG
    if new_config:
        PRICING_CONFIG = new_config


# Try to load initial from config manager
try:
    from app.core.config_manager import SYSTEM_CONFIG

    # Only override if SYSTEM_CONFIG has meaningful pricing data
    if "pricing_config" in SYSTEM_CONFIG and SYSTEM_CONFIG["pricing_config"]:
        PRICING_CONFIG = SYSTEM_CONFIG["pricing_config"]
except ImportError:
    pass


HANZI_REPO = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜金生丽水玉出昆冈"
NUMBERS = "0123456789"
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def generate_order_code() -> str:
    """Generate a complex 24-character mixed code to reduce collision probability."""
    hanzi_part = random.choices(HANZI_REPO, k=6)
    num_part = random.choices(NUMBERS, k=6)
    letter_part = random.choices(LETTERS, k=6)
    mixed_part = random.choices(HANZI_REPO + NUMBERS + LETTERS, k=6)

    combined = list(hanzi_part + num_part + letter_part + mixed_part)
    random.shuffle(combined)
    return "".join(combined)


def get_renewal_bonus_days(duration_days: int) -> int:
    return int(RENEWAL_BONUS_BY_DAYS.get(int(duration_days or 0), 0))


def get_upgrade_bonus_days(order_amount: float) -> int:
    amount = float(order_amount or 0.0)
    if amount >= 1200:
        return 20
    if amount >= 600:
        return 12
    if amount >= 300:
        return 7
    if amount >= 150:
        return 3
    if amount >= 60:
        return 1
    return 0


def calculate_membership_extension(
    *,
    current_version: str,
    current_expires_at: Optional[datetime],
    target_version: str,
    purchased_days: int,
    order_amount: float,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    ref_now = now or datetime.utcnow()
    current_ver = str(current_version or "").strip().lower()
    target_ver = str(target_version or "").strip().lower()

    current_rank = int(VERSION_ORDER.get(current_ver, -1))
    target_rank = int(VERSION_ORDER.get(target_ver, -1))
    is_same_version_renewal = current_ver != "trial" and current_ver == target_ver
    is_upgrade = current_rank > 0 and target_rank > current_rank

    remaining_minutes = 0.0
    if current_expires_at and current_expires_at > ref_now and current_ver != "trial":
        remaining_minutes = max(0.0, float((current_expires_at - ref_now).total_seconds()) / 60.0)

    current_price_per_month = float(VERSION_MONTHLY_PRICES.get(current_ver, 0.0) or 0.0)
    target_price_per_month = float(VERSION_MONTHLY_PRICES.get(target_ver, 0.0) or 0.0)
    current_price_per_minute = current_price_per_month / (30 * 24 * 60) if current_price_per_month > 0 else 0.0
    target_price_per_minute = target_price_per_month / (30 * 24 * 60) if target_price_per_month > 0 else 0.0

    converted_minutes = 0.0
    if remaining_minutes > 0 and current_price_per_minute > 0 and target_price_per_minute > 0:
        remaining_value = remaining_minutes * current_price_per_minute
        converted_minutes = max(0.0, remaining_value / target_price_per_minute)

    renewal_bonus_days = get_renewal_bonus_days(purchased_days) if is_same_version_renewal else 0
    upgrade_bonus_days = get_upgrade_bonus_days(order_amount) if is_upgrade else 0
    bonus_days = int(renewal_bonus_days + upgrade_bonus_days)

    purchased_minutes = max(0, int(purchased_days or 0)) * 24 * 60
    total_minutes = purchased_minutes + converted_minutes + (bonus_days * 24 * 60)

    return {
        "is_upgrade": bool(is_upgrade),
        "is_same_version_renewal": bool(is_same_version_renewal),
        "remaining_minutes": float(remaining_minutes),
        "converted_minutes": float(converted_minutes),
        "converted_days": float(converted_minutes / (24 * 60)),
        "renewal_bonus_days": int(renewal_bonus_days),
        "upgrade_bonus_days": int(upgrade_bonus_days),
        "bonus_days": int(bonus_days),
        "purchased_days": int(max(0, int(purchased_days or 0))),
        "total_granted_days": float(total_minutes / (24 * 60)),
        "total_granted_minutes": float(total_minutes),
    }


def is_three_day_trial(duration_days: int) -> bool:
    return int(duration_days or 0) == 3


def get_pricing_options(used_trial_versions: Iterable[str] = None) -> Dict:
    options = copy.deepcopy(PRICING_CONFIG)
    if not used_trial_versions:
        return options

    for version in used_trial_versions:
        if version in options and isinstance(options[version], dict):
            options[version].pop("3d", None)
    return options


def calculate_price(version: str, duration_key: str):
    if version not in PRICING_CONFIG:
        return None
    if duration_key not in PRICING_CONFIG[version]:
        return None
    return PRICING_CONFIG[version][duration_key]
