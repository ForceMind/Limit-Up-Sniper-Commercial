from pathlib import Path
import json

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
STATS_FILE = DATA_DIR / "watchlist_stats.json"


def _load_stats():
    if not STATS_FILE.exists():
        return {"user_codes": {}, "counts": {}}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "user_codes": data.get("user_codes", {}),
                "counts": data.get("counts", {})
            }
    except Exception:
        return {"user_codes": {}, "counts": {}}


def _save_stats(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_favorite_stat(user_id: str, code: str):
    if not user_id or not code:
        return
    code = code.lower().strip()

    data = _load_stats()
    user_codes = data["user_codes"]
    counts = data["counts"]

    user_set = set(user_codes.get(user_id, []))
    if code in user_set:
        return

    user_set.add(code)
    user_codes[user_id] = sorted(list(user_set))
    counts[code] = int(counts.get(code, 0)) + 1
    data["counts"] = counts
    _save_stats(data)


def remove_favorite_stat(user_id: str, code: str):
    if not user_id or not code:
        return
    code = code.lower().strip()

    data = _load_stats()
    user_codes = data["user_codes"]
    counts = data["counts"]

    user_set = set(user_codes.get(user_id, []))
    if code not in user_set:
        return

    user_set.remove(code)
    user_codes[user_id] = sorted(list(user_set))

    current = int(counts.get(code, 0))
    if current <= 1:
        counts.pop(code, None)
    else:
        counts[code] = current - 1

    data["counts"] = counts
    _save_stats(data)


def list_favorite_stats():
    data = _load_stats()
    counts = data.get("counts", {})
    result = [{"code": code, "count": int(count)} for code, count in counts.items()]
    result.sort(key=lambda item: item["count"], reverse=True)
    return result
