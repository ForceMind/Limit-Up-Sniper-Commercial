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
    "data_provider_config": {
        "biying_enabled": False,
        "biying_license_key": "",
        "biying_endpoint": "",
        "biying_cert_path": "",
        "biying_daily_limit": 200
    },
    "community_config": {
        "qq_group_number": "",
        "qq_group_link": "",
        "welcome_text": "欢迎加入技术交流群，获取版本更新与使用答疑。"
    },
    "referral_config": {
        "enabled": True,
        "reward_days": 30,
        "share_base_url": "",
        "share_template": "我在用涨停狙击手，注册链接：{invite_link}，邀请码：{invite_code}。注册后在充值页填写邀请码，可获得赠送权益。"
    },
    "pricing_config": {
        "basic": {
            "3d": {"days": 3, "price": 9.9, "label": "3天体验"},
            "1m": {"days": 30, "price": 58, "label": "1个月"},
            "3m": {"days": 90, "price": 158, "label": "3个月"},
            "6m": {"days": 180, "price": 298, "label": "6个月"},
            "12m": {"days": 365, "price": 518, "label": "12个月"}
        },
        "advanced": {
            "3d": {"days": 3, "price": 29.9, "label": "3天体验"},
            "1m": {"days": 30, "price": 128, "label": "1个月"},
            "3m": {"days": 90, "price": 348, "label": "3个月"},
            "6m": {"days": 180, "price": 648, "label": "6个月"},
            "12m": {"days": 365, "price": 1088, "label": "12个月"}
        },
        "flagship": {
            "3d": {"days": 3, "price": 59.9, "label": "3天体验"},
            "1m": {"days": 30, "price": 298, "label": "1个月"},
            "3m": {"days": 90, "price": 798, "label": "3个月"},
            "6m": {"days": 180, "price": 1498, "label": "6个月"},
            "12m": {"days": 365, "price": 2688, "label": "12个月"}
        }
    }
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
                           "last_run_time", "next_run_time",
                           "email_config", "api_keys", "data_provider_config", "community_config", "referral_config", "pricing_config"]:
                    if key in saved_config:
                        SYSTEM_CONFIG[key] = saved_config[key]
        except Exception as e:
            print(f"加载配置失败: {e}")

def save_config():
    """Save configuration to disk"""
    config_path = DATA_DIR / "config.json"
    try:
        existing = {}
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as rf:
                    loaded = json.load(rf)
                    if isinstance(loaded, dict):
                        existing = loaded
            except Exception:
                existing = {}

        with open(config_path, "w", encoding="utf-8") as f:
            export_data = {
                "auto_analysis_enabled": SYSTEM_CONFIG["auto_analysis_enabled"],
                "use_smart_schedule": SYSTEM_CONFIG["use_smart_schedule"],
                "fixed_interval_minutes": SYSTEM_CONFIG["fixed_interval_minutes"],
                "last_run_time": SYSTEM_CONFIG.get("last_run_time", 0),
                "next_run_time": SYSTEM_CONFIG.get("next_run_time", 0),
                "schedule_plan": SYSTEM_CONFIG.get("schedule_plan", DEFAULT_SCHEDULE),
                "news_auto_clean_enabled": SYSTEM_CONFIG.get("news_auto_clean_enabled", True),
                "news_auto_clean_days": SYSTEM_CONFIG.get("news_auto_clean_days", 14),
                "email_config": SYSTEM_CONFIG.get("email_config", {}),
                "api_keys": SYSTEM_CONFIG.get("api_keys", {}),
                "data_provider_config": SYSTEM_CONFIG.get("data_provider_config", {}),
                "community_config": SYSTEM_CONFIG.get("community_config", {}),
                "referral_config": SYSTEM_CONFIG.get("referral_config", {}),
                "pricing_config": SYSTEM_CONFIG.get("pricing_config", {})
            }
            existing.update(export_data)
            json.dump(existing, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")

# Load on import
load_config()
