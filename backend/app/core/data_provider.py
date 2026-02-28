import akshare as ak
import requests
import pandas as pd
import time
import json
import threading
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

class DataProvider:
    def __init__(self, logger=None):
        self.logger = logger
        self._last_market_df = None
        self._last_market_ts = 0
        self._last_failure_ts = 0
        self._market_fail_cooldown_base_sec = 3600
        self._market_fail_cooldown_sec = self._market_fail_cooldown_base_sec
        self._market_fail_cooldown_max_sec = 86400
        self._market_fail_streak = 0
        self._failure_cooldown_skip_log_ts = 0
        self._non_trading_skip_log_ts = 0
        self._eastmoney_next_retry_ts = 0
        self._eastmoney_fail_cooldown_base_sec = 3600
        self._eastmoney_fail_cooldown_sec = self._eastmoney_fail_cooldown_base_sec
        self._eastmoney_fail_cooldown_max_sec = 86400
        self._eastmoney_fail_streak = 0
        self._eastmoney_last_error_log_ts = 0
        self._eastmoney_skip_log_ts = 0
        self._base_info_df = None
        self._base_info_ts = 0
        self._base_info_retry_ts = 0
        self._base_info_next_retry_ts = 0
        self._base_info_fail_cooldown_base_sec = 3600
        self._base_info_fail_cooldown_sec = self._base_info_fail_cooldown_base_sec
        self._base_info_fail_cooldown_max_sec = 86400
        self._base_info_fail_streak = 0
        self._base_info_last_error_log_ts = 0
        self._base_info_skip_scan_log_ts = 0
        self._base_info_lock = threading.Lock()
        self._lock = threading.Lock() # Global lock for heavy operations (market overview)
        self._biying_usage_lock = threading.Lock()
        self._biying_minute_state = {"minute": "", "count": 0}
        self._biying_quota_log_ts = 0.0
        self._biying_http_err_log_ts = 0.0
        self._biying_quote_cache = {}
        self._biying_quote_cache_ttl = 30
        self._biying_intraday_cache = {}
        self._biying_intraday_cache_ttl = 600
        self._biying_day_kline_cache = {}
        self._biying_day_kline_cache_ttl = 3600
        self._biying_stock_info_cache = {}
        self._biying_stock_info_cache_ttl = 86400 * 7
        self._biying_stock_info_cache_file = DATA_DIR / "biying_stock_info_cache.json"
        self._biying_stock_list_file = DATA_DIR / "biying_stock_list.json"
        self._biying_stock_list_map = {}
        self._biying_stock_list_last_refresh = ""
        self._market_cache_ttl_sec = 900
        self._provider_throttle_lock = threading.Lock()
        self._provider_last_request_ts = {}
        self._provider_exec_locks = {}
        self._provider_min_interval_sec = {
            "biying": 0.025,   # <= 40 req/s, below 3000 req/min limit
            "akshare": 1.2,
            "sina": 0.25,
            "eastmoney": 0.3,
            "tushare": 1.0,
        }
        self._load_biying_cache_files()

    def log(self, msg):
        if self.logger:
            self.logger(msg)
        else:
            print(msg)

    def _today_cn_ymd(self):
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")

    def _safe_float(self, value, default=0.0):
        try:
            if value is None:
                return float(default)
            text = str(value).strip()
            if not text or text in {"--", "None", "nan"}:
                return float(default)
            return float(text.replace(",", ""))
        except Exception:
            return float(default)

    def _first_value(self, row, keys, default=None):
        if not isinstance(row, dict):
            return default
        for key in keys:
            if key in row:
                value = row.get(key)
                if value is not None and str(value).strip() not in {"", "--", "null", "None"}:
                    return value
        return default

    def _throttle_provider_request(self, provider_key, min_interval_sec=None):
        key = str(provider_key or "generic").strip().lower() or "generic"
        interval = self._provider_min_interval_sec.get(key, 0.2) if min_interval_sec is None else max(0.0, float(min_interval_sec))
        with self._provider_throttle_lock:
            now_ts = time.time()
            last_ts = float(self._provider_last_request_ts.get(key, 0.0) or 0.0)
            wait_s = interval - (now_ts - last_ts)
            if wait_s > 0:
                time.sleep(wait_s)
            self._provider_last_request_ts[key] = time.time()

    def _call_provider(self, provider_key, func, min_interval_sec=None):
        key = str(provider_key or "generic").strip().lower() or "generic"
        lock = self._provider_exec_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            self._provider_exec_locks[key] = lock
        with lock:
            self._throttle_provider_request(key, min_interval_sec=min_interval_sec)
            return func()

    def _load_biying_cache_files(self):
        try:
            if self._biying_stock_info_cache_file.exists():
                with open(self._biying_stock_info_cache_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    for k, v in loaded.items():
                        if not isinstance(v, dict):
                            continue
                        ts = float(v.get("ts", 0) or 0)
                        data = v.get("data")
                        if ts > 0 and isinstance(data, dict):
                            self._biying_stock_info_cache[str(k)] = {"ts": ts, "data": data}
        except Exception:
            pass

        try:
            if self._biying_stock_list_file.exists():
                with open(self._biying_stock_list_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    stock_map = loaded.get("stocks", {})
                    refresh_date = str(loaded.get("refresh_date", "") or "")
                    if isinstance(stock_map, dict):
                        self._biying_stock_list_map = {str(k): str(v) for k, v in stock_map.items() if str(k).isdigit()}
                    self._biying_stock_list_last_refresh = refresh_date
        except Exception:
            pass

    def _save_biying_stock_info_cache(self):
        try:
            now_ts = time.time()
            kept = {}
            for code, payload in self._biying_stock_info_cache.items():
                ts = float(payload.get("ts", 0) or 0)
                data = payload.get("data")
                if now_ts - ts <= self._biying_stock_info_cache_ttl and isinstance(data, dict):
                    kept[str(code)] = {"ts": ts, "data": data}
            with open(self._biying_stock_info_cache_file, "w", encoding="utf-8") as f:
                json.dump(kept, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _save_biying_stock_list_cache(self):
        try:
            payload = {
                "refresh_date": self._biying_stock_list_last_refresh,
                "stocks": self._biying_stock_list_map,
            }
            with open(self._biying_stock_list_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _get_biying_config(self):
        try:
            from app.core.config_manager import SYSTEM_CONFIG
            cfg = SYSTEM_CONFIG.get("data_provider_config", {}) or {}
            try:
                minute_limit = int(cfg.get("biying_minute_limit", 3000) or 3000)
            except Exception:
                minute_limit = 3000
            return {
                "enabled": bool(cfg.get("biying_enabled")),
                "license_key": str(cfg.get("biying_license_key", "")).strip(),
                "endpoint": str(cfg.get("biying_endpoint", "")).strip(),
                "cert_path": str(cfg.get("biying_cert_path", "")).strip(),
                "minute_limit": max(1, min(minute_limit, 100000)),
            }
        except Exception:
            return {
                "enabled": False,
                "license_key": "",
                "endpoint": "",
                "cert_path": "",
                "minute_limit": 3000,
            }

    def _biying_enabled(self, cfg=None):
        cfg = cfg or self._get_biying_config()
        return bool(cfg.get("enabled")) and bool(cfg.get("license_key"))

    def _biying_base_url(self, cfg):
        endpoint = str((cfg or {}).get("endpoint", "")).strip()
        if not endpoint:
            return "https://api.biyingapi.com"
        parsed = urlsplit(endpoint)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return "https://api.biyingapi.com"

    def _reserve_biying_quota(self, cfg, calls=1):
        calls = max(1, int(calls or 1))
        with self._biying_usage_lock:
            minute_key = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d%H%M")
            if self._biying_minute_state.get("minute") != minute_key:
                self._biying_minute_state = {"minute": minute_key, "count": 0}
            minute_count = int(self._biying_minute_state.get("count", 0) or 0)
            minute_limit = max(1, int((cfg or {}).get("minute_limit", 3000) or 3000))
            if minute_count + calls > minute_limit:
                now_ts = time.time()
                if now_ts - self._biying_quota_log_ts >= 60:
                    remain = max(0, minute_limit - minute_count)
                    self.log(f"[*] 必盈分钟频率保护触发（剩余 {remain}/{minute_limit} 次/分钟），回退到默认数据源")
                    self._biying_quota_log_ts = now_ts
                return False
            self._biying_minute_state["count"] = minute_count + calls
            return True

    def _biying_request_json(self, path, params=None, timeout=6):
        cfg = self._get_biying_config()
        if not self._biying_enabled(cfg):
            return None
        if not self._reserve_biying_quota(cfg, 1):
            return None

        base_url = self._biying_base_url(cfg).rstrip("/")
        req_path = "/" + str(path or "").lstrip("/")
        url = f"{base_url}{req_path}"
        cert_value = None
        cert_path = str(cfg.get("cert_path", "")).strip()
        if cert_path:
            parts = [p.strip() for p in cert_path.split(",") if p.strip()]
            if len(parts) == 1:
                cert_value = parts[0]
            elif len(parts) >= 2:
                cert_value = (parts[0], parts[1])

        try:
            with requests.Session() as session:
                session.trust_env = False
                request_kwargs = {
                    "params": params or {},
                    "timeout": timeout,
                    "headers": {
                        "User-Agent": "Limit-Up-Sniper/2.x",
                        "Accept": "application/json,text/plain,*/*",
                    },
                }
                if cert_value:
                    request_kwargs["cert"] = cert_value
                resp = self._call_provider("biying", lambda: session.get(url, **request_kwargs))
            if resp.status_code != 200:
                now_ts = time.time()
                if now_ts - self._biying_http_err_log_ts >= 60:
                    self.log(f"[!] 必盈接口返回异常状态码 {resp.status_code}: {url}")
                    self._biying_http_err_log_ts = now_ts
                return None
            try:
                return resp.json()
            except Exception:
                text = (resp.text or "").strip()
                if not text:
                    return None
                return json.loads(text)
        except Exception as e:
            now_ts = time.time()
            if now_ts - self._biying_http_err_log_ts >= 60:
                self.log(f"[!] 必盈请求失败: {e}")
                self._biying_http_err_log_ts = now_ts
            return None

    def _extract_biying_rows(self, payload):
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "result", "rows", "list", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    for sub_key in ("list", "rows", "items"):
                        sub_value = value.get(sub_key)
                        if isinstance(sub_value, list):
                            return sub_value
            dict_values = [v for v in payload.values() if isinstance(v, dict)]
            if dict_values:
                return dict_values
            if any(k in payload for k in ("code", "symbol", "stock_code", "close", "latest", "price")):
                return [payload]
        return []

    def _normalize_biying_symbol(self, code):
        clean_code = "".join(filter(str.isdigit, str(code or "")))
        if len(clean_code) > 6:
            clean_code = clean_code[-6:]
        if len(clean_code) < 6:
            return ""
        if clean_code.startswith("6"):
            return f"{clean_code}.SH"
        if clean_code.startswith("0") or clean_code.startswith("3"):
            return f"{clean_code}.SZ"
        if clean_code.startswith("8") or clean_code.startswith("4") or clean_code.startswith("9"):
            return f"{clean_code}.BJ"
        return f"{clean_code}.SZ"

    def _normalize_biying_time(self, raw_value):
        text = str(raw_value or "").strip()
        if not text:
            return ""
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 14:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
        if len(digits) == 12:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:00"
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        if len(digits) == 6 and ":" not in text:
            return f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}"
        return text

    def _should_refresh_biying_stock_list(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        today = now.strftime("%Y%m%d")
        if not self._biying_stock_list_map:
            return True
        if self._biying_stock_list_last_refresh == today:
            return False
        # Stock list updates daily around 16:20. Before that, reuse previous day list.
        if now.hour < 16 or (now.hour == 16 and now.minute < 20):
            return False
        return True

    def _fetch_stock_list_biying(self, force=False):
        cfg = self._get_biying_config()
        if not self._biying_enabled(cfg):
            return {}

        should_refresh = bool(force) or self._should_refresh_biying_stock_list()
        if not should_refresh and self._biying_stock_list_map:
            return self._biying_stock_list_map

        payload = self._biying_request_json(
            f"/hslt/list/{cfg['license_key']}",
            timeout=8,
        )
        rows = self._extract_biying_rows(payload)
        if not rows and isinstance(payload, dict):
            rows = [payload]
        if not rows:
            return self._biying_stock_list_map

        stock_map = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_code = self._first_value(row, ["code", "dm", "stock_code", "symbol", "股票代码"], "")
            clean_code = "".join(filter(str.isdigit, str(raw_code or "")))
            if len(clean_code) > 6:
                clean_code = clean_code[-6:]
            if len(clean_code) != 6:
                continue
            name = str(self._first_value(row, ["name", "mc", "stock_name", "简称", "股票简称"], clean_code) or clean_code).strip()
            stock_map[clean_code] = name

        if stock_map:
            self._biying_stock_list_map = stock_map
            self._biying_stock_list_last_refresh = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
            self._save_biying_stock_list_cache()
        return self._biying_stock_list_map

    def _is_bse(self, code):
        return code.startswith('8') or code.startswith('4') or code.startswith('92') or code.startswith('bj')

    def _format_code(self, code):
        """Ensure code has prefix (sh/sz/bj)"""
        code = str(code)
        if code.startswith('sh') or code.startswith('sz') or code.startswith('bj'):
            return code
        
        if code.startswith('6'):
            return f"sh{code}"
        elif code.startswith('0') or code.startswith('3'):
            return f"sz{code}"
        elif code.startswith('8') or code.startswith('4') or code.startswith('9'):
            return f"bj{code}"
        return code

    def _strip_code(self, code):
        """Remove prefix"""
        return code.replace('sh', '').replace('sz', '').replace('bj', '')

    def _is_market_trading_session(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if now.weekday() >= 5:
            return False
        now_t = now.time()
        in_morning = dt_time(9, 15) <= now_t <= dt_time(11, 35)
        in_afternoon = dt_time(12, 55) <= now_t <= dt_time(15, 5)
        return in_morning or in_afternoon

    def _mark_market_success(self):
        if self._market_fail_streak > 0:
            upgraded_base = max(
                int(self._market_fail_cooldown_base_sec or 3600),
                int(self._market_fail_cooldown_sec or 3600),
            )
            self._market_fail_cooldown_base_sec = min(upgraded_base, self._market_fail_cooldown_max_sec)
        self._market_fail_streak = 0
        self._market_fail_cooldown_sec = self._market_fail_cooldown_base_sec
        self._last_failure_ts = 0

    def _mark_market_failure(self):
        if self._market_fail_streak <= 0:
            next_cooldown = self._market_fail_cooldown_base_sec
        else:
            next_cooldown = min(
                self._market_fail_cooldown_sec * 2,
                self._market_fail_cooldown_max_sec,
            )
        self._market_fail_streak += 1
        self._market_fail_cooldown_sec = int(next_cooldown)
        self._last_failure_ts = time.time()
        return self._market_fail_cooldown_sec

    def _mark_eastmoney_success(self):
        if self._eastmoney_fail_streak > 0:
            upgraded_base = max(
                int(self._eastmoney_fail_cooldown_base_sec or 3600),
                int(self._eastmoney_fail_cooldown_sec or 3600),
            )
            self._eastmoney_fail_cooldown_base_sec = min(upgraded_base, self._eastmoney_fail_cooldown_max_sec)
        self._eastmoney_fail_streak = 0
        self._eastmoney_fail_cooldown_sec = self._eastmoney_fail_cooldown_base_sec
        self._eastmoney_next_retry_ts = 0
        self._eastmoney_last_error_log_ts = 0

    def _mark_eastmoney_failure(self):
        if self._eastmoney_fail_streak <= 0:
            next_cooldown = self._eastmoney_fail_cooldown_base_sec
        else:
            next_cooldown = min(
                self._eastmoney_fail_cooldown_sec * 2,
                self._eastmoney_fail_cooldown_max_sec,
            )
        self._eastmoney_fail_streak += 1
        self._eastmoney_fail_cooldown_sec = int(next_cooldown)
        now_ts = time.time()
        self._eastmoney_next_retry_ts = now_ts + self._eastmoney_fail_cooldown_sec
        return self._eastmoney_fail_cooldown_sec

    def _mark_base_info_success(self):
        if self._base_info_fail_streak > 0:
            upgraded_base = max(
                int(self._base_info_fail_cooldown_base_sec or 3600),
                int(self._base_info_fail_cooldown_sec or 3600),
            )
            self._base_info_fail_cooldown_base_sec = min(upgraded_base, self._base_info_fail_cooldown_max_sec)
        self._base_info_fail_streak = 0
        self._base_info_fail_cooldown_sec = self._base_info_fail_cooldown_base_sec
        self._base_info_next_retry_ts = 0
        self._base_info_last_error_log_ts = 0

    def _mark_base_info_failure(self):
        if self._base_info_fail_streak <= 0:
            next_cooldown = self._base_info_fail_cooldown_base_sec
        else:
            next_cooldown = min(
                self._base_info_fail_cooldown_sec * 2,
                self._base_info_fail_cooldown_max_sec,
            )
        self._base_info_fail_streak += 1
        self._base_info_fail_cooldown_sec = int(next_cooldown)
        now_ts = time.time()
        self._base_info_next_retry_ts = now_ts + self._base_info_fail_cooldown_sec
        return self._base_info_fail_cooldown_sec

    def _parse_biying_quote_row(self, row, default_clean_code=""):
        if not isinstance(row, dict):
            return None
        raw_code = self._first_value(row, ["code", "stock_code", "symbol", "dm", "股票代码"], default_clean_code)
        raw_code = str(raw_code or "").strip()
        if "." in raw_code:
            raw_code = raw_code.split(".", 1)[0]
        clean_code = "".join(filter(str.isdigit, raw_code))
        if len(clean_code) > 6:
            clean_code = clean_code[-6:]
        if len(clean_code) < 6:
            return None

        full_code = self._format_code(clean_code)
        name = str(self._first_value(row, ["name", "stock_name", "简称", "股票简称"], clean_code))
        if name == clean_code:
            name = str(self._fetch_stock_list_biying().get(clean_code, clean_code))
        if name == clean_code and self._base_info_df is not None and not self._base_info_df.empty:
            try:
                matched = self._base_info_df[self._base_info_df["code"] == full_code]
                if not matched.empty:
                    name = str(matched.iloc[0].get("name", name) or name)
            except Exception:
                pass
        current = self._safe_float(self._first_value(row, ["latest", "price", "current", "close", "now", "p", "最新价", "现价"], 0))
        prev_close = self._safe_float(self._first_value(row, ["prev_close", "pre_close", "yclose", "yc", "昨收", "昨收价"], 0))
        if current <= 0 and prev_close > 0:
            current = prev_close
        open_price = self._safe_float(self._first_value(row, ["open", "open_price", "o", "今开"], current))
        high_price = self._safe_float(self._first_value(row, ["high", "h", "最高"], current))
        raw_change_percent = self._first_value(row, ["change_percent", "pct_chg", "change_rate", "pc", "涨跌幅"], None)
        if raw_change_percent is None:
            change_percent = ((current - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
        else:
            change_percent = self._safe_float(raw_change_percent, 0.0)
        turnover = self._safe_float(self._first_value(row, ["turnover", "turnover_rate", "tr", "换手", "换手率"], 0))
        circ_mv = self._safe_float(self._first_value(row, ["circulation_value", "circ_mv", "float_mv", "流通市值"], 0))
        ask1_vol = self._safe_float(self._first_value(row, ["ask1_vol", "sell1_vol", "卖一量"], 0))
        bid1_price = self._safe_float(self._first_value(row, ["bid1_price", "buy1_price", "买一价"], current))

        is_20cm = clean_code.startswith("30") or clean_code.startswith("68")
        limit_ratio = 1.2 if is_20cm else 1.1
        limit_up_price = round(prev_close * limit_ratio, 2) if prev_close > 0 else 0.0
        raw_limit_flag = self._first_value(row, ["is_limit_up", "zt", "涨停"], None)
        if raw_limit_flag is None:
            is_limit_up = bool(current >= limit_up_price - 0.01) if limit_up_price > 0 else False
        else:
            is_limit_up = str(raw_limit_flag).strip().lower() in {"1", "true", "yes", "y", "涨停"}

        return {
            "code": full_code,
            "name": name,
            "current": round(current, 3),
            "change_percent": round(change_percent, 2),
            "high": round(high_price if high_price > 0 else current, 3),
            "open": round(open_price if open_price > 0 else current, 3),
            "prev_close": round(prev_close, 3),
            "turnover": round(turnover, 2),
            "limit_up_price": limit_up_price,
            "is_limit_up": is_limit_up,
            "ask1_vol": ask1_vol,
            "bid1_price": bid1_price,
            "circulation_value": circ_mv,
        }

    def _parse_biying_kline_rows(self, payload):
        rows = self._extract_biying_rows(payload)
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_time = self._first_value(
                row,
                ["time", "datetime", "date", "trade_time", "t", "交易时间", "时间", "日期", "day"],
                ""
            )
            ts = self._normalize_biying_time(raw_time)
            open_price = self._safe_float(self._first_value(row, ["open", "open_price", "o", "开盘", "开盘价"], 0))
            close_price = self._safe_float(self._first_value(row, ["close", "latest", "price", "c", "收盘", "收盘价"], 0))
            high_price = self._safe_float(self._first_value(row, ["high", "h", "最高", "最高价"], close_price))
            low_price = self._safe_float(self._first_value(row, ["low", "l", "最低", "最低价"], close_price))
            volume = self._safe_float(self._first_value(row, ["volume", "vol", "v", "成交量"], 0))
            amount = self._safe_float(self._first_value(row, ["amount", "turnover_amount", "a", "成交额", "成交金额"], 0))

            if close_price <= 0:
                continue
            out.append({
                "time": ts,
                "date": ts[:10] if len(ts) >= 10 else ts,
                "open": open_price if open_price > 0 else close_price,
                "close": close_price,
                "high": high_price if high_price > 0 else close_price,
                "low": low_price if low_price > 0 else close_price,
                "volume": volume,
                "amount": amount,
            })
        if not out:
            return out
        out.sort(key=lambda x: x.get("time", ""))
        return out

    def _fetch_stock_info_biying(self, code):
        cfg = self._get_biying_config()
        if not self._biying_enabled(cfg):
            return {}
        clean_code = "".join(filter(str.isdigit, self._strip_code(code)))
        if len(clean_code) != 6:
            return {}

        now_ts = time.time()
        cached = self._biying_stock_info_cache.get(clean_code)
        if cached and now_ts - cached.get("ts", 0) < self._biying_stock_info_cache_ttl:
            return dict(cached.get("data") or {})

        payload = self._biying_request_json(
            f"/hszg/zg/{clean_code}/{cfg['license_key']}",
            timeout=6,
        )
        rows = self._extract_biying_rows(payload)
        if not rows and isinstance(payload, dict):
            rows = [payload]
        if not rows:
            return {}

        concepts = []
        industries = []
        indexes = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            concept = self._first_value(row, ["concept", "concept_name", "概念", "概念名称"], "")
            industry = self._first_value(row, ["industry", "industry_name", "行业", "所属行业"], "")
            index_name = self._first_value(row, ["index", "index_name", "指数", "相关指数"], "")
            label = str(self._first_value(row, ["name", "title", "label"], "") or "").strip()
            if label:
                tail = label.split("-")[-1].strip()
                if ("概念" in label) and tail:
                    concept = concept or tail
                if ("行业" in label or "申万" in label) and ("概念" not in label) and tail:
                    industry = industry or tail
                if ("指数" in label) and tail:
                    index_name = index_name or tail

            if concept:
                concepts.append(str(concept).strip())
            if industry:
                industries.append(str(industry).strip())
            if index_name:
                indexes.append(str(index_name).strip())

        def _uniq(items):
            out = []
            seen = set()
            for item in items:
                if item and item not in seen:
                    seen.add(item)
                    out.append(item)
            return out

        concepts = _uniq(concepts)
        industries = _uniq(industries)
        indexes = _uniq(indexes)

        parts = []
        if industries:
            parts.append("/".join(industries[:2]))
        if concepts:
            parts.append("/".join(concepts[:3]))
        if indexes:
            parts.append("/".join(indexes[:2]))

        info = {
            "concept": " | ".join(parts) if parts else "",
            "industry": "/".join(industries),
            "index": "/".join(indexes),
        }
        self._biying_stock_info_cache[clean_code] = {"ts": now_ts, "data": info}
        self._save_biying_stock_info_cache()
        return info

    def _fetch_quotes_biying(self, codes):
        cfg = self._get_biying_config()
        if not self._biying_enabled(cfg):
            return []
        if not codes:
            return []
        # Daily stock list refresh (updated around 16:20) for stable code->name mapping.
        self._fetch_stock_list_biying()

        now_ts = time.time()
        requested_clean_codes = []
        for code in codes:
            clean = "".join(filter(str.isdigit, self._strip_code(code)))
            if len(clean) >= 6:
                requested_clean_codes.append(clean[-6:])
        requested_clean_codes = list(dict.fromkeys(requested_clean_codes))
        if not requested_clean_codes:
            return []

        missing = []
        for clean_code in requested_clean_codes:
            cached = self._biying_quote_cache.get(clean_code)
            if not cached or now_ts - cached.get("ts", 0) >= self._biying_quote_cache_ttl:
                missing.append(clean_code)

        for i in range(0, len(missing), 20):
            batch = missing[i:i + 20]
            payload = self._biying_request_json(
                f"/hsrl/ssjy_more/{cfg['license_key']}",
                params={"stock_codes": ",".join(batch)},
                timeout=6,
            )
            rows = self._extract_biying_rows(payload)
            if not rows and isinstance(payload, dict):
                rows = [payload]
            for row in rows:
                parsed = self._parse_biying_quote_row(row)
                if not parsed:
                    continue
                clean_key = "".join(filter(str.isdigit, self._strip_code(parsed.get("code", ""))))
                if len(clean_key) >= 6:
                    self._biying_quote_cache[clean_key[-6:]] = {"ts": time.time(), "data": parsed}

        results = []
        for clean_code in requested_clean_codes:
            cached = self._biying_quote_cache.get(clean_code)
            if cached:
                results.append(dict(cached.get("data") or {}))
        return results

    def _fetch_intraday_data_biying(self, code):
        cfg = self._get_biying_config()
        if not self._biying_enabled(cfg):
            return None

        clean_code = "".join(filter(str.isdigit, self._strip_code(code)))
        symbol = self._normalize_biying_symbol(clean_code)
        if len(clean_code) != 6 or not symbol:
            return None

        cache_key = f"intraday:{clean_code}"
        now_ts = time.time()
        cached = self._biying_intraday_cache.get(cache_key)
        if cached and now_ts - cached.get("ts", 0) < self._biying_intraday_cache_ttl:
            df = cached.get("data")
            if isinstance(df, pd.DataFrame):
                return df.copy()

        now_dt = datetime.now(ZoneInfo("Asia/Shanghai"))
        end_date = now_dt.strftime("%Y%m%d")
        start_date = (now_dt - pd.Timedelta(days=14)).strftime("%Y%m%d")
        payload = self._biying_request_json(
            f"/hsstock/history/{symbol}/5/n/{cfg['license_key']}",
            params={"st": start_date, "et": end_date, "lt": 240},
            timeout=6,
        )
        rows = self._parse_biying_kline_rows(payload)
        if not rows:
            payload = self._biying_request_json(
                f"/hsstock/latest/{symbol}/5/n/{cfg['license_key']}",
                params={"lt": 5},
                timeout=6,
            )
            rows = self._parse_biying_kline_rows(payload)
        if not rows:
            return None

        df = pd.DataFrame(rows)
        if "time" not in df.columns:
            return None
        if "volume" not in df.columns:
            df["volume"] = 0.0
        if "close" not in df.columns:
            return None
        out_df = df[["time", "close", "volume"]].copy()
        self._biying_intraday_cache[cache_key] = {"ts": now_ts, "data": out_df.copy()}
        return out_df

    def fetch_day_kline_history(self, code, days=365):
        cfg = self._get_biying_config()
        if not self._biying_enabled(cfg):
            return []
        clean_code = "".join(filter(str.isdigit, self._strip_code(code)))
        symbol = self._normalize_biying_symbol(clean_code)
        if len(clean_code) != 6 or not symbol:
            return []

        cache_key = f"day:{clean_code}:{int(days or 365)}"
        now_ts = time.time()
        cached = self._biying_day_kline_cache.get(cache_key)
        if cached and now_ts - cached.get("ts", 0) < self._biying_day_kline_cache_ttl:
            return list(cached.get("data") or [])

        end_date = self._today_cn_ymd()
        start_date = (datetime.now(ZoneInfo("Asia/Shanghai")) - pd.Timedelta(days=max(30, int(days or 365) + 20))).strftime("%Y%m%d")
        payload = self._biying_request_json(
            f"/hsstock/history/{symbol}/d/f/{cfg['license_key']}",
            params={
                "st": start_date,
                "et": end_date,
                "lt": max(120, int(days or 365)),
            },
            timeout=8,
        )
        rows = self._parse_biying_kline_rows(payload)
        if not rows:
            return []
        out = []
        for row in rows[-max(1, int(days or 365)):]:
            if not row.get("date"):
                continue
            out.append({
                "date": row.get("date"),
                "open": self._safe_float(row.get("open"), 0),
                "close": self._safe_float(row.get("close"), 0),
                "high": self._safe_float(row.get("high"), 0),
                "low": self._safe_float(row.get("low"), 0),
                "volume": self._safe_float(row.get("volume"), 0),
            })
        self._biying_day_kline_cache[cache_key] = {"ts": now_ts, "data": out}
        return out

    def fetch_stock_info(self, code):
        """
        Fetch basic info (Industry/Concept) for a single stock.
        """
        biying_info = self._fetch_stock_info_biying(code)
        if biying_info:
            return biying_info
        try:
            clean_code = self._strip_code(code)
            df = self._call_provider("akshare", lambda: ak.stock_individual_info_em(symbol=clean_code))
            if df is None or df.empty:
                return {}

            info = {}
            for _, row in df.iterrows():
                item = str(row.get('item', row.get('项目', ''))).strip()
                value = row.get('value', row.get('值', ''))
                if item in {'行业', '所属行业'}:
                    info['concept'] = value
                elif item == '总市值':
                    info['total_mv'] = value
                elif item == '流通市值':
                    info['circ_mv'] = value

            return info
        except Exception as e:
            self.log(f"获取个股信息失败 {code}: {e}")
            return {}

    def fetch_quotes(self, codes):
        """
        Fetch real-time quotes for a list of codes.
        Returns a list of dicts with standardized fields.
        """
        if not codes:
            return []

        try:
            biying_rows = self._fetch_quotes_biying(codes)
            if biying_rows:
                return biying_rows
        except Exception as e:
            self.log(f"[!] 必盈实时行情获取失败，回退新浪: {e}")

        # Use Sina directly as requested by user (EastMoney is unstable/forbidden for quotes)
        try:
            return self._fetch_quotes_sina(codes)
        except Exception as e:
            self.log(f"[!] 鏂版氮琛屾儏鑾峰彇澶辫触: {e}")
            
        return []

    def fetch_intraday_data(self, code):
        """
        Fetch intraday 1-minute data for a single stock (Sina).
        Returns DataFrame with 'time', 'close', 'volume'.
        """
        try:
            biying_df = self._fetch_intraday_data_biying(code)
            if biying_df is not None and not biying_df.empty:
                return biying_df
        except Exception as e:
            self.log(f"[!] 必盈分时获取失败，回退新浪: {e}")

        full_code = self._format_code(code)
        url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={full_code}&scale=1&ma=no&datalen=240"
        
        try:
            with requests.Session() as session:
                session.trust_env = False
                resp = self._call_provider("sina", lambda: session.get(url, timeout=3))
            
            if not resp.text.strip():
                # self.log(f"[Warn] Empty response for {code}")
                return None

            try:
                data = resp.json()
            except ValueError:
                # Handle "null" or invalid JSON
                return None

            if not data: return None
            
            df = pd.DataFrame(data)
            df['close'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(float)
            # Sina volume is technically number of shares (or lots? Usually shares for API)
            
            # Map columns
            # day -> time, close -> close, volume -> volume
            return df[['day', 'close', 'volume']].rename(columns={'day': 'time'})
            
        except Exception as e:
            self.log(f"鑾峰彇鍒嗘椂鏁版嵁澶辫触 {code}: {e}")
            return None

    def _fetch_quotes_sina(self, codes):
        # Ensure base info exists for circulation value / turnover calculation
        now_ts = time.time()
        need_refresh = (
            self._base_info_df is None
            or self._base_info_df.empty
            or (now_ts - self._base_info_ts > 3600)
        )
        if need_refresh and self._is_market_trading_session() and (now_ts - self._base_info_retry_ts > 300):
            self._base_info_retry_ts = now_ts
            try:
                self.update_base_info()
            except Exception as e:
                self.log(f"[!] fetch_quotes 鍐呮洿鏂板熀纭€淇℃伅澶辫触: {e}")

        # Prepare base info map for CircMV calculation
        base_map = {}
        if self._base_info_df is not None and not self._base_info_df.empty:
            # Create a dict for fast lookup: code -> circ_shares
            # Assuming _base_info_df has 'code' and 'circ_shares'
            try:
                base_map = dict(zip(self._base_info_df['code'], self._base_info_df['circ_shares']))
            except:
                pass

        # Sina supports batch, but URL length limit exists.
        # Split into batches of 50
        stocks = []
        batch_size = 50
        
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i+batch_size]
            url = "http://hq.sinajs.cn/list=" + ",".join(batch)
            headers = {"Referer": "http://finance.sina.com.cn"}
            try:
                # Use session with trust_env=False to bypass system proxy
                with requests.Session() as session:
                    session.trust_env = False
                    resp = self._call_provider("sina", lambda: session.get(url, headers=headers, timeout=5))
                
                resp.encoding = 'gbk'
                
                for line in resp.text.split('\n'):
                    if not line: continue
                    parts = line.split('=')
                    if len(parts) < 2: continue
                    
                    code = parts[0].split('_')[-1]
                    data_str = parts[1].strip('";')
                    if not data_str: continue
                    
                    data = data_str.split(',')
                    if len(data) < 30: continue
                    
                    name = data[0]
                    current = float(data[3])
                    prev_close = float(data[2])
                    if current == 0: current = prev_close
                    
                    change_percent = 0.0
                    if prev_close > 0:
                        change_percent = ((current - prev_close) / prev_close) * 100
                        
                    is_20cm = code.startswith('sz30') or code.startswith('sh68')
                    limit_ratio = 1.2 if is_20cm else 1.1
                    limit_up_price = round(prev_close * limit_ratio, 2)
                    
                    # Parse Sell 1 (Ask 1) for sealed check
                    # Index 20: Sell 1 Volume, Index 21: Sell 1 Price
                    ask1_vol = float(data[20])
                    ask1_price = float(data[21])
                    bid1_price = float(data[11]) # Index 11: Buy 1 Price
                    
                    # Strict Sealed Check:
                    # 1. Current price >= Limit Up Price (approx)
                    # 2. Ask 1 Volume is 0 (No sellers) OR Ask 1 Price is 0
                    # Actually, for limit up, usually Ask 1 is empty (0 volume, 0 price)
                    # OR Bid 1 Price == Limit Up Price
                    
                    is_sealed = False
                    if current >= limit_up_price - 0.01:
                        if ask1_vol == 0:
                            is_sealed = True
                    
                    # Calculate CircMV and Turnover
                    circ_mv = 0
                    turnover = 0.0
                    circ_shares = base_map.get(code, 0)
                    volume = float(data[8]) # Volume in shares
                    
                    if circ_shares > 0:
                        circ_mv = circ_shares * current
                        turnover = (volume / circ_shares) * 100
                    
                    stocks.append({
                        "code": code,
                        "name": name,
                        "current": current,
                        "change_percent": round(change_percent, 2),
                        "high": float(data[4]),
                        "open": float(data[1]),
                        "prev_close": prev_close,
                        "turnover": round(turnover, 2), 
                        "limit_up_price": limit_up_price,
                        "is_limit_up": is_sealed, # Use strict check
                        "ask1_vol": ask1_vol,
                        "bid1_price": bid1_price,
                        "circulation_value": circ_mv # Use standard key
                    })
            except Exception as e:
                self.log(f"[!] 鎵归噺鎶撳彇澶辫触: {e}")
                continue
                
        return stocks

    def fetch_all_market_data(self):
        """
        Fetch ALL stocks for market overview and scanning.
        Returns DataFrame.
        """
        now_ts = time.time()

        # Throttle logic
        if self._last_market_df is not None and now_ts - self._last_market_ts < self._market_cache_ttl_sec:
            return self._last_market_df.copy()

        # Non-trading session: never request full-market network data.
        if not self._is_market_trading_session():
            if now_ts - self._non_trading_skip_log_ts >= 60:
                self.log("[*] 当前非交易时段，跳过全市场抓取并复用旧缓存")
                self._non_trading_skip_log_ts = now_ts
            if self._last_market_df is not None:
                return self._last_market_df.copy()
            return None

        # Cooldown prevents hammering API on failures
        if now_ts - self._last_failure_ts < self._market_fail_cooldown_sec:
            if self._last_market_df is not None:
                return self._last_market_df.copy()
            return None

        # Lock to ensure only one thread updates data at a time
        with self._lock:
            now_ts = time.time()
            if self._last_market_df is not None and now_ts - self._last_market_ts < self._market_cache_ttl_sec:
                return self._last_market_df.copy()

            # Re-check non-trading and cooldown inside lock for queued callers
            if not self._is_market_trading_session():
                if now_ts - self._non_trading_skip_log_ts >= 60:
                    self.log("[*] 当前非交易时段，跳过全市场抓取并复用旧缓存")
                    self._non_trading_skip_log_ts = now_ts
                if self._last_market_df is not None:
                    return self._last_market_df.copy()
                return None

            if now_ts - self._last_failure_ts < self._market_fail_cooldown_sec:
                if now_ts - self._failure_cooldown_skip_log_ts >= 60:
                    remain = int(max(0, self._market_fail_cooldown_sec - (now_ts - self._last_failure_ts)))
                    self.log(f"[*] 全市场抓取失败冷却中（剩余{remain}s），复用旧缓存")
                    self._failure_cooldown_skip_log_ts = now_ts
                if self._last_market_df is not None:
                    return self._last_market_df.copy()
                return None

            import os
            old_http = os.environ.get("HTTP_PROXY")
            old_https = os.environ.get("HTTPS_PROXY")
            if old_http:
                os.environ.pop("HTTP_PROXY", None)
            if old_https:
                os.environ.pop("HTTPS_PROXY", None)

            try:
                # 1. Sina paged (primary)
                try:
                    self.log("[*] 正在抓取全市场数据（新浪分页）...")
                    df = self._fetch_sina_market_paged()
                    if df is not None and not df.empty:
                        self._last_market_df = df.copy()
                        self._last_market_ts = time.time()
                        self._mark_market_success()
                        return df
                except Exception as e:
                    self.log(f"[!] 新浪分页抓取失败: {e}")

                # 2-3. EastMoney channel (AKShare + manual paged) with cooldown
                eastmoney_ready = True
                now_ts = time.time()
                if now_ts < self._eastmoney_next_retry_ts:
                    eastmoney_ready = False
                    if now_ts - self._eastmoney_skip_log_ts >= 60:
                        remain = int(max(0, self._eastmoney_next_retry_ts - now_ts))
                        self.log(f"[*] 东财通道冷却中（剩余{remain}s），跳过 AKShare/东财 与 东财手工分页")
                        self._eastmoney_skip_log_ts = now_ts

                if eastmoney_ready:
                    try:
                        self.log("[*] 正在抓取全市场数据（AKShare/东财）...")
                        df = self._call_provider("akshare", lambda: ak.stock_zh_a_spot_em())
                        rename_map = {
                            '代码': 'code', '名称': 'name', '最新价': 'current', '涨跌幅': 'change_percent',
                            '涨速': 'speed', '换手率': 'turnover', '流通市值': 'circ_mv', '昨收': 'prev_close',
                            '最高': 'high', '最低': 'low', '今开': 'open', '成交额': 'amount'
                        }
                        df = df.rename(columns=rename_map)
                        for col in ['current', 'change_percent', 'speed', 'turnover', 'circ_mv', 'prev_close', 'high']:
                            if col in df.columns:
                                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

                        self._last_market_df = df.copy()
                        self._last_market_ts = time.time()
                        self._mark_eastmoney_success()
                        self._mark_market_success()
                        return df
                    except Exception as e:
                        now_ts = time.time()
                        wait_s = self._mark_eastmoney_failure()
                        if now_ts - self._eastmoney_last_error_log_ts >= 60:
                            self.log(f"[!] AKShare/东财抓取失败: {e}（进入通道冷却，{wait_s}s 后重试）")
                            self._eastmoney_last_error_log_ts = now_ts

                    try:
                        self.log("[*] 正在抓取全市场数据（东财手工分页）...")
                        df = self._fetch_em_market_paged()
                        if df is not None and not df.empty:
                            self._last_market_df = df.copy()
                            self._last_market_ts = time.time()
                            self._mark_eastmoney_success()
                            self._mark_market_success()
                            return df
                    except Exception as e:
                        now_ts = time.time()
                        wait_s = self._mark_eastmoney_failure()
                        if now_ts - self._eastmoney_last_error_log_ts >= 60:
                            self.log(f"[!] 东财手工分页抓取失败: {e}（进入通道冷却，{wait_s}s 后重试）")
                            self._eastmoney_last_error_log_ts = now_ts

                # 4. Tushare fallback
                try:
                    self.log("[*] 正在抓取全市场数据（Tushare）...")
                    df = self._fetch_tushare_spot()
                    if df is not None and not df.empty:
                        self._last_market_df = df.copy()
                        self._last_market_ts = time.time()
                        self._mark_market_success()
                        return df
                except Exception as e:
                    self.log(f"[!] Tushare抓取失败: {e}")

                # Full chain failed
                wait_s = self._mark_market_failure()
                now_ts = time.time()
                if now_ts - self._failure_cooldown_skip_log_ts >= 60:
                    self.log(f"[!] 全市场抓取失败，进入失败冷却（{wait_s}s）")
                    self._failure_cooldown_skip_log_ts = now_ts
                if self._last_market_df is not None:
                    return self._last_market_df.copy()
                return None
            finally:
                if old_http:
                    os.environ["HTTP_PROXY"] = old_http
                if old_https:
                    os.environ["HTTPS_PROXY"] = old_https

    def fetch_limit_up_pool(self):
        """Fetch Limit Up Pool"""
        try:
            date_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
            df = self._call_provider("akshare", lambda: ak.stock_zt_pool_em(date=date_str))
            return df
        except Exception as e:
            self.log(f"[!] 娑ㄥ仠姹犳姄鍙栧け璐? {e}")
            return None

    def fetch_broken_limit_pool(self):
        """Fetch Broken Limit Pool"""
        try:
            date_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
            df = self._call_provider("akshare", lambda: ak.stock_zt_pool_zbgc_em(date=date_str))
            return df
        except Exception as e:
            self.log(f"[!] 鐐告澘姹犳姄鍙栧け璐? {e}")
            return None

    def fetch_indices(self):
        """Fetch major indices"""
        try:
            url = "http://hq.sinajs.cn/list=sh000001,sz399001,sz399006"
            headers = {"Referer": "http://finance.sina.com.cn"}
            
            with requests.Session() as session:
                session.trust_env = False
                resp = self._call_provider("sina", lambda: session.get(url, headers=headers, timeout=5))
            
            indices = []
            indices_map = {"sh000001": "涓婅瘉鎸囨暟", "sz399001": "娣辫瘉鎴愭寚", "sz399006": "鍒涗笟鏉挎寚"}
            
            for line in resp.text.split('\n'):
                if not line: continue
                parts = line.split('=')
                if len(parts) < 2: continue
                code = parts[0].split('_')[-1]
                data = parts[1].strip('";').split(',')
                if len(data) < 10: continue
                
                name = indices_map.get(code, code)
                current = float(data[3])
                prev_close = float(data[2])
                amount = float(data[9])
                
                # Fix: If current is 0 (before open), use prev_close
                if current == 0:
                    current = prev_close
                    change = 0.0
                else:
                    change = ((current - prev_close) / prev_close) * 100 if prev_close > 0 else 0
                
                indices.append({
                    "name": name,
                    "current": current,
                    "change": round(change, 2),
                    "amount": round(amount / 100000000, 2)
                })
            return indices
        except Exception as e:
            self.log(f"[!] 鎸囨暟鎶撳彇澶辫触: {e}")
            return []

    def fetch_history_data(self, code, days=300):
        """
        Fetch last N days of K-line data from Sina Finance.
        """
        # Ensure code format for Sina (e.g. sh600519)
        code = self._format_code(code)
        
        url = f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={days}"
        
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = self._call_provider("sina", lambda: requests.get(url, headers=headers, timeout=5))
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                return data
        except Exception as e:
            self.log(f"[!] 鍘嗗彶鏁版嵁鎶撳彇澶辫触 {code}: {e}")
        
        return []

    def search_stock(self, q):
        """
        Search stock by code/name/pinyin.
        """
        if not q:
            return []
            
        url = "https://searchapi.eastmoney.com/api/suggest/get"
        params = {
            "input": q,
            "type": "14", # Stock
            "token": "D43BF722C8E33BDC906FB84D85E326E8",
            "count": 5
        }
        
        try:
            resp = self._call_provider("eastmoney", lambda: requests.get(url, params=params, timeout=3))
            resp.encoding = 'utf-8'
            data = resp.json()
            
            if "QuotationCodeTable" in data and "Data" in data["QuotationCodeTable"]:
                results = []
                for item in data["QuotationCodeTable"]["Data"]:
                    market_type = item.get("MarketType")
                    code = item.get("Code")
                    name = item.get("Name")
                    
                    prefix = ""
                    if market_type == "1": prefix = "sh"
                    elif market_type == "2": prefix = "sz"
                    else: continue 
                    
                    full_code = f"{prefix}{code}"
                    results.append({
                        "code": full_code,
                        "name": name,
                        "display_code": code
                    })
                return results
        except Exception as e:
            self.log(f"[!] 鎼滅储澶辫触: {e}")
            
        return []

    def get_stock_info(self, code):
        """
        Get basic stock info (name, concept) for adding to watchlist.
        """
        name = "未知股票"
        concept = "自选"
        try:
            biying_info = self._fetch_stock_info_biying(code)
            if biying_info and biying_info.get("concept"):
                concept = str(biying_info.get("concept") or concept).strip() or concept
        except Exception:
            pass

        try:
            raw_code = self._strip_code(code)
            market = '1' if str(code).startswith('sh') else '0'
            secid = f"{market}.{raw_code}"

            em_url = f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f14,f127,f116"
            resp = self._call_provider("eastmoney", lambda: requests.get(em_url, timeout=3))
            em_data = resp.json()
            if em_data and em_data.get('data'):
                name = em_data['data'].get('f14', name)
                concept = em_data['data'].get('f127', "")
                industry = em_data['data'].get('f116', "")

                if not concept and industry:
                    concept = industry
                elif concept and industry:
                    concept = f"{industry} | {concept}"
                elif not concept and not industry:
                    concept = "自选"
        except Exception as e:
            self.log(f"[!] 获取股票信息失败: {e}")

        return name, concept

    def _generate_candidate_codes(self):
        """Generate a list of potential A-share codes to scan (Main Board + ChiNext only)."""
        codes = []
        # Shanghai Main: 600000-603999, 605000-605999
        for i in range(600000, 604000): codes.append(f"sh{i}")
        for i in range(605000, 606000): codes.append(f"sh{i}")
        # Shenzhen Main: 000001-003999
        for i in range(1, 4000): codes.append(f"sz{i:06d}")
        # ChiNext: 300000-301999
        for i in range(300000, 302000): codes.append(f"sz{i}")
        return codes

    def _is_target_stock(self, code):
        """
        Filter for A-share Main Board and ChiNext.
        Exclude STAR (688) and BSE (8/4/92/bj).
        """
        code = str(code)
        c = code.replace('sh', '').replace('sz', '').replace('bj', '')
        
        # Exclude BSE
        if c.startswith('8') or c.startswith('4') or c.startswith('92'):
            return False
        # Exclude STAR
        if c.startswith('68'):
            return False
        # Include Main Board (60, 00) and ChiNext (30)
        if c.startswith('60') or c.startswith('00') or c.startswith('30'):
            return True
        return False

    def update_base_info(self):
        """Fetch base info (Name, CircMV, CircShares) from AKShare."""
        now_ts = time.time()

        if not self._is_market_trading_session():
            if now_ts - self._base_info_skip_scan_log_ts >= 60:
                self.log("[*] 当前非交易时段，跳过股票基础信息更新")
                self._base_info_skip_scan_log_ts = now_ts
            return

        if now_ts < self._base_info_next_retry_ts:
            return

        with self._base_info_lock:
            now_ts = time.time()
            if not self._is_market_trading_session():
                if now_ts - self._base_info_skip_scan_log_ts >= 60:
                    self.log("[*] 当前非交易时段，跳过股票基础信息更新")
                    self._base_info_skip_scan_log_ts = now_ts
                return

            if now_ts < self._base_info_next_retry_ts:
                return
            try:
                biying_cfg = self._get_biying_config()
                if self._biying_enabled(biying_cfg):
                    stock_map = self._fetch_stock_list_biying()
                    if stock_map:
                        valid_rows = []
                        for clean_code, name in stock_map.items():
                            if not self._is_target_stock(clean_code):
                                continue
                            valid_rows.append({
                                "code": self._format_code(clean_code),
                                "name": str(name or clean_code),
                                "circ_mv": 0.0,
                                "circ_shares": 0.0,
                            })
                        if valid_rows:
                            self._base_info_df = pd.DataFrame(valid_rows)
                            self._base_info_ts = time.time()
                            self._mark_base_info_success()
                            self.log(f"[*] 已通过必盈股票列表更新基础信息：{len(valid_rows)} 只。")
                            return

                self.log("[*] 正在从 AKShare 更新股票基础信息（用于流通市值与换手率计算）...")
                df = self._call_provider("akshare", lambda: ak.stock_zh_a_spot_em())

                def _pick_col(candidates):
                    for col in candidates:
                        if col in df.columns:
                            return col
                    return None

                code_col = _pick_col(["code", "symbol"])
                name_col = _pick_col(["name"])
                price_col = _pick_col(["price", "latest", "current", "close"])
                circ_mv_col = _pick_col(["circ_mv", "circulation_market_value", "float_mv"])

                if not code_col:
                    for col in df.columns:
                        sample = df[col].astype(str).str.extract(r"(\d{6})", expand=False)
                        if sample.notna().mean() > 0.6:
                            code_col = col
                            break

                if not price_col:
                    for col in df.columns:
                        values = pd.to_numeric(df[col], errors="coerce").dropna()
                        if len(values) < 10:
                            continue
                        median_value = float(values.median())
                        if 0 < median_value < 10000:
                            price_col = col
                            break

                if not circ_mv_col:
                    best_col = None
                    best_median = 0.0
                    for col in df.columns:
                        values = pd.to_numeric(df[col], errors="coerce").dropna()
                        if len(values) < 10:
                            continue
                        median_value = float(values.median())
                        if median_value > best_median and median_value > 1e7:
                            best_median = median_value
                            best_col = col
                    circ_mv_col = best_col

                if not code_col:
                    raise ValueError(f"Cannot find code column from: {list(df.columns)}")

                valid_rows = []
                for _, row in df.iterrows():
                    raw_code = str(row.get(code_col, "")).strip()
                    clean_code = "".join(filter(str.isdigit, self._strip_code(raw_code)))
                    if len(clean_code) < 6:
                        continue
                    clean_code = clean_code[-6:]
                    if not self._is_target_stock(clean_code):
                        continue

                    full_code = self._format_code(clean_code)

                    try:
                        price = float(pd.to_numeric(row.get(price_col, 0), errors="coerce") or 0) if price_col else 0.0
                    except Exception:
                        price = 0.0
                    try:
                        circ_mv = float(pd.to_numeric(row.get(circ_mv_col, 0), errors="coerce") or 0) if circ_mv_col else 0.0
                    except Exception:
                        circ_mv = 0.0

                    circ_shares = (circ_mv / price) if price > 0 else 0.0

                    valid_rows.append({
                        "code": full_code,
                        "name": str(row.get(name_col, full_code)) if name_col else full_code,
                        "circ_mv": circ_mv,
                        "circ_shares": circ_shares,
                    })

                if len(valid_rows) < 500:
                    raise ValueError(f"AKShare基础信息条数异常: {len(valid_rows)}")

                self._base_info_df = pd.DataFrame(valid_rows)
                self._base_info_ts = time.time()
                self._mark_base_info_success()
                self.log(f"[*] 股票基础信息更新完成，已加载 {len(self._base_info_df)} 只股票（用于行情补全）。")
            except Exception as e:
                now_ts = time.time()
                wait_s = self._mark_base_info_failure()
                if now_ts - self._base_info_last_error_log_ts >= 60:
                    self.log(f"[!] 从 AKShare 更新基础信息失败: {e}（进入失败冷却，{wait_s}s 后重试）")
                    self._base_info_last_error_log_ts = now_ts

    def _fetch_sina_market_paged(self):
        """
        Fetch market data by iterating through candidate codes.
        Uses hq.sinajs.cn which is generally more stable.
        """
        now_ts = time.time()
        if self._base_info_df is None or now_ts - self._base_info_ts > 3600:
            self.update_base_info()
            now_ts = time.time()

        if self._base_info_df is not None and not self._base_info_df.empty:
            candidates = self._base_info_df['code'].tolist()
            base_map = self._base_info_df.set_index('code').to_dict('index')
        else:
            if now_ts < self._base_info_next_retry_ts:
                if now_ts - self._base_info_skip_scan_log_ts >= 60:
                    remain = int(max(0, self._base_info_next_retry_ts - now_ts))
                    self.log(f"[*] 基础信息冷却中（剩余{remain}s），跳过全市场扫描并复用旧缓存")
                    self._base_info_skip_scan_log_ts = now_ts
                if self._last_market_df is not None:
                    return self._last_market_df.copy()
                return None
            candidates = self._generate_candidate_codes()
            base_map = {}

        valid_stocks = []
        batch_size = 200

        self.log(f"[*] 正在通过新浪扫描 {len(candidates)} 只股票（每批200）...")

        with requests.Session() as session:
            session.trust_env = False
            headers = {"Referer": "http://finance.sina.com.cn"}

            for i in range(0, len(candidates), batch_size):
                batch = candidates[i:i + batch_size]
                url = "http://hq.sinajs.cn/list=" + ",".join(batch)

                try:
                    resp = self._call_provider("sina", lambda: session.get(url, headers=headers, timeout=5))
                    resp.encoding = 'gbk'

                    for line in resp.text.split('\n'):
                        if not line:
                            continue
                        parts = line.split('=')
                        if len(parts) < 2:
                            continue

                        code = parts[0].split('_')[-1]
                        data_str = parts[1].strip('";')
                        if not data_str:
                            continue

                        data = data_str.split(',')
                        if len(data) < 30:
                            continue

                        name = data[0]
                        open_price = float(data[1])
                        prev_close = float(data[2])
                        current = float(data[3])
                        high = float(data[4])
                        low = float(data[5])
                        volume = float(data[8])
                        amount = float(data[9])

                        if open_price == 0 and volume == 0:
                            continue

                        change_percent = 0.0
                        if prev_close > 0:
                            change_percent = ((current - prev_close) / prev_close) * 100

                        base_data = base_map.get(code, {})
                        circ_shares = base_data.get('circ_shares', 0)

                        turnover = 0.0
                        if circ_shares > 0:
                            turnover = (volume / circ_shares) * 100

                        circ_mv = base_data.get('circ_mv', 0)
                        if circ_shares > 0:
                            circ_mv = circ_shares * current

                        valid_stocks.append({
                            "code": code,
                            "name": name,
                            "current": current,
                            "change_percent": round(change_percent, 2),
                            "open": open_price,
                            "high": high,
                            "low": low,
                            "prev_close": prev_close,
                            "volume": volume,
                            "amount": amount,
                            "turnover": round(turnover, 2),
                            "circ_mv": circ_mv,
                        })

                    time.sleep(0.5)
                except Exception as e:
                    self.log(f"[!] 新浪批次抓取失败 (offset={i}): {e}")
                    time.sleep(1)

        if not valid_stocks:
            return None

        return pd.DataFrame(valid_stocks)

    def _fetch_em_market_paged(self):
        """
        Robust paged fetch for EastMoney.
        Fetches market snapshot in pages to reduce timeout risk.
        """
        all_data = []
        page = 1
        page_size = 500
        max_pages = 20

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "http://quote.eastmoney.com/",
            "Connection": "keep-alive",
        }

        with requests.Session() as session:
            session.trust_env = False
            consecutive_page_failures = 0

            while page <= max_pages:
                try:
                    url = "http://82.push2.eastmoney.com/api/qt/clist/get"
                    params = {
                        "pn": str(page),
                        "pz": str(page_size),
                        "po": "1",
                        "np": "1",
                        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                        "fltt": "2",
                        "invt": "2",
                        "fid": "f3",
                        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
                        "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152",
                    }

                    resp = self._call_provider("eastmoney", lambda: session.get(url, params=params, headers=headers, timeout=5))
                    data = resp.json()

                    if not data or 'data' not in data or 'diff' not in data['data']:
                        break

                    rows = data['data']['diff']
                    if not rows:
                        break

                    all_data.extend(rows)
                    consecutive_page_failures = 0

                    if len(rows) < page_size:
                        break

                    page += 1
                    time.sleep(0.3)
                except Exception as e:
                    self.log(f"[!] 东财第 {page} 页抓取失败: {e}")
                    consecutive_page_failures += 1
                    time.sleep(1)
                    try:
                        resp = self._call_provider("eastmoney", lambda: session.get(url, params=params, headers=headers, timeout=5))
                        data = resp.json()
                        if data and 'data' in data and 'diff' in data['data']:
                            rows = data['data']['diff']
                            all_data.extend(rows)
                            consecutive_page_failures = 0
                            if len(rows) < page_size:
                                break
                            page += 1
                            continue
                    except Exception:
                        pass

                    if consecutive_page_failures >= 2:
                        self.log("[!] 东财分页连续失败，提前终止本轮抓取")
                        break
                    page += 1

        if not all_data:
            return None

        df = pd.DataFrame(all_data)
        rename_map = {
            'f12': 'code', 'f14': 'name', 'f2': 'current', 'f3': 'change_percent',
            'f8': 'turnover', 'f20': 'circ_mv', 'f18': 'prev_close',
            'f15': 'high', 'f16': 'low', 'f17': 'open', 'f6': 'amount',
        }
        df = df.rename(columns=rename_map)

        for col in ['current', 'change_percent', 'prev_close', 'high', 'amount']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        return df

    def _fetch_sina_market_json(self):
        """Fallback: use Sina Market_Center JSON API for full market data."""
        url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getNameList"
        params = {
            "page": 1,
            "num": 5000,
            "sort": "symbol",
            "asc": 1,
            "node": "hs_a",
        }
        headers = {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

        with requests.Session() as session:
            session.trust_env = False
            resp = self._call_provider("sina", lambda: session.get(url, params=params, headers=headers, timeout=10))
        resp.encoding = 'utf-8'

        # The API returns JSON text; if blocked, text may start with '<'
        text = resp.text.strip()
        if not text or text.startswith('<'):
            raise ValueError("Sina JSON blocked or empty")
        data = json.loads(text)
        if not data:
            return None

        df = pd.DataFrame(data)
        rename_map = {
            'symbol': 'code',
            'name': 'name',
            'trade': 'current',
            'changepercent': 'change_percent',
            'settlement': 'prev_close',
            'high': 'high',
            'amount': 'amount'
        }
        df = df.rename(columns=rename_map)

        # Normalize fields
        for col in ['current', 'change_percent', 'prev_close', 'high', 'amount']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        # Add placeholders to align schema
        for col in ['speed', 'turnover', 'circ_mv']:
            df[col] = 0.0

        return df

    def _fetch_tushare_spot(self):
        """Fallback using Tushare daily data. Requires env TUSHARE_TOKEN and tushare installed."""
        try:
            import tushare as ts
        except Exception:
            return None
        token = os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            return None
        ts.set_token(token)
        pro = ts.pro_api(token)
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        try:
            df = self._call_provider("tushare", lambda: pro.daily(trade_date=today))
            if df is None or df.empty:
                return None
            df = df.rename(columns={
                'ts_code': 'code',
                'close': 'current',
                'pct_chg': 'change_percent',
                'pre_close': 'prev_close',
                'high': 'high',
                'amount': 'amount'
            })
            for col in ['current', 'change_percent', 'prev_close', 'high', 'amount']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            # Normalize code to sh/sz
            def _fmt(code):
                if code.endswith('.SH'):
                    return 'sh' + code.split('.')[0]
                if code.endswith('.SZ'):
                    return 'sz' + code.split('.')[0]
                return code
            df['code'] = df['code'].apply(_fmt)
            for col in ['speed', 'turnover', 'circ_mv']:
                df[col] = 0.0
            return df
        except Exception as e:
            self.log(f"[!] Tushare閿欒: {e}")
            return None

# Global instance
data_provider = DataProvider()






