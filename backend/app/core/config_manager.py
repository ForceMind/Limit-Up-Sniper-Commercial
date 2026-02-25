import json
import time
from pathlib import Path
from typing import List, Optional

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

# Ensure data dir exists
DATA_DIR.mkdir(exist_ok=True)

# Global Configuration
DEFAULT_SCHEDULE = [
    {"start": "09:30", "end": "15:00", "interval": 15, "mode": "intraday", "desc": "盘中交易"},
    {"start": "15:00", "end": "15:15", "interval": 9999, "mode": "none", "desc": "收盘等待"},
    {"start": "15:15", "end": "18:00", "interval": 60, "mode": "after_hours", "desc": "盘后复盘"},
    {"start": "18:00", "end": "23:00", "interval": 180, "mode": "after_hours", "desc": "晚间复盘"},
    {"start": "23:00", "end": "06:00", "interval": 360, "mode": "after_hours", "desc": "夜间休眠"},
    {"start": "06:00", "end": "08:30", "interval": 60, "mode": "after_hours", "desc": "早间复盘"},
    {"start": "08:30", "end": "09:30", "interval": 15, "mode": "after_hours", "desc": "盘前准备"}
]

SYSTEM_CONFIG = {
    "auto_analysis_enabled": True,
    "use_smart_schedule": True,
    "fixed_interval_minutes": 60,
    "last_run_time": 0,
    "next_run_time": 0,
    "current_status": "空闲中",
    "active_rule_index": -1,
    "schedule_plan": DEFAULT_SCHEDULE,
    "news_auto_clean_enabled": True,
    "news_auto_clean_days": 14,
    "email_config": {
        "enabled": False,
        "smtp_server": "",
        "smtp_port": 465,
        "smtp_user": "",
        "smtp_password": "",
        "recipient_email": ""
    },
    "api_keys": {
        "deepseek": "",
        "aliyun": "",
        "other": ""
    },
    "pricing_config": {} # Will be populated by purchase_manager or loaded
}

def load_config():
    """Load configuration from disk"""
    global SYSTEM_CONFIG
    config_path = DATA_DIR / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                saved_config = json.load(f)
                # Update only persistent fields
                for key in ["auto_analysis_enabled", "use_smart_schedule", "fixed_interval_minutes", 
                           "schedule_plan", "news_auto_clean_enabled", "news_auto_clean_days", 
                           "email_config", "api_keys", "pricing_config"]:
                    if key in saved_config:
                        SYSTEM_CONFIG[key] = saved_config[key]
        except Exception as e:
            print(f"Failed to load config: {e}")

def save_config():
    """Save configuration to disk"""
    config_path = DATA_DIR / "config.json"
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            export_data = {
                "auto_analysis_enabled": SYSTEM_CONFIG["auto_analysis_enabled"],
                "use_smart_schedule": SYSTEM_CONFIG["use_smart_schedule"],
                "fixed_interval_minutes": SYSTEM_CONFIG["fixed_interval_minutes"],
                "schedule_plan": SYSTEM_CONFIG.get("schedule_plan", DEFAULT_SCHEDULE),
                "news_auto_clean_enabled": SYSTEM_CONFIG.get("news_auto_clean_enabled", True),
                "news_auto_clean_days": SYSTEM_CONFIG.get("news_auto_clean_days", 14),
                "email_config": SYSTEM_CONFIG.get("email_config", {}),
                "api_keys": SYSTEM_CONFIG.get("api_keys", {}),
                "pricing_config": SYSTEM_CONFIG.get("pricing_config", {})
            }
            json.dump(export_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Failed to save config: {e}")

# Load on import
load_config()
