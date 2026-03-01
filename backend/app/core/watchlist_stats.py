import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
STATS_FILE = DATA_DIR / "watchlist_stats.json"


def _empty_stats() -> Dict[str, Any]:
    return {
        "version": 2,
        "user_codes": {},
        "code_names": {},
    }


def _normalize_code(code: str) -> str:
    return str(code or "").strip().lower()


def _normalize_name(name: str) -> str:
    return str(name or "").strip()


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _ensure_v2(data: Any) -> Dict[str, Any]:
    out = _empty_stats()
    if not isinstance(data, dict):
        return out

    raw_user_codes = data.get("user_codes", {})
    raw_code_names = data.get("code_names", {})

    if isinstance(raw_code_names, dict):
        for code, name in raw_code_names.items():
            code_norm = _normalize_code(code)
            name_norm = _normalize_name(name)
            if code_norm and name_norm:
                out["code_names"][code_norm] = name_norm

    if not isinstance(raw_user_codes, dict):
        return out

    for user_id, entries in raw_user_codes.items():
        uid = str(user_id or "").strip()
        if not uid:
            continue

        normalized_entries: Dict[str, Dict[str, str]] = {}

        # Legacy v1: user_codes[user_id] = ["sh600000", ...]
        if isinstance(entries, list):
            for code in entries:
                c = _normalize_code(code)
                if c:
                    normalized_entries[c] = {
                        "joined_at": "",
                        "stock_name": out["code_names"].get(c, ""),
                    }
        elif isinstance(entries, dict):
            # v2 expected: user_codes[user_id][code] = {joined_at, stock_name}
            # Legacy variant: user_codes[user_id][code] = true / 1 / "x"
            for code, value in entries.items():
                c = _normalize_code(code)
                if not c:
                    continue
                joined_at = ""
                stock_name = out["code_names"].get(c, "")
                if isinstance(value, dict):
                    joined_at = str(value.get("joined_at", "")).strip()
                    stock_name = _normalize_name(value.get("stock_name", stock_name))
                normalized_entries[c] = {
                    "joined_at": joined_at,
                    "stock_name": stock_name,
                }
        else:
            continue

        if normalized_entries:
            out["user_codes"][uid] = normalized_entries
            for c, info in normalized_entries.items():
                if info.get("stock_name"):
                    out["code_names"][c] = info["stock_name"]

    return out


def _materialize_counts(data: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    user_codes = data.get("user_codes", {})
    if not isinstance(user_codes, dict):
        return counts

    for entries in user_codes.values():
        if not isinstance(entries, dict):
            continue
        for code in entries.keys():
            c = _normalize_code(code)
            if c:
                counts[c] = int(counts.get(c, 0)) + 1
    return counts


def _load_stats() -> Dict[str, Any]:
    if not STATS_FILE.exists():
        return _empty_stats()
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _ensure_v2(data)
    except Exception:
        return _empty_stats()


def _save_stats(data: Dict[str, Any]):
    payload = _ensure_v2(data)
    payload["counts"] = _materialize_counts(payload)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def add_favorite_stat(user_id: str, code: str, stock_name: str = ""):
    uid = str(user_id or "").strip()
    c = _normalize_code(code)
    name = _normalize_name(stock_name)
    if not uid or not c:
        return

    data = _load_stats()
    user_codes = data["user_codes"]
    entries = user_codes.get(uid, {})
    if not isinstance(entries, dict):
        entries = {}

    existing = entries.get(c)
    if isinstance(existing, dict):
        # Already tracked: only refresh stock name if newly available.
        if name:
            existing["stock_name"] = name
            entries[c] = existing
            data["code_names"][c] = name
            user_codes[uid] = entries
            _save_stats(data)
        return

    entries[c] = {
        "joined_at": _utc_now_iso(),
        "stock_name": name,
    }
    user_codes[uid] = entries
    if name:
        data["code_names"][c] = name
    _save_stats(data)


def remove_favorite_stat(user_id: str, code: str):
    uid = str(user_id or "").strip()
    c = _normalize_code(code)
    if not uid or not c:
        return

    data = _load_stats()
    user_codes = data["user_codes"]
    entries = user_codes.get(uid, {})
    if not isinstance(entries, dict) or c not in entries:
        return

    entries.pop(c, None)
    if entries:
        user_codes[uid] = entries
    else:
        user_codes.pop(uid, None)

    # If no one keeps this code, remove stale code_name.
    still_used = False
    for item in user_codes.values():
        if isinstance(item, dict) and c in item:
            still_used = True
            break
    if not still_used:
        data.get("code_names", {}).pop(c, None)

    _save_stats(data)


def list_favorite_stats() -> List[Dict[str, Any]]:
    data = _load_stats()
    code_names = data.get("code_names", {})
    counts = _materialize_counts(data)

    result = []
    for code, count in counts.items():
        result.append(
            {
                "code": code,
                "name": _normalize_name(code_names.get(code, "")),
                "count": int(count),
            }
        )
    result.sort(key=lambda item: (-int(item["count"]), item["code"]))
    return result


def list_favorite_users(code: str) -> List[Dict[str, str]]:
    c = _normalize_code(code)
    if not c:
        return []

    data = _load_stats()
    user_codes = data.get("user_codes", {})
    if not isinstance(user_codes, dict):
        return []

    items: List[Dict[str, str]] = []
    for user_id, entries in user_codes.items():
        if not isinstance(entries, dict):
            continue
        info = entries.get(c)
        if not isinstance(info, dict):
            continue
        items.append(
            {
                "user_id": str(user_id),
                "joined_at": str(info.get("joined_at", "")).strip(),
                "stock_name": _normalize_name(info.get("stock_name", "")),
            }
        )

    items.sort(key=lambda x: x.get("joined_at", ""), reverse=True)
    return items
