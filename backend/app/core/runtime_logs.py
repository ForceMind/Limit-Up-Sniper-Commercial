from collections import deque
from datetime import datetime
from threading import Lock
from typing import List
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import os

_MAX_RUNTIME_LOGS = 5000
_runtime_logs = deque(maxlen=_MAX_RUNTIME_LOGS)
_log_lock = Lock()
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
RUNTIME_LOG_FILE = DATA_DIR / "runtime_logs.jsonl"
RUNTIME_LOG_FILE_MAX_BYTES = int(os.getenv("RUNTIME_LOG_FILE_MAX_BYTES", str(8 * 1024 * 1024)) or (8 * 1024 * 1024))
RUNTIME_LOG_FILE_TRIM_LINES = int(os.getenv("RUNTIME_LOG_FILE_TRIM_LINES", str(max(8000, _MAX_RUNTIME_LOGS))) or max(8000, _MAX_RUNTIME_LOGS))
_persist_check_counter = 0


def _normalize_entry(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None
    message = str(entry.get("message", "")).strip()
    if not message:
        return None
    level = str(entry.get("level", "INFO") or "INFO").strip().upper() or "INFO"
    time_text = str(entry.get("time", "")).strip()
    if not time_text:
        time_text = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "time": time_text,
        "level": level,
        "message": message,
    }


def _trim_runtime_log_file_unlocked() -> None:
    if not RUNTIME_LOG_FILE.exists():
        return
    tail = deque(maxlen=max(_MAX_RUNTIME_LOGS, int(RUNTIME_LOG_FILE_TRIM_LINES)))
    try:
        with open(RUNTIME_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                raw = str(line or "").strip()
                if raw:
                    tail.append(raw)
        tmp_file = RUNTIME_LOG_FILE.with_suffix(".jsonl.tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            for raw in tail:
                f.write(raw + "\n")
        os.replace(tmp_file, RUNTIME_LOG_FILE)
    except Exception:
        pass


def _append_runtime_log_file(entry: dict) -> None:
    global _persist_check_counter
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(RUNTIME_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        _persist_check_counter += 1
        if _persist_check_counter >= 200:
            _persist_check_counter = 0
            try:
                if RUNTIME_LOG_FILE.exists() and RUNTIME_LOG_FILE.stat().st_size > int(RUNTIME_LOG_FILE_MAX_BYTES):
                    _trim_runtime_log_file_unlocked()
            except Exception:
                pass
    except Exception:
        pass


def _load_runtime_logs_from_file() -> None:
    if not RUNTIME_LOG_FILE.exists():
        return
    tail_entries = deque(maxlen=_MAX_RUNTIME_LOGS)
    try:
        with open(RUNTIME_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                raw = str(line or "").strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except Exception:
                    continue
                norm = _normalize_entry(parsed)
                if norm is not None:
                    tail_entries.append(norm)
    except Exception:
        return

    if not tail_entries:
        return
    with _log_lock:
        _runtime_logs.clear()
        _runtime_logs.extend(tail_entries)


def add_runtime_log(message: str, level: str = "INFO") -> None:
    entry = _normalize_entry(
        {
            "time": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": str(message),
        }
    )
    if entry is None:
        return
    with _log_lock:
        _runtime_logs.append(entry)
    _append_runtime_log_file(entry)


def get_runtime_logs(limit: int = 200) -> List[str]:
    safe_limit = max(1, int(limit))
    with _log_lock:
        items = list(_runtime_logs)[-safe_limit:]
    return [f"[{x['time']}] [{x['level']}] {x['message']}" for x in items]


_load_runtime_logs_from_file()

