import json
import threading
import atexit
import queue
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
USER_OP_LOG_FILE = DATA_DIR / "user_operation_logs.jsonl"

_log_lock = threading.Lock()
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_log_queue: "queue.Queue[str]" = queue.Queue(maxsize=10000)
_writer_thread: Optional[threading.Thread] = None
_writer_stop = threading.Event()


def _writer_loop():
    pending: List[str] = []
    while not _writer_stop.is_set() or not _log_queue.empty() or pending:
        try:
            line = _log_queue.get(timeout=0.5)
            pending.append(line)
            while len(pending) < 200:
                pending.append(_log_queue.get_nowait())
        except queue.Empty:
            pass

        if not pending:
            continue

        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with _log_lock:
                with open(USER_OP_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write("\n".join(pending) + "\n")
            pending.clear()
        except Exception:
            pending.clear()


def _ensure_writer_started():
    global _writer_thread
    if _writer_thread and _writer_thread.is_alive():
        return
    with _log_lock:
        if _writer_thread and _writer_thread.is_alive():
            return
        _writer_stop.clear()
        _writer_thread = threading.Thread(target=_writer_loop, name="user-op-log-writer", daemon=True)
        _writer_thread.start()


def _shutdown_writer():
    _writer_stop.set()
    thread = _writer_thread
    if thread and thread.is_alive():
        thread.join(timeout=2.0)


atexit.register(_shutdown_writer)


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
    device_info: str = "",
    ip: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    entry: Dict[str, Any] = {
        "time": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "actor": _safe_text(actor, 50),
        "action": _safe_text(action, 80),
        "status": _safe_text(status, 20),
        "method": _safe_text(method, 16),
        "path": _safe_text(path, 180),
        "username": _safe_text(username, 64),
        "device_id": _safe_text(device_id, 80),
        "device_info": _safe_text(device_info, 240),
        "ip": _safe_text(ip, 64),
        "detail": _safe_text(detail, 300),
    }
    if extra:
        safe_extra = {}
        for k, v in extra.items():
            safe_extra[_safe_text(k, 80)] = _safe_text(v, 300)
        entry["extra"] = safe_extra

    try:
        _ensure_writer_started()
        line = json.dumps(entry, ensure_ascii=False)
        _log_queue.put_nowait(line)
    except queue.Full:
        pass
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
