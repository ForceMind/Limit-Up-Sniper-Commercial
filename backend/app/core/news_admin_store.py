import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
NEWS_HISTORY_FILE = DATA_DIR / "news_history.json"
NEWS_ANALYSIS_FILE = DATA_DIR / "news_analysis_records.json"


def _safe_text(value: Any, max_len: int = 6000) -> str:
    text = str(value or "")
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_ts(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _normalize_news_item(item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict):
        item = {}
    timestamp = _normalize_ts(item.get("timestamp", int(time.time())))
    text = _safe_text(item.get("text", ""), 8000).strip()
    source = _safe_text(item.get("source", ""), 64).strip() or "未知来源"
    time_str = _safe_text(item.get("time_str", ""), 40).strip()
    if not time_str:
        try:
            time_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            time_str = ""
    return {
        "timestamp": timestamp,
        "time_str": time_str,
        "source": source,
        "text": text,
    }


def build_news_item_id(item: Dict[str, Any]) -> str:
    normalized = _normalize_news_item(item)
    raw = f"{normalized.get('timestamp', 0)}|{normalized.get('source', '')}|{normalized.get('text', '')}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_news_history() -> List[Dict[str, Any]]:
    data = _load_json(NEWS_HISTORY_FILE, [])
    if not isinstance(data, list):
        return []
    items = [_normalize_news_item(x) for x in data if isinstance(x, dict)]
    items.sort(key=lambda x: int(x.get("timestamp", 0)), reverse=True)
    return items


def save_news_history(items: List[Dict[str, Any]]):
    normalized = [_normalize_news_item(x) for x in (items or []) if isinstance(x, dict)]
    normalized.sort(key=lambda x: int(x.get("timestamp", 0)), reverse=True)
    _save_json(NEWS_HISTORY_FILE, normalized[:5000])


def load_news_analysis_records() -> List[Dict[str, Any]]:
    data = _load_json(NEWS_ANALYSIS_FILE, [])
    if not isinstance(data, list):
        return []
    items: List[Dict[str, Any]] = []
    for row in data:
        if isinstance(row, dict):
            items.append(row)
    items.sort(key=lambda x: str(x.get("analyzed_at", "")), reverse=True)
    return items


def save_news_analysis_records(records: List[Dict[str, Any]]):
    payload = [x for x in (records or []) if isinstance(x, dict)]
    payload.sort(key=lambda x: str(x.get("analyzed_at", "")), reverse=True)
    _save_json(NEWS_ANALYSIS_FILE, payload[:1000])


def summarize_analysis_result(result: Any) -> str:
    if isinstance(result, dict):
        stocks = result.get("stocks")
        remove_stocks = result.get("remove_stocks")
        if isinstance(stocks, list):
            names = []
            for s in stocks[:3]:
                if not isinstance(s, dict):
                    continue
                name = str(s.get("name") or s.get("code") or "").strip()
                if name:
                    names.append(name)
            focus = "、".join(names) if names else "无"
            removed = len(remove_stocks) if isinstance(remove_stocks, list) else 0
            return f"AI关注{len(stocks)}只，重点：{focus}；移除{removed}只"
        text = _safe_text(json.dumps(result, ensure_ascii=False), 220).strip()
        return text or "已分析"
    if isinstance(result, list):
        preview = []
        for item in result[:3]:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("code") or "").strip()
                if name:
                    preview.append(name)
        if preview:
            return f"AI返回{len(result)}条候选：{'、'.join(preview)}"
        return f"AI返回{len(result)}条结果"
    text = _safe_text(result, 220).strip()
    return text or "已分析"


def append_news_analysis_record(
    mode: str,
    news_batch: List[Dict[str, Any]],
    analysis_result: Any,
    *,
    market_summary: str = "",
    from_cache: bool = False,
) -> Dict[str, Any]:
    now_iso = datetime.utcnow().replace(microsecond=0).isoformat()
    items: List[Dict[str, Any]] = []
    news_ids: List[str] = []
    for raw in news_batch or []:
        normalized = _normalize_news_item(raw)
        news_id = build_news_item_id(normalized)
        news_ids.append(news_id)
        items.append(
            {
                "id": news_id,
                "timestamp": int(normalized.get("timestamp", 0)),
                "time_str": str(normalized.get("time_str", "")).strip(),
                "source": str(normalized.get("source", "")).strip(),
                "text": _safe_text(normalized.get("text", ""), 1200),
            }
        )

    result_hash = hashlib.md5(
        json.dumps(analysis_result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if isinstance(analysis_result, (dict, list))
        else str(analysis_result).encode("utf-8")
    ).hexdigest()
    record_key = hashlib.md5(
        json.dumps(
            {
                "mode": str(mode or "").strip().lower(),
                "news_ids": sorted(news_ids),
                "result_hash": result_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    records = load_news_analysis_records()
    for row in records:
        if str(row.get("record_key", "")).strip() != record_key:
            continue
        row["last_seen_at"] = now_iso
        row["from_cache"] = bool(from_cache)
        row["hit_count"] = int(row.get("hit_count", 1) or 1) + 1
        save_news_analysis_records(records)
        return row

    entry = {
        "record_key": record_key,
        "mode": str(mode or "").strip().lower(),
        "analyzed_at": now_iso,
        "last_seen_at": now_iso,
        "from_cache": bool(from_cache),
        "hit_count": 1,
        "news_ids": news_ids,
        "news_items": items,
        "market_summary": _safe_text(market_summary, 2000),
        "result_summary": summarize_analysis_result(analysis_result),
        "result": analysis_result,
    }
    records.insert(0, entry)
    save_news_analysis_records(records)
    return entry
