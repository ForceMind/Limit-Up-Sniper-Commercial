import ipaddress
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
IP_BANLIST_FILE = DATA_DIR / "ip_banlist.json"

_ip_ban_lock = threading.Lock()
_ip_ban_cache: Dict[str, Any] = {"loaded": False, "items": {}}


def normalize_ip_text(raw_ip: str) -> str:
    text = str(raw_ip or "").strip()
    if not text:
        return ""
    if "," in text:
        text = text.split(",")[0].strip()
    return text


def _ip_addr_obj(ip_text: str):
    text = normalize_ip_text(ip_text)
    if not text:
        return None
    try:
        return ipaddress.ip_address(text)
    except Exception:
        return None


def is_local_or_private_ip(ip_text: str) -> bool:
    ip_obj = _ip_addr_obj(ip_text)
    if ip_obj is None:
        return True
    return bool(
        ip_obj.is_loopback
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _load_items_locked() -> Dict[str, Dict[str, Any]]:
    if bool(_ip_ban_cache.get("loaded")):
        items = _ip_ban_cache.get("items", {})
        return items if isinstance(items, dict) else {}

    data = {}
    try:
        if IP_BANLIST_FILE.exists():
            data = json.loads(str(IP_BANLIST_FILE.read_text(encoding="utf-8") or "{}"))
    except Exception:
        data = {}

    items: Dict[str, Dict[str, Any]] = {}
    src = data.get("items", {}) if isinstance(data, dict) else {}
    if isinstance(src, dict):
        for raw_ip, meta in src.items():
            ip_text = normalize_ip_text(raw_ip)
            if not ip_text or is_local_or_private_ip(ip_text):
                continue
            if isinstance(meta, dict):
                items[ip_text] = {
                    "reason": str(meta.get("reason", "") or "").strip(),
                    "banned_at": str(meta.get("banned_at", "") or "").strip(),
                    "path": str(meta.get("path", "") or "").strip(),
                    "method": str(meta.get("method", "") or "").strip().upper(),
                }
            else:
                items[ip_text] = {
                    "reason": "",
                    "banned_at": "",
                    "path": "",
                    "method": "",
                }

    _ip_ban_cache["loaded"] = True
    _ip_ban_cache["items"] = items
    return items


def _save_items_locked(items: Dict[str, Dict[str, Any]]) -> None:
    payload = {"items": items if isinstance(items, dict) else {}}
    try:
        IP_BANLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        IP_BANLIST_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def is_ip_banned(ip_text: str) -> bool:
    ip_addr = normalize_ip_text(ip_text)
    if not ip_addr or is_local_or_private_ip(ip_addr):
        return False
    with _ip_ban_lock:
        items = _load_items_locked()
        return ip_addr in items


def ban_ip(ip_text: str, *, path: str = "", method: str = "", reason: str = "") -> bool:
    ip_addr = normalize_ip_text(ip_text)
    if not ip_addr or is_local_or_private_ip(ip_addr):
        return False

    with _ip_ban_lock:
        items = _load_items_locked()
        if ip_addr in items:
            meta = items.get(ip_addr, {})
            if isinstance(meta, dict):
                meta["path"] = str(path or "").strip()
                meta["method"] = str(method or "").strip().upper()
                if reason:
                    meta["reason"] = str(reason or "").strip()
                items[ip_addr] = meta
                _save_items_locked(items)
            return False
        items[ip_addr] = {
            "reason": str(reason or "").strip(),
            "banned_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "path": str(path or "").strip(),
            "method": str(method or "").strip().upper(),
        }
        _save_items_locked(items)
        return True


def list_ip_bans() -> List[Dict[str, Any]]:
    with _ip_ban_lock:
        items = dict(_load_items_locked())

    rows: List[Dict[str, Any]] = []
    for ip_addr, meta in items.items():
        payload = meta if isinstance(meta, dict) else {}
        rows.append(
            {
                "ip": ip_addr,
                "reason": str(payload.get("reason", "") or "").strip(),
                "banned_at": str(payload.get("banned_at", "") or "").strip(),
                "path": str(payload.get("path", "") or "").strip(),
                "method": str(payload.get("method", "") or "").strip().upper(),
            }
        )
    rows.sort(key=lambda x: str(x.get("banned_at", "") or ""), reverse=True)
    return rows


def unban_ip(ip_text: str) -> bool:
    ip_addr = normalize_ip_text(ip_text)
    if not ip_addr:
        return False
    with _ip_ban_lock:
        items = _load_items_locked()
        if ip_addr not in items:
            return False
        items.pop(ip_addr, None)
        _save_items_locked(items)
        return True
