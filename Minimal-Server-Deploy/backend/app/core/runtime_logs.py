from collections import deque
from datetime import datetime
from threading import Lock
from typing import List
from zoneinfo import ZoneInfo

_MAX_RUNTIME_LOGS = 5000
_runtime_logs = deque(maxlen=_MAX_RUNTIME_LOGS)
_log_lock = Lock()
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def add_runtime_log(message: str, level: str = "INFO") -> None:
    text = str(message)
    entry = {
        "time": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "message": text,
    }
    with _log_lock:
        _runtime_logs.append(entry)


def get_runtime_logs(limit: int = 200) -> List[str]:
    safe_limit = max(1, int(limit))
    with _log_lock:
        items = list(_runtime_logs)[-safe_limit:]
    return [f"[{x['time']}] [{x['level']}] {x['message']}" for x in items]

