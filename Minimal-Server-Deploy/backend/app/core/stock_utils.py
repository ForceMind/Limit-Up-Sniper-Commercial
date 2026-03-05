from datetime import datetime, time
import threading
import time as time_module
from app.core.data_provider import data_provider


_METRICS_CACHE_LOCK = threading.Lock()
_METRICS_CACHE = {}
_METRICS_CACHE_MAX_ITEMS = 4096
_METRICS_CACHE_TTL_TRADING_SEC = 20 * 60
_METRICS_CACHE_TTL_OFFHOURS_SEC = 6 * 60 * 60


def _normalize_stock_code(code: str) -> str:
    raw = str(code or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith(("sh", "sz", "bj")):
        return raw
    digits = "".join(filter(str.isdigit, raw))
    if len(digits) != 6:
        return raw
    if digits.startswith("6"):
        return f"sh{digits}"
    if digits.startswith(("0", "3")):
        return f"sz{digits}"
    if digits.startswith(("8", "4", "9")):
        return f"bj{digits}"
    return raw


def _metrics_ttl_sec() -> int:
    return _METRICS_CACHE_TTL_TRADING_SEC if is_trading_time() else _METRICS_CACHE_TTL_OFFHOURS_SEC


def _zero_metrics() -> dict:
    return {
        "seal_rate": 0,
        "broken_rate": 0,
        "next_day_premium": 0,
        "limit_up_days": 0,
    }


def _get_metrics_cache(code: str):
    if not code:
        return None
    now_ts = time_module.time()
    ttl = _metrics_ttl_sec()
    with _METRICS_CACHE_LOCK:
        item = _METRICS_CACHE.get(code)
        if not isinstance(item, dict):
            return None
        ts = float(item.get("ts", 0) or 0)
        if ts <= 0 or now_ts - ts > ttl:
            _METRICS_CACHE.pop(code, None)
            return None
        data = item.get("data")
        if isinstance(data, dict):
            return dict(data)
    return None


def _set_metrics_cache(code: str, data: dict):
    if not code or not isinstance(data, dict):
        return
    now_ts = time_module.time()
    with _METRICS_CACHE_LOCK:
        _METRICS_CACHE[code] = {"ts": now_ts, "data": dict(data)}
        if len(_METRICS_CACHE) <= _METRICS_CACHE_MAX_ITEMS:
            return
        # Keep memory bounded: evict oldest 10%.
        evict_count = max(1, int(_METRICS_CACHE_MAX_ITEMS * 0.1))
        oldest = sorted(_METRICS_CACHE.items(), key=lambda x: float((x[1] or {}).get("ts", 0) or 0))[:evict_count]
        for key, _ in oldest:
            _METRICS_CACHE.pop(key, None)

def is_trading_time():
    """
    Check if current time is within trading hours (9:15 - 15:00) on a weekday.
    Simple check, does not account for public holidays.
    """
    now = datetime.now()
    
    # Check if weekend (Saturday=5, Sunday=6)
    if now.weekday() >= 5:
        return False
        
    current_time = now.time()
    start_time = time(9, 15)
    end_time = time(15, 5) # Allow 5 mins buffer
    
    # Lunch break check (11:30 - 13:00) - Optional, but good for reducing load
    lunch_start = time(11, 35)
    lunch_end = time(12, 55)
    
    if start_time <= current_time <= end_time:
        if lunch_start <= current_time <= lunch_end:
            return False
        return True
        
    return False

def is_market_open_day():
    """Check if today is a potential trading day (Mon-Fri) and not a holiday."""
    now = datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    
    # 2026 Holidays (Simple List)
    # 元旦: 1.1 - 1.3
    # 春节: 2.17 - 2.24 (Approx)
    # 清明: 4.4 - 4.6
    # 劳动: 5.1 - 5.5
    # 端午: 6.19 - 6.21
    # 中秋: 9.25 - 9.27
    # 国庆: 10.1 - 10.7
    HOLIDAYS_2026 = [
        "2026-01-01", "2026-01-02", "2026-01-03",
        "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23", "2026-02-24",
        "2026-04-04", "2026-04-05", "2026-04-06",
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-06-19", "2026-06-20", "2026-06-21",
        "2026-09-25", "2026-09-26", "2026-09-27",
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07"
    ]
    
    if date_str in HOLIDAYS_2026:
        return False
        
    return now.weekday() < 5

def fetch_history_data(code, days=300):
    """
    Fetch last N days of K-line data.
    """
    safe_days = max(60, int(days or 300))
    try:
        day_rows = data_provider.fetch_day_kline_history(code, days=safe_days)
        if isinstance(day_rows, list) and day_rows:
            normalized = []
            for row in day_rows:
                if not isinstance(row, dict):
                    continue
                day = str(row.get("date") or row.get("day") or "").strip()
                if not day:
                    continue
                normalized.append({
                    "day": day,
                    "open": row.get("open", 0),
                    "high": row.get("high", 0),
                    "low": row.get("low", 0),
                    "close": row.get("close", 0),
                    "volume": row.get("volume", 0),
                })
            if normalized:
                return normalized[-safe_days:]
    except Exception:
        pass
    return data_provider.fetch_history_data(code, safe_days)

def calculate_metrics(code):
    """
    Calculate advanced metrics based on history.
    """
    norm_code = _normalize_stock_code(code)
    cached = _get_metrics_cache(norm_code)
    if isinstance(cached, dict):
        return cached

    klines = fetch_history_data(norm_code, days=300)
    if not klines:
        result = _zero_metrics()
        _set_metrics_cache(norm_code, result)
        return result
    
    # Parse data
    # Sina Format: [{"day":"2023-01-01","open":"10.0","high":"10.5","low":"9.9","close":"10.2","volume":"1000"}, ...]
    
    parsed_data = []
    for item in klines:
        if not isinstance(item, dict):
            continue
        day = str(item.get('day') or item.get('date') or '').strip()
        if not day:
            continue
        try:
            parsed_data.append({
                "date": day,
                "open": float(item.get('open') or 0),
                "close": float(item.get('close') or 0),
                "high": float(item.get('high') or 0),
                "low": float(item.get('low') or 0),
            })
        except Exception:
            continue

    if len(parsed_data) < 2:
        result = _zero_metrics()
        _set_metrics_cache(norm_code, result)
        return result
        
    # Determine limit up threshold
    # 20% for 688(sh) and 300(sz), 30% for 8xx(bj - ignored for now), 10% others
    is_20cm = norm_code.startswith('sh688') or norm_code.startswith('sz30')
    limit_threshold = 1.195 if is_20cm else 1.095
    
    first_board_attempts = 0
    first_board_failures = 0
    
    consecutive_attempts = 0
    consecutive_successes = 0
    
    premium_sum = 0
    premium_count = 0
    
    # Calculate metrics
    for i in range(1, len(parsed_data)):
        prev = parsed_data[i-1]
        curr = parsed_data[i]
        
        prev_close = prev['close']
        
        # Check if touched limit up (High >= Limit Price approx)
        high_pct = curr['high'] / prev_close
        is_attempt = high_pct >= limit_threshold
        is_sealed = (curr['close'] / prev_close) >= limit_threshold
        
        # Check if previous day was limit up
        prev_is_limit_up = False
        if i > 1:
            prev_prev = parsed_data[i-2]
            if (prev['close'] / prev_prev['close']) >= limit_threshold:
                prev_is_limit_up = True
        
        if is_attempt:
            if prev_is_limit_up:
                # Consecutive attempt (Promotion)
                consecutive_attempts += 1
                if is_sealed:
                    consecutive_successes += 1
            else:
                # First board attempt
                first_board_attempts += 1
                if not is_sealed:
                    first_board_failures += 1
            
            if is_sealed:
                # Calculate next day premium if available
                if i + 1 < len(parsed_data):
                    next_day = parsed_data[i+1]
                    # Premium = (Open - PrevClose) / PrevClose
                    premium = ((next_day['open'] - curr['close']) / curr['close']) * 100
                    premium_sum += premium
                    premium_count += 1

    # Finalize metrics
    seal_rate = 0
    if first_board_attempts > 0:
        seal_rate = ((first_board_attempts - first_board_failures) / first_board_attempts) * 100
        
    broken_rate = 0
    if first_board_attempts > 0:
        broken_rate = (first_board_failures / first_board_attempts) * 100
        
    next_day_premium = 0
    if premium_count > 0:
        next_day_premium = premium_sum / premium_count
        
    # Calculate current limit up days (consecutive)
    limit_up_days = 0
    for i in range(len(parsed_data)-1, 0, -1):
        curr = parsed_data[i]
        prev = parsed_data[i-1]
        if (curr['close'] / prev['close']) >= limit_threshold:
            limit_up_days += 1
        else:
            break
            
    result = {
        "seal_rate": round(seal_rate, 1),
        "broken_rate": round(broken_rate, 1),
        "next_day_premium": round(next_day_premium, 2),
        "limit_up_days": limit_up_days
    }
    _set_metrics_cache(norm_code, result)
    return result
