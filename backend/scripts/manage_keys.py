import sys
import os
import secrets
import argparse
from datetime import datetime, timedelta

# Add path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import database, models

def generate_key(type="standard", days=30, limit=-1, remark=None):
    db = database.SessionLocal()
    try:
        key = f"SNIPER-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
        
        # Expiry is set on activation usually, but we can set hard expiry here if needed
        # We'll stick to logic in auth.py: expires_at is set on activation + 30 days
        # OR we can store 'days' in the DB? 
        # Current model has expires_at. If valid_days is a policy, we might need to store it or apply on activation.
        # For this script, let's just make the key.
        
        # Logic in auth.py: if not db_license.expires_at: set to now + 30 days.
        # So creating a key with null expires_at is fine for a "30 day key upon activation".
        
        license_obj = models.License(
            key=key,
            type=type,
            total_usage=limit,
            remark=remark,
            # We don't set expires_at here, it will be set on activation.
            # But wait, auth.py logic hardcoded 30 days if not set.
            # So this script relies on that hardcoded 30 days.
        )
        db.add(license_obj)
        db.commit()
        print(f"Generated Key: {key}")
        print(f"Type: {type}, Limit: {limit}, Remark: {remark}")
        return key
    finally:
        db.close()

def list_keys():
    db = database.SessionLocal()
    try:
        keys = db.query(models.License).all()
        print(f"{'ID':<4} | {'Key':<30} | {'Status':<10} | {'Used/Limit':<15} | {'Device':<20} | {'Expires'}")
        print("-" * 110)
        for k in keys:
            status = "Active" if k.is_active else "Banned"
            usage = f"{k.used_usage}/{k.total_usage if k.total_usage!=-1 else 'Inf'}"
            exp = k.expires_at.strftime('%Y-%m-%d') if k.expires_at else "Pending"
            print(f"{k.id:<4} | {k.key:<30} | {status:<10} | {usage:<15} | {str(k.device_id)[:20]:<20} | {exp}")
    finally:
        db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["gen", "list"], help="Action")
    parser.add_argument("--type", default="standard", help="License Type")
    parser.add_argument("--days", type=int, default=30, help="Valid days (Not fully implemented in auth logic yet)")
    parser.add_argument("--limit", type=int, default=-1, help="Usage limit")
    parser.add_argument("--remark", help="Remark")
    
    args = parser.parse_args()
    
    # Init DB
    database.Base.metadata.create_all(bind=database.engine)
    
    if args.action == "gen":
        generate_key(args.type, args.days, args.limit, args.remark)
    elif args.action == "list":
        list_keys()
