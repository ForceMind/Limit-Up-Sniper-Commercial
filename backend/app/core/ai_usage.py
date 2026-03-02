import json
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Optional
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
        if model_cfg.get("provider") and str(model_cfg.get("provider")).strip().lower() != provider_norm:
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
        "input_per_million_cny": _safe_float(model_cfg.get("input_per_million_cny", default_cfg["input_per_million_cny"]), default_cfg["input_per_million_cny"]),
        "output_per_million_cny": _safe_float(model_cfg.get("output_per_million_cny", default_cfg["output_per_million_cny"]), default_cfg["output_per_million_cny"]),
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
                if str(row.get("date_shanghai", "")).strip() != date_text:
                    continue
                prompt = max(0, _safe_int(row.get("prompt_tokens", 0)))
                completion = max(0, _safe_int(row.get("completion_tokens", 0)))
                total = max(0, _safe_int(row.get("total_tokens", prompt + completion)))
                cost = max(0.0, _safe_float(row.get("cost_cny", 0.0)))
                summary["count"] += 1
                summary["prompt_tokens"] += prompt
                summary["completion_tokens"] += completion
                summary["total_tokens"] += total
                summary["total_cost_cny"] = round(summary["total_cost_cny"] + cost, 8)
    except Exception:
        return summary
    return summary


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

    subject = f"【AI费用告警】今日累计 ¥{total_cost:.4f}"
    body = "\n".join(
        [
            "检测到 AI 费用出现大金额变化，请关注：",
            f"日期(北京): {date_text}",
            f"今日累计费用: ¥{total_cost:.4f}",
            f"今日请求次数: {int(today_summary.get('count', 0) or 0)}",
            f"今日 Token: {int(today_summary.get('total_tokens', 0) or 0)} (输入 {int(today_summary.get('prompt_tokens', 0) or 0)} / 输出 {int(today_summary.get('completion_tokens', 0) or 0)})",
            "",
            "最近一次请求:",
            f"- 时间(北京): {latest_entry.get('time_shanghai', '-')}",
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
        add_runtime_log(f"[AI费用] 告警邮件已发送: date={date_text}, total={total_cost:.4f}")
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
    total_tokens = max(0, _safe_int(usage.get("total_tokens", prompt_tokens + completion_tokens), prompt_tokens + completion_tokens))
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
