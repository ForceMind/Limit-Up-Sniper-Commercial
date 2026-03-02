import json
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.config_manager import SYSTEM_CONFIG
from app.core.runtime_logs import add_runtime_log


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
USAGE_LOG_FILE = DATA_DIR / "ai_usage_logs.jsonl"
ALERT_STATE_FILE = DATA_DIR / "ai_usage_alert_state.json"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return int(default)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, payload: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: Dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _now_shanghai() -> datetime:
    return datetime.now(tz=SHANGHAI_TZ)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace(" ", "T")
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _to_shanghai_dt(value: Any, assume_utc_when_naive: bool = True) -> Optional[datetime]:
    dt = _parse_dt(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if assume_utc_when_naive else SHANGHAI_TZ)
    return dt.astimezone(SHANGHAI_TZ)


def _usage_pricing_config() -> Dict[str, Any]:
    cfg = SYSTEM_CONFIG.get("ai_cost_config", {})
    return cfg if isinstance(cfg, dict) else {}


def get_ai_pricing_snapshot() -> Dict[str, Any]:
    cfg = _usage_pricing_config()
    default_cfg = cfg.get("default", {})
    models_cfg = cfg.get("models", {})
    return {
        "default": {
            "input_per_million_cny": _safe_float(default_cfg.get("input_per_million_cny", 2.0), 2.0),
            "output_per_million_cny": _safe_float(default_cfg.get("output_per_million_cny", 3.0), 3.0),
        },
        "models": models_cfg if isinstance(models_cfg, dict) else {},
    }


def _resolve_model_pricing(provider: str, model: str) -> Dict[str, float]:
    pricing = get_ai_pricing_snapshot()
    model_key = str(model or "").strip()
    default_cfg = pricing["default"]

    models_cfg = pricing.get("models", {})
    model_cfg = models_cfg.get(model_key) if isinstance(models_cfg, dict) else None
    if not isinstance(model_cfg, dict):
        model_cfg = None

    provider_norm = str(provider or "").strip().lower()
    if model_cfg:
        cfg_provider = str(model_cfg.get("provider", "")).strip().lower()
        if cfg_provider and cfg_provider != provider_norm:
            model_cfg = None

    if not model_cfg:
        if provider_norm == "deepseek" and model_key == "deepseek-chat":
            model_cfg = {
                "input_per_million_cny": default_cfg["input_per_million_cny"],
                "output_per_million_cny": default_cfg["output_per_million_cny"],
            }
        else:
            model_cfg = default_cfg

    return {
        "input_per_million_cny": _safe_float(
            model_cfg.get("input_per_million_cny", default_cfg["input_per_million_cny"]),
            default_cfg["input_per_million_cny"],
        ),
        "output_per_million_cny": _safe_float(
            model_cfg.get("output_per_million_cny", default_cfg["output_per_million_cny"]),
            default_cfg["output_per_million_cny"],
        ),
    }


def calculate_ai_cost_cny(
    prompt_tokens: int,
    completion_tokens: int,
    provider: str = "deepseek",
    model: str = "deepseek-chat",
) -> float:
    prompt = max(0, _safe_int(prompt_tokens, 0))
    completion = max(0, _safe_int(completion_tokens, 0))
    p = _resolve_model_pricing(provider, model)
    input_cost = (prompt / 1_000_000.0) * p["input_per_million_cny"]
    output_cost = (completion / 1_000_000.0) * p["output_per_million_cny"]
    return round(input_cost + output_cost, 8)


def _normalize_usage_row(row: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(row.get("provider", "deepseek") or "deepseek").strip().lower()
    model = str(row.get("model", "deepseek-chat") or "deepseek-chat").strip()
    source = str(row.get("source", "-") or "-").strip()

    prompt_tokens = max(0, _safe_int(row.get("prompt_tokens", 0), 0))
    completion_tokens = max(0, _safe_int(row.get("completion_tokens", 0), 0))
    total_tokens = max(
        0,
        _safe_int(row.get("total_tokens", prompt_tokens + completion_tokens), prompt_tokens + completion_tokens),
    )
    cost_cny = max(0.0, _safe_float(row.get("cost_cny", 0.0), 0.0))

    ts = _safe_int(row.get("ts", 0), 0)
    time_shanghai = str(row.get("time_shanghai", "")).strip()
    date_shanghai = str(row.get("date_shanghai", "")).strip()

    dt_sh = None
    if time_shanghai:
        dt_sh = _to_shanghai_dt(time_shanghai, assume_utc_when_naive=False)
    if not dt_sh and ts > 0:
        try:
            dt_sh = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(SHANGHAI_TZ)
        except Exception:
            dt_sh = None

    if dt_sh:
        if not time_shanghai:
            time_shanghai = dt_sh.strftime("%Y-%m-%d %H:%M:%S")
        if not date_shanghai:
            date_shanghai = dt_sh.strftime("%Y-%m-%d")
    elif not date_shanghai:
        date_shanghai = ""

    return {
        "ts": int(ts),
        "time_shanghai": time_shanghai,
        "date_shanghai": date_shanghai,
        "provider": provider,
        "model": model,
        "source": source,
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        "cost_cny": float(round(cost_cny, 8)),
        "extra": row.get("extra"),
    }


def summarize_ai_usage_for_date(date_shanghai: str) -> Dict[str, Any]:
    date_text = str(date_shanghai or "").strip()
    summary = {
        "date": date_text,
        "count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "total_cost_cny": 0.0,
    }
    if not date_text or not USAGE_LOG_FILE.exists():
        return summary

    try:
        with open(USAGE_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                item = _normalize_usage_row(row)
                if item["date_shanghai"] != date_text:
                    continue
                summary["count"] += 1
                summary["prompt_tokens"] += item["prompt_tokens"]
                summary["completion_tokens"] += item["completion_tokens"]
                summary["total_tokens"] += item["total_tokens"]
                summary["total_cost_cny"] = round(summary["total_cost_cny"] + item["cost_cny"], 8)
    except Exception:
        return summary
    return summary


def query_ai_usage_report(
    page: int = 1,
    page_size: int = 50,
    date_from: str = "",
    date_to: str = "",
    provider: str = "",
    model: str = "",
    source: str = "",
) -> Dict[str, Any]:
    safe_page = max(1, int(page or 1))
    safe_size = max(10, min(int(page_size or 50), 200))
    date_from_text = str(date_from or "").strip()
    date_to_text = str(date_to or "").strip()
    provider_text = str(provider or "").strip().lower()
    model_text = str(model or "").strip().lower()
    source_text = str(source or "").strip().lower()

    rows: List[Dict[str, Any]] = []
    if USAGE_LOG_FILE.exists():
        try:
            with open(USAGE_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        raw = json.loads(text)
                    except Exception:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    row = _normalize_usage_row(raw)
                    if date_from_text and (not row["date_shanghai"] or row["date_shanghai"] < date_from_text):
                        continue
                    if date_to_text and (not row["date_shanghai"] or row["date_shanghai"] > date_to_text):
                        continue
                    if provider_text and provider_text not in str(row["provider"]).lower():
                        continue
                    if model_text and model_text not in str(row["model"]).lower():
                        continue
                    if source_text and source_text not in str(row["source"]).lower():
                        continue
                    rows.append(row)
        except Exception:
            rows = []

    rows.sort(
        key=lambda x: (
            _safe_int(x.get("ts", 0), 0),
            str(x.get("time_shanghai", "")),
        ),
        reverse=True,
    )

    totals = {
        "count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "total_cost_cny": 0.0,
    }
    by_provider: Dict[str, Dict[str, Any]] = {}
    by_model: Dict[str, Dict[str, Any]] = {}
    by_source: Dict[str, Dict[str, Any]] = {}
    by_day: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        totals["count"] += 1
        totals["prompt_tokens"] += row["prompt_tokens"]
        totals["completion_tokens"] += row["completion_tokens"]
        totals["total_tokens"] += row["total_tokens"]
        totals["total_cost_cny"] = round(totals["total_cost_cny"] + row["cost_cny"], 8)

        pkey = row["provider"] or "-"
        mkey = f"{row['provider']}/{row['model']}"
        skey = row["source"] or "-"
        dkey = row["date_shanghai"] or "-"

        for target, key in (
            (by_provider, pkey),
            (by_model, mkey),
            (by_source, skey),
            (by_day, dkey),
        ):
            if key not in target:
                target[key] = {
                    "key": key,
                    "count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "total_cost_cny": 0.0,
                }
            agg = target[key]
            agg["count"] += 1
            agg["prompt_tokens"] += row["prompt_tokens"]
            agg["completion_tokens"] += row["completion_tokens"]
            agg["total_tokens"] += row["total_tokens"]
            agg["total_cost_cny"] = round(agg["total_cost_cny"] + row["cost_cny"], 8)

    total = len(rows)
    start = (safe_page - 1) * safe_size
    page_items = rows[start:start + safe_size]

    def _sorted_agg(d: Dict[str, Dict[str, Any]], by_date_desc: bool = False) -> List[Dict[str, Any]]:
        arr = list(d.values())
        if by_date_desc:
            arr.sort(key=lambda x: str(x.get("key", "")), reverse=True)
            return arr
        arr.sort(
            key=lambda x: (
                _safe_float(x.get("total_cost_cny", 0.0), 0.0),
                _safe_int(x.get("count", 0), 0),
            ),
            reverse=True,
        )
        return arr

    return {
        "items": page_items,
        "total": total,
        "page": safe_page,
        "page_size": safe_size,
        "total_pages": max(1, (total + safe_size - 1) // safe_size),
        "totals": {
            **totals,
            "total_cost_cny": round(totals["total_cost_cny"], 8),
        },
        "agg": {
            "by_provider": _sorted_agg(by_provider),
            "by_model": _sorted_agg(by_model),
            "by_source": _sorted_agg(by_source),
            "by_day": _sorted_agg(by_day, by_date_desc=True),
        },
        "filters": {
            "date_from": date_from_text,
            "date_to": date_to_text,
            "provider": provider_text,
            "model": model_text,
            "source": source_text,
        },
    }


def _send_alert_email(subject: str, body: str) -> bool:
    cfg = SYSTEM_CONFIG.get("email_config", {})
    if not isinstance(cfg, dict) or not bool(cfg.get("enabled")):
        return False
    sender_email = str(cfg.get("smtp_user", "") or "").strip()
    sender_password = str(cfg.get("smtp_password", "") or "").strip()
    recipient_email = str(cfg.get("recipient_email", "") or "").strip()
    smtp_server = str(cfg.get("smtp_server", "") or "").strip()
    try:
        smtp_port = int(cfg.get("smtp_port", 465) or 465)
    except Exception:
        smtp_port = 465

    if not (sender_email and sender_password and recipient_email and smtp_server):
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = recipient_email
    try:
        _ = ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, recipient_email, msg.as_string())
        else:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, recipient_email, msg.as_string())
        return True
    except Exception:
        return False


def _maybe_send_cost_alert(today_summary: Dict[str, Any], latest_entry: Dict[str, Any]):
    cfg = _usage_pricing_config()
    alert_cfg = cfg.get("alert", {}) if isinstance(cfg.get("alert", {}), dict) else {}
    enabled = bool(alert_cfg.get("enabled", True))
    if not enabled:
        return

    threshold = max(0.01, _safe_float(alert_cfg.get("daily_threshold_cny", 100.0), 100.0))
    step = max(0.01, _safe_float(alert_cfg.get("step_cny", threshold), threshold))
    cooldown_min = max(1, _safe_int(alert_cfg.get("cooldown_minutes", 60), 60))
    total_cost = _safe_float(today_summary.get("total_cost_cny", 0.0), 0.0)
    if total_cost < threshold:
        return

    now_ts = time.time()
    date_text = str(today_summary.get("date", "")).strip()
    state = _load_json(ALERT_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    last_date = str(state.get("date", "")).strip()
    last_total = _safe_float(state.get("last_total_cost_cny", 0.0), 0.0)
    last_alert_ts = _safe_float(state.get("last_alert_ts", 0.0), 0.0)

    if last_date == date_text:
        if now_ts - last_alert_ts < cooldown_min * 60:
            return
        if total_cost < (last_total + step):
            return

    subject = f"[AI费用告警] 今日累计 ¥{total_cost:.4f}"
    body = "\n".join(
        [
            "检测到 AI 费用出现较大变动，请关注：",
            f"日期(北京时间): {date_text}",
            f"今日累计费用: ¥{total_cost:.4f}",
            f"今日请求次数: {int(today_summary.get('count', 0) or 0)}",
            (
                "今日 Token: "
                f"{int(today_summary.get('total_tokens', 0) or 0)} "
                f"(输入 {int(today_summary.get('prompt_tokens', 0) or 0)} / "
                f"输出 {int(today_summary.get('completion_tokens', 0) or 0)})"
            ),
            "",
            "最近一次请求：",
            f"- 时间(北京时间): {latest_entry.get('time_shanghai', '-')}",
            f"- 来源: {latest_entry.get('source', '-')}",
            f"- 模型: {latest_entry.get('provider', '-')}/{latest_entry.get('model', '-')}",
            f"- 成本: ¥{_safe_float(latest_entry.get('cost_cny', 0.0), 0.0):.6f}",
            f"- Token: {int(latest_entry.get('total_tokens', 0) or 0)}",
        ]
    )
    sent = _send_alert_email(subject, body)
    if sent:
        _save_json(
            ALERT_STATE_FILE,
            {
                "date": date_text,
                "last_total_cost_cny": round(total_cost, 8),
                "last_alert_ts": now_ts,
                "updated_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
            },
        )
        add_runtime_log(f"[AI费用] 告警邮件已发送 date={date_text}, total={total_cost:.4f}")
    else:
        add_runtime_log("[AI费用] 告警触发但邮件发送失败（请检查SMTP配置）")


def record_ai_usage_from_result(result: Dict[str, Any], source: str = "", extra: Optional[Dict[str, Any]] = None):
    if not isinstance(result, dict):
        return
    usage = result.get("usage", {})
    if not isinstance(usage, dict):
        return

    prompt_tokens = max(0, _safe_int(usage.get("prompt_tokens", 0), 0))
    completion_tokens = max(0, _safe_int(usage.get("completion_tokens", 0), 0))
    total_tokens = max(
        0,
        _safe_int(usage.get("total_tokens", prompt_tokens + completion_tokens), prompt_tokens + completion_tokens),
    )
    if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
        return

    provider = "deepseek"
    model = str(result.get("model", "deepseek-chat") or "deepseek-chat").strip()
    cost_cny = calculate_ai_cost_cny(prompt_tokens, completion_tokens, provider=provider, model=model)

    now_sh = _now_shanghai()
    payload: Dict[str, Any] = {
        "ts": int(time.time()),
        "time_shanghai": now_sh.strftime("%Y-%m-%d %H:%M:%S"),
        "date_shanghai": now_sh.strftime("%Y-%m-%d"),
        "provider": provider,
        "model": model,
        "source": str(source or "").strip() or "-",
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        "cost_cny": float(cost_cny),
    }
    if isinstance(extra, dict) and extra:
        payload["extra"] = extra

    try:
        _append_jsonl(USAGE_LOG_FILE, payload)
    except Exception:
        return

    today_summary = summarize_ai_usage_for_date(payload["date_shanghai"])
    _maybe_send_cost_alert(today_summary, payload)
