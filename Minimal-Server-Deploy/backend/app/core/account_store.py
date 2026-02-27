import hashlib
import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
USER_ACCOUNTS_FILE = DATA_DIR / "user_accounts.json"
REFERRAL_RECORDS_FILE = DATA_DIR / "referral_records.json"

INVITE_CODE_RE = re.compile(r"^[A-Z0-9]{6,20}$")
INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


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


def load_accounts() -> Dict[str, dict]:
    data = _load_json(USER_ACCOUNTS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_accounts(data: Dict[str, dict]):
    _save_json(USER_ACCOUNTS_FILE, data)


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def make_device_id(username: str) -> str:
    return "user_" + hashlib.md5(username.encode("utf-8")).hexdigest()[:12]


def get_account_by_device_id(
    device_id: str,
    accounts: Optional[Dict[str, dict]] = None,
) -> Tuple[Optional[str], Optional[dict]]:
    data = accounts if isinstance(accounts, dict) else load_accounts()
    did = str(device_id or "").strip()
    if not did:
        return None, None
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
    accounts = load_accounts()
    username, account = get_account_by_device_id(device_id, accounts=accounts)
    if not username or not account:
        return "", ""
    code = ensure_account_invite_code(username, accounts)
    save_accounts(accounts)
    return username, code


def find_account_by_invite_code(
    invite_code: str,
    accounts: Optional[Dict[str, dict]] = None,
) -> Tuple[Optional[str], Optional[dict], str]:
    normalized = normalize_invite_code(invite_code)
    if not normalized:
        return None, None, ""
    data = accounts if isinstance(accounts, dict) else load_accounts()
    for username, account in data.items():
        if not isinstance(account, dict):
            continue
        code = normalize_invite_code(account.get("invite_code", ""))
        if code == normalized:
            return username, account, normalized
    return None, None, normalized


def is_device_banned(device_id: str, accounts: Optional[Dict[str, dict]] = None) -> Tuple[bool, str]:
    _, account = get_account_by_device_id(device_id, accounts=accounts)
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
