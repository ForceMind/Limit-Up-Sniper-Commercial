import random
from typing import List, Dict
from app.db import schemas

# Pricing Configuration
# Versions: basic, advanced, flagship
# Durations: 3days, 1month, 3months, 6months, 12months

PRICING_CONFIG = {
    "basic": {
        "3d": {"days": 3, "price": 9.9, "label": "3天体验"},
        "1m": {"days": 30, "price": 58, "label": "1个月"},
        "3m": {"days": 90, "price": 158, "label": "3个月"},
        "6m": {"days": 180, "price": 298, "label": "6个月"},
        "12m": {"days": 365, "price": 518, "label": "12个月"},
    },
    "advanced": {
        "3d": {"days": 3, "price": 29.9, "label": "3天体验"},
        "1m": {"days": 30, "price": 128, "label": "1个月"},
        "3m": {"days": 90, "price": 348, "label": "3个月"},
        "6m": {"days": 180, "price": 648, "label": "6个月"},
        "12m": {"days": 365, "price": 1088, "label": "12个月"},
    },
    "flagship": {
        "3d": {"days": 3, "price": 59.9, "label": "3天体验"},
        "1m": {"days": 30, "price": 298, "label": "1个月"},
        "3m": {"days": 90, "price": 798, "label": "3个月"},
        "6m": {"days": 180, "price": 1498, "label": "6个月"},
        "12m": {"days": 365, "price": 2688, "label": "12个月"},
    }
}

# Update config from SYSTEM_CONFIG on module load if possible or provide update method
def update_pricing(new_config):
    global PRICING_CONFIG
    if new_config:
        PRICING_CONFIG = new_config

# Try to load initial from config manager
try:
    from app.core.config_manager import SYSTEM_CONFIG
    if "pricing_config" in SYSTEM_CONFIG:
        PRICING_CONFIG = SYSTEM_CONFIG["pricing_config"]
except ImportError:
    pass

HANZI_Repo = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜金生丽水玉出昆冈"  
NUMBERS = "0123456789"
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"

def generate_order_code() -> str:
    """Generates a complex 24-character mixed code to prevent collision"""
    # Increased complexity: 6 Hanzi + 6 Numbers + 6 Letters + 6 Mixed
    hanzi_part = random.choices(HANZI_Repo, k=6)
    num_part = random.choices(NUMBERS, k=6)
    letter_part = random.choices(LETTERS, k=6)
    mixed_part = random.choices(HANZI_Repo + NUMBERS + LETTERS, k=6)

    # Mix them
    combined = list(hanzi_part + num_part + letter_part + mixed_part)
    random.shuffle(combined)
    return "".join(combined)

def get_pricing_options() -> Dict:
    return PRICING_CONFIG

def calculate_price(version: str, duration_key: str):
    if version not in PRICING_CONFIG:
        return None
    if duration_key not in PRICING_CONFIG[version]:
        return None
    return PRICING_CONFIG[version][duration_key]
