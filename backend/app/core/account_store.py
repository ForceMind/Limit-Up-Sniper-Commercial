import hashlib
import json
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db import database, models

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
USER_ACCOUNTS_FILE = DATA_DIR / "user_accounts.json"
REFERRAL_RECORDS_FILE = DATA_DIR / "referral_records.json"

INVITE_CODE_RE = re.compile(r"^[A-Z0-9]{6,20}$")
INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_account_table_ready = False
_account_table_lock = threading.Lock()


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data):
    _ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _as_utc_naive(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.replace(tzinfo=None)
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _ensure_account_table():
    global _account_table_ready
    if _account_table_ready:
        return
    with _account_table_lock:
        if _account_table_ready:
            return
        database.Base.metadata.create_all(
            bind=database.engine,
            tables=[models.AccountCredential.__table__],
        )
        _account_table_ready = True


def _row_to_account_dict(row: models.AccountCredential) -> Dict[str, Any]:
    return {
        "username": str(row.username or "").strip(),
        "salt": str(row.salt or "").strip(),
        "password_hash": str(row.password_hash or "").strip(),
        "device_id": str(row.device_id or "").strip(),
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "trial_applied": bool(row.trial_applied),
        "trial_applied_at": row.trial_applied_at.isoformat() if row.trial_applied_at else "",
        "is_banned": bool(row.is_banned),
        "banned_reason": str(row.banned_reason or "").strip(),
        "banned_at": row.banned_at.isoformat() if row.banned_at else "",
        "unbanned_at": row.unbanned_at.isoformat() if row.unbanned_at else "",
        "invite_code": str(row.invite_code or "").strip(),
        "invite_code_updated_at": row.invite_code_updated_at.isoformat() if row.invite_code_updated_at else "",
    }


def _load_accounts_from_db() -> Dict[str, dict]:
    _ensure_account_table()
    db: Session = database.SessionLocal()
    try:
        rows = db.query(models.AccountCredential).all()
        data: Dict[str, dict] = {}
        for row in rows:
            item = _row_to_account_dict(row)
            username = item.get("username", "")
            if username:
                data[username] = item
        return data
    finally:
        db.close()


def _sync_accounts_to_db(data: Dict[str, dict]):
    _ensure_account_table()
    db: Session = database.SessionLocal()
    try:
        existing_rows = db.query(models.AccountCredential).all()
        existing_by_username = {str(row.username or "").strip(): row for row in existing_rows}
        incoming_usernames = set()

        for username, account in data.items():
            uname = str(username or "").strip()
            if not uname or not isinstance(account, dict):
                continue
            incoming_usernames.add(uname)

            row = existing_by_username.get(uname)
            if row is None:
                row = models.AccountCredential(username=uname)
                db.add(row)

            row.salt = str(account.get("salt", "")).strip()
            row.password_hash = str(account.get("password_hash", "")).strip()
            row.device_id = str(account.get("device_id", "")).strip()
            row.invite_code = normalize_invite_code(account.get("invite_code", "")) or None
            row.trial_applied = bool(account.get("trial_applied", False))
            row.trial_applied_at = _as_utc_naive(account.get("trial_applied_at"))
            row.is_banned = bool(account.get("is_banned", False))
            row.banned_reason = str(account.get("banned_reason", "")).strip() or None
            row.banned_at = _as_utc_naive(account.get("banned_at"))
            row.unbanned_at = _as_utc_naive(account.get("unbanned_at"))
            row.created_at = _as_utc_naive(account.get("created_at")) or datetime.utcnow()
            row.invite_code_updated_at = _as_utc_naive(account.get("invite_code_updated_at"))

        for uname, row in existing_by_username.items():
            if uname not in incoming_usernames:
                db.delete(row)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _upsert_account_in_db(username: str, account: Dict[str, Any]):
    _ensure_account_table()
    uname = str(username or "").strip()
    if not uname:
        raise ValueError("username required")
    db: Session = database.SessionLocal()
    try:
        row = db.query(models.AccountCredential).filter(models.AccountCredential.username == uname).first()
        if row is None:
            row = models.AccountCredential(username=uname)
            db.add(row)
        row.salt = str(account.get("salt", "")).strip()
        row.password_hash = str(account.get("password_hash", "")).strip()
        row.device_id = str(account.get("device_id", "")).strip()
        row.invite_code = normalize_invite_code(account.get("invite_code", "")) or None
        row.trial_applied = bool(account.get("trial_applied", False))
        row.trial_applied_at = _as_utc_naive(account.get("trial_applied_at"))
        row.is_banned = bool(account.get("is_banned", False))
        row.banned_reason = str(account.get("banned_reason", "")).strip() or None
        row.banned_at = _as_utc_naive(account.get("banned_at"))
        row.unbanned_at = _as_utc_naive(account.get("unbanned_at"))
        row.created_at = _as_utc_naive(account.get("created_at")) or datetime.utcnow()
        row.invite_code_updated_at = _as_utc_naive(account.get("invite_code_updated_at"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _dump_accounts_snapshot(data: Dict[str, dict]):
    _save_json(USER_ACCOUNTS_FILE, data)


def load_accounts() -> Dict[str, dict]:
    try:
        data = _load_accounts_from_db()
        if data:
            return data
    except Exception:
        pass

    data = _load_json(USER_ACCOUNTS_FILE, {})
    if not isinstance(data, dict):
        return {}

    try:
        _sync_accounts_to_db(data)
    except Exception:
        pass
    return data


def save_accounts(data: Dict[str, dict]):
    safe = data if isinstance(data, dict) else {}
    _sync_accounts_to_db(safe)
    _dump_accounts_snapshot(safe)


def update_account_fields(
    username: str,
    updates: Dict[str, Any],
    *,
    accounts: Optional[Dict[str, dict]] = None,
) -> Dict[str, Any]:
    uname = str(username or "").strip()
    if not uname:
        raise ValueError("Account not found")

    data = accounts if isinstance(accounts, dict) else load_accounts()
    account = data.get(uname)
    if not isinstance(account, dict):
        raise ValueError("Account not found")

    safe_updates = updates if isinstance(updates, dict) else {}
    for key, value in safe_updates.items():
        account[str(key)] = value

    data[uname] = account
    if isinstance(accounts, dict):
        _upsert_account_in_db(uname, account)
        _dump_accounts_snapshot(data)
    else:
        save_accounts(data)
    return account


def get_account_by_username(
    username: str,
    accounts: Optional[Dict[str, dict]] = None,
) -> Optional[dict]:
    uname = str(username or "").strip()
    if not uname:
        return None
    if isinstance(accounts, dict):
        item = accounts.get(uname)
        return item if isinstance(item, dict) else None

    try:
        _ensure_account_table()
        db: Session = database.SessionLocal()
        try:
            row = db.query(models.AccountCredential).filter(models.AccountCredential.username == uname).first()
            if row:
                return _row_to_account_dict(row)
        finally:
            db.close()
    except Exception:
        pass

    data = load_accounts()
    item = data.get(uname)
    return item if isinstance(item, dict) else None


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def make_device_id(username: str) -> str:
    return "user_" + hashlib.md5(username.encode("utf-8")).hexdigest()[:12]


def get_account_by_device_id(
    device_id: str,
    accounts: Optional[Dict[str, dict]] = None,
) -> Tuple[Optional[str], Optional[dict]]:
    did = str(device_id or "").strip()
    if not did:
        return None, None

    if not isinstance(accounts, dict):
        try:
            _ensure_account_table()
            db: Session = database.SessionLocal()
            try:
                row = db.query(models.AccountCredential).filter(models.AccountCredential.device_id == did).first()
                if row:
                    item = _row_to_account_dict(row)
                    return item.get("username"), item
            finally:
                db.close()
        except Exception:
            pass

    data = accounts if isinstance(accounts, dict) else load_accounts()
    for username, account in data.items():
        if not isinstance(account, dict):
            continue
        if str(account.get("device_id", "")).strip() == did:
            return username, account
    return None, None


def get_username_by_device_id(
    device_id: str,
    accounts: Optional[Dict[str, dict]] = None,
) -> str:
    username, _ = get_account_by_device_id(device_id, accounts=accounts)
    return username or ""


def normalize_invite_code(raw_code: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(raw_code or "")).upper().strip()


def _existing_invite_map(accounts: Dict[str, dict]) -> Dict[str, str]:
    existing: Dict[str, str] = {}
    for username, account in accounts.items():
        if not isinstance(account, dict):
            continue
        code = normalize_invite_code(account.get("invite_code", ""))
        if code:
            existing[code] = username
    return existing


def _generate_unique_invite_code(username: str, existing: Dict[str, str]) -> str:
    base = normalize_invite_code(hashlib.sha256(username.encode("utf-8")).hexdigest()[:8])
    preferred = f"INV{base}"
    owner = existing.get(preferred)
    if not owner or owner == username:
        return preferred

    while True:
        suffix = "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(4))
        candidate = f"INV{base[:4]}{suffix}"
        owner = existing.get(candidate)
        if not owner or owner == username:
            return candidate


def ensure_account_invite_code(username: str, accounts: Dict[str, dict]) -> str:
    if username not in accounts or not isinstance(accounts[username], dict):
        raise ValueError("Account not found")

    existing = _existing_invite_map(accounts)
    account = accounts[username]
    current = normalize_invite_code(account.get("invite_code", ""))
    if current and INVITE_CODE_RE.fullmatch(current):
        owner = existing.get(current)
        if not owner or owner == username:
            account["invite_code"] = current
            account.setdefault("invite_code_updated_at", datetime.utcnow().isoformat())
            accounts[username] = account
            return current

    new_code = _generate_unique_invite_code(username, existing)
    account["invite_code"] = new_code
    account["invite_code_updated_at"] = datetime.utcnow().isoformat()
    accounts[username] = account
    return new_code


def ensure_invite_code_for_device(device_id: str) -> Tuple[str, str]:
    username, account = get_account_by_device_id(device_id)
    if not username or not account:
        return "", ""
    accounts = load_accounts()
    if username not in accounts:
        accounts[username] = account
    code = ensure_account_invite_code(username, accounts)
    save_accounts(accounts)
    return username, code


def ensure_invite_code_for_username(username: str) -> str:
    uname = str(username or "").strip()
    if not uname:
        return ""
    account = get_account_by_username(uname)
    if not isinstance(account, dict):
        return ""

    current = normalize_invite_code(account.get("invite_code", ""))
    if current and INVITE_CODE_RE.fullmatch(current):
        return current

    accounts = load_accounts()
    if uname not in accounts:
        accounts[uname] = account
    code = ensure_account_invite_code(uname, accounts)
    save_accounts(accounts)
    return code


def find_account_by_invite_code(
    invite_code: str,
    accounts: Optional[Dict[str, dict]] = None,
) -> Tuple[Optional[str], Optional[dict], str]:
    normalized = normalize_invite_code(invite_code)
    if not normalized:
        return None, None, ""
    if not isinstance(accounts, dict):
        try:
            _ensure_account_table()
            db: Session = database.SessionLocal()
            try:
                row = db.query(models.AccountCredential).filter(models.AccountCredential.invite_code == normalized).first()
                if row:
                    item = _row_to_account_dict(row)
                    return item.get("username"), item, normalized
            finally:
                db.close()
        except Exception:
            pass

    data = accounts if isinstance(accounts, dict) else load_accounts()
    for username, account in data.items():
        if not isinstance(account, dict):
            continue
        code = normalize_invite_code(account.get("invite_code", ""))
        if code == normalized:
            return username, account, normalized
    return None, None, normalized


def is_device_banned(device_id: str, accounts: Optional[Dict[str, dict]] = None) -> Tuple[bool, str]:
    did = str(device_id or "").strip()
    if not did:
        return False, ""

    if not isinstance(accounts, dict):
        try:
            _ensure_account_table()
            db: Session = database.SessionLocal()
            try:
                row = db.query(models.AccountCredential).filter(models.AccountCredential.device_id == did).first()
                if row:
                    if not bool(row.is_banned):
                        return False, ""
                    return True, str(row.banned_reason or "").strip()
            finally:
                db.close()
        except Exception:
            pass

    _, account = get_account_by_device_id(did, accounts=accounts)
    if not account:
        return False, ""
    if not bool(account.get("is_banned")):
        return False, ""
    reason = str(account.get("banned_reason", "")).strip()
    return True, reason


def ensure_device_not_banned(device_id: str, accounts: Optional[Dict[str, dict]] = None):
    banned, reason = is_device_banned(device_id, accounts=accounts)
    if banned:
        detail = "账号已被封禁"
        if reason:
            detail += f"（原因：{reason}）"
        raise HTTPException(status_code=403, detail=detail)


def set_account_ban_status(
    username: str,
    banned: bool,
    reason: str = "",
    accounts: Optional[Dict[str, dict]] = None,
) -> dict:
    data = accounts if isinstance(accounts, dict) else load_accounts()
    account = data.get(username)
    if not isinstance(account, dict):
        raise ValueError("Account not found")

    account["is_banned"] = bool(banned)
    if banned:
        account["banned_reason"] = str(reason or "").strip()
        account["banned_at"] = datetime.utcnow().isoformat()
    else:
        account["banned_reason"] = ""
        account["unbanned_at"] = datetime.utcnow().isoformat()
    data[username] = account
    if isinstance(accounts, dict):
        _upsert_account_in_db(username, account)
        _dump_accounts_snapshot(data)
    else:
        save_accounts(data)
    return account


def _referral_default() -> Dict[str, Any]:
    return {
        "order_invites": {},
        "rewarded_invitees": {},
    }


def load_referral_records() -> Dict[str, Any]:
    data = _load_json(REFERRAL_RECORDS_FILE, _referral_default())
    if not isinstance(data, dict):
        return _referral_default()
    if not isinstance(data.get("order_invites"), dict):
        data["order_invites"] = {}
    if not isinstance(data.get("rewarded_invitees"), dict):
        data["rewarded_invitees"] = {}
    return data


def save_referral_records(data: Dict[str, Any]):
    _save_json(REFERRAL_RECORDS_FILE, data)


def can_apply_invite_for_device(invitee_device_id: str) -> Tuple[bool, str]:
    did = str(invitee_device_id or "").strip()
    if not did:
        return False, "缺少设备标识"

    records = load_referral_records()
    rewarded = records.get("rewarded_invitees", {})
    if isinstance(rewarded, dict) and rewarded.get(did):
        return False, "当前账号已使用过邀请码奖励"

    order_invites = records.get("order_invites", {})
    if isinstance(order_invites, dict):
        for info in order_invites.values():
            if not isinstance(info, dict):
                continue
            if str(info.get("invitee_device_id", "")).strip() != did:
                continue
            if str(info.get("status", "")).strip() in {"pending", "rewarded"}:
                return False, "当前账号已有生效中的邀请码订单"
    return True, ""


def bind_order_invite(
    order_code: str,
    invite_code: str,
    inviter_username: str,
    inviter_device_id: str,
    invitee_device_id: str,
    reward_days: int = 30,
) -> Tuple[bool, str, Optional[dict]]:
    code = str(order_code or "").strip()
    if not code:
        return False, "订单号为空", None

    ok, reason = can_apply_invite_for_device(invitee_device_id)
    if not ok:
        return False, reason, None

    records = load_referral_records()
    order_invites = records.setdefault("order_invites", {})
    bonus_token = "GIFT" + "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(8))
    record = {
        "order_code": code,
        "invite_code": normalize_invite_code(invite_code),
        "inviter_username": str(inviter_username or "").strip(),
        "inviter_device_id": str(inviter_device_id or "").strip(),
        "invitee_device_id": str(invitee_device_id or "").strip(),
        "reward_days": int(reward_days or 30),
        "bonus_token": bonus_token,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    }
    order_invites[code] = record
    save_referral_records(records)
    return True, "", record


def get_order_invite(order_code: str) -> Optional[dict]:
    code = str(order_code or "").strip()
    if not code:
        return None
    records = load_referral_records()
    order_invites = records.get("order_invites", {})
    if not isinstance(order_invites, dict):
        return None
    info = order_invites.get(code)
    return info if isinstance(info, dict) else None


def update_order_invite_status(order_code: str, status: str, reason: str = ""):
    code = str(order_code or "").strip()
    if not code:
        return
    records = load_referral_records()
    order_invites = records.get("order_invites", {})
    if not isinstance(order_invites, dict):
        return
    info = order_invites.get(code)
    if not isinstance(info, dict):
        return
    info["status"] = str(status or "").strip() or info.get("status", "")
    info["updated_at"] = datetime.utcnow().isoformat()
    if reason:
        info["reason"] = reason
    order_invites[code] = info
    records["order_invites"] = order_invites
    save_referral_records(records)


def claim_order_invite_reward(order_code: str) -> Optional[dict]:
    code = str(order_code or "").strip()
    if not code:
        return None
    records = load_referral_records()
    order_invites = records.get("order_invites", {})
    if not isinstance(order_invites, dict):
        return None
    info = order_invites.get(code)
    if not isinstance(info, dict):
        return None
    if str(info.get("status", "")).strip() != "pending":
        return None

    info["status"] = "rewarded"
    info["rewarded_at"] = datetime.utcnow().isoformat()
    order_invites[code] = info
    records["order_invites"] = order_invites

    invitee_device = str(info.get("invitee_device_id", "")).strip()
    if invitee_device:
        rewarded = records.setdefault("rewarded_invitees", {})
        rewarded[invitee_device] = {
            "order_code": code,
            "invite_code": info.get("invite_code", ""),
            "rewarded_at": info["rewarded_at"],
        }
        records["rewarded_invitees"] = rewarded

    save_referral_records(records)
    return info
