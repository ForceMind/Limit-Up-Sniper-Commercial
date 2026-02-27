import json
from datetime import datetime, time
from app.core.data_provider import data_provider

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
    return data_provider.fetch_history_data(code, days)

def calculate_metrics(code):
    """
    Calculate advanced metrics based on history.
    """
    klines = fetch_history_data(code, days=300)
    if not klines:
        return {
            "seal_rate": 0,
            "broken_rate": 0,
            "next_day_premium": 0,
            "limit_up_days": 0
        }
    
    # Parse data
    # Sina Format: [{"day":"2023-01-01","open":"10.0","high":"10.5","low":"9.9","close":"10.2","volume":"1000"}, ...]
    
    parsed_data = []
    for item in klines:
        parsed_data.append({
            "date": item['day'],
            "open": float(item['open']),
            "close": float(item['close']),
            "high": float(item['high']),
            "low": float(item['low'])
        })
        
    # Determine limit up threshold
    # 20% for 688(sh) and 300(sz), 30% for 8xx(bj - ignored for now), 10% others
    is_20cm = code.startswith('sh688') or code.startswith('sz30')
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
            
    return {
        "seal_rate": round(seal_rate, 1),
        "broken_rate": round(broken_rate, 1),
        "next_day_premium": round(next_day_premium, 2),
        "limit_up_days": limit_up_days
    }
