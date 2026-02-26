import json
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
USER_OP_LOG_FILE = DATA_DIR / "user_operation_logs.jsonl"

_log_lock = threading.Lock()


def _safe_text(value: Any, max_len: int = 300) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def log_user_operation(
    action: str,
    *,
    status: str = "success",
    actor: str = "system",
    method: str = "",
    path: str = "",
    detail: str = "",
    username: str = "",
    device_id: str = "",
    ip: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    entry: Dict[str, Any] = {
        "time": datetime.utcnow().isoformat(),
        "actor": _safe_text(actor, 50),
        "action": _safe_text(action, 80),
        "status": _safe_text(status, 20),
        "method": _safe_text(method, 16),
        "path": _safe_text(path, 180),
        "username": _safe_text(username, 64),
        "device_id": _safe_text(device_id, 80),
        "ip": _safe_text(ip, 64),
        "detail": _safe_text(detail, 300),
    }
    if extra:
        safe_extra = {}
        for k, v in extra.items():
            safe_extra[_safe_text(k, 80)] = _safe_text(v, 300)
        entry["extra"] = safe_extra

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with _log_lock:
            with open(USER_OP_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def get_recent_user_operations(limit: int = 300) -> List[Dict[str, Any]]:
    safe_limit = max(20, min(int(limit or 300), 5000))
    if not USER_OP_LOG_FILE.exists():
        return []

    lines = deque(maxlen=safe_limit)
    try:
        with open(USER_OP_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                text = line.strip()
                if text:
                    lines.append(text)
    except Exception:
        return []

    result: List[Dict[str, Any]] = []
    for text in reversed(lines):
        try:
            item = json.loads(text)
            if isinstance(item, dict):
                result.append(item)
        except Exception:
            continue
    return result
