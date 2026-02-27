import akshare as ak
import requests
import pandas as pd
import time
import json
import threading
from datetime import datetime

class DataProvider:
    def __init__(self, logger=None):
        self.logger = logger
        self._last_market_df = None
        self._last_market_ts = 0
        self._last_failure_ts = 0
        self._eastmoney_next_retry_ts = 0
        self._eastmoney_fail_cooldown_sec = 600
        self._eastmoney_last_error_log_ts = 0
        self._eastmoney_skip_log_ts = 0
        self._base_info_df = None
        self._base_info_ts = 0
        self._base_info_retry_ts = 0
        self._base_info_next_retry_ts = 0
        self._base_info_fail_cooldown_sec = 600
        self._base_info_last_error_log_ts = 0
        self._base_info_skip_scan_log_ts = 0
        self._base_info_lock = threading.Lock()
        self._lock = threading.Lock() # Global lock for heavy operations (market overview)

    def log(self, msg):
        if self.logger:
            self.logger(msg)
        else:
            print(msg)

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

    def fetch_stock_info(self, code):
        """
        Fetch basic info (Industry/Concept) for a single stock.
        """
        try:
            # Remove prefix for akshare
            clean_code = self._strip_code(code)
            df = ak.stock_individual_info_em(symbol=clean_code)
            if df is None or df.empty:
                return {}
            
            # df columns: item, value
            info = {}
            for _, row in df.iterrows():
                if row['item'] == '行业':
                    info['concept'] = row['value']
                elif row['item'] == '总市值':
                    info['total_mv'] = row['value']
                elif row['item'] == '流通市值':
                    info['circ_mv'] = row['value']
            
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

        # Use Sina directly as requested by user (EastMoney is unstable/forbidden for quotes)
        try:
            return self._fetch_quotes_sina(codes)
        except Exception as e:
            self.log(f"[!] 新浪行情获取失败: {e}")
            
        return []

    def fetch_intraday_data(self, code):
        """
        Fetch intraday 1-minute data for a single stock (Sina).
        Returns DataFrame with 'time', 'close', 'volume'.
        """
        full_code = self._format_code(code)
        url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={full_code}&scale=1&ma=no&datalen=240"
        
        try:
            with requests.Session() as session:
                session.trust_env = False
                resp = session.get(url, timeout=3)
            
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
            self.log(f"获取分时数据失败 {code}: {e}")
            return None

    def _fetch_quotes_sina(self, codes):
        # Ensure base info exists for circulation value / turnover calculation
        now_ts = time.time()
        need_refresh = (
            self._base_info_df is None
            or self._base_info_df.empty
            or (now_ts - self._base_info_ts > 3600)
        )
        if need_refresh and (now_ts - self._base_info_retry_ts > 300):
            self._base_info_retry_ts = now_ts
            try:
                self.update_base_info()
            except Exception as e:
                self.log(f"[!] fetch_quotes 内更新基础信息失败: {e}")

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
                    resp = session.get(url, headers=headers, timeout=5)
                
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
                self.log(f"[!] 批量抓取失败: {e}")
                continue
                
        return stocks

    def fetch_all_market_data(self):
        """
        Fetch ALL stocks for market overview and scanning.
        Returns DataFrame.
        """
        now_ts = time.time()
        
        # Throttle logic
        if self._last_market_df is not None and now_ts - self._last_market_ts < 300: # 5 minutes cache
            return self._last_market_df.copy()

        # Cooldown prevents hammering API on failures
        if now_ts - self._last_failure_ts < 60:
            if self._last_market_df is not None:
                return self._last_market_df.copy()
            # If no cache and in cooldown, return empty DF to avoid crash, or None
            return None

        # Lock to ensure only one thread updates data at a time
        with self._lock:
            # Doublets check inside lock
            if self._last_market_df is not None and time.time() - self._last_market_ts < 300:
                return self._last_market_df.copy()
                
            # Helper to temporarily unset proxy
            import os
            old_http = os.environ.get("HTTP_PROXY")
            old_https = os.environ.get("HTTPS_PROXY")
            if old_http: os.environ.pop("HTTP_PROXY", None)
            if old_https: os.environ.pop("HTTPS_PROXY", None)

            try:
                # 1. Try Sina Paged (Slow & Steady) - Primary as requested
                try:
                    self.log("[*] 正在抓取全市场数据（新浪分页）...")
                    df = self._fetch_sina_market_paged()
                    if df is not None and not df.empty:
                        self._last_market_df = df.copy()
                        self._last_market_ts = now_ts
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
                    # 2. Try AKShare (EastMoney) - Fallback
                    try:
                        self.log("[*] 正在抓取全市场数据（AKShare/东财）...")
                        df = ak.stock_zh_a_spot_em()
                        rename_map = {
                            '代码': 'code', '名称': 'name', '最新价': 'current', '涨跌幅': 'change_percent',
                            '涨速': 'speed', '换手率': 'turnover', '流通市值': 'circ_mv', '昨收': 'prev_close',
                            '最高': 'high', '最低': 'low', '今开': 'open', '成交额': 'amount'
                        }
                        df = df.rename(columns=rename_map)
                        # Ensure numeric
                        for col in ['current', 'change_percent', 'speed', 'turnover', 'circ_mv', 'prev_close', 'high']:
                            if col in df.columns:
                                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
                        
                        self._last_market_df = df.copy()
                        self._last_market_ts = now_ts
                        self._eastmoney_next_retry_ts = 0
                        self._eastmoney_last_error_log_ts = 0
                        return df
                    except Exception as e:
                        now_ts = time.time()
                        self._eastmoney_next_retry_ts = now_ts + self._eastmoney_fail_cooldown_sec
                        if now_ts - self._eastmoney_last_error_log_ts >= 60:
                            wait_s = int(self._eastmoney_fail_cooldown_sec)
                            self.log(f"[!] AKShare/东财抓取失败: {e}（进入通道冷却，{wait_s}s 后重试）")
                            self._eastmoney_last_error_log_ts = now_ts

                    # 3. Try Manual EM Paged (Backup)
                    try:
                        self.log("[*] 正在抓取全市场数据（东财手工分页）...")
                        df = self._fetch_em_market_paged()
                        if df is not None and not df.empty:
                            self._last_market_df = df.copy()
                            self._last_market_ts = now_ts
                            self._eastmoney_next_retry_ts = 0
                            self._eastmoney_last_error_log_ts = 0
                            return df
                    except Exception as e:
                        now_ts = time.time()
                        self._eastmoney_next_retry_ts = now_ts + self._eastmoney_fail_cooldown_sec
                        if now_ts - self._eastmoney_last_error_log_ts >= 60:
                            wait_s = int(self._eastmoney_fail_cooldown_sec)
                            self.log(f"[!] 东财手工分页抓取失败: {e}（进入通道冷却，{wait_s}s 后重试）")
                            self._eastmoney_last_error_log_ts = now_ts

                # 4. Try Tushare (Requires TUSHARE_TOKEN and package installed)
                try:
                    self.log("[*] 正在抓取全市场数据（Tushare）...")
                    df = self._fetch_tushare_spot()
                    if df is not None and not df.empty:
                        self._last_market_df = df.copy()
                        self._last_market_ts = now_ts
                        return df
                except Exception as e:
                    self.log(f"[!] Tushare抓取失败: {e}")
                    
                # Fallback: return last cache even if stale
                self._last_failure_ts = now_ts # Mark failure
                if self._last_market_df is not None:
                    return self._last_market_df.copy()
                return None
            finally:
                # Restore proxy settings
                if old_http: os.environ["HTTP_PROXY"] = old_http
                if old_https: os.environ["HTTPS_PROXY"] = old_https

    def fetch_limit_up_pool(self):
        """Fetch Limit Up Pool"""
        try:
            date_str = datetime.now().strftime("%Y%m%d")
            df = ak.stock_zt_pool_em(date=date_str)
            return df
        except Exception as e:
            self.log(f"[!] 涨停池抓取失败: {e}")
            return None

    def fetch_broken_limit_pool(self):
        """Fetch Broken Limit Pool"""
        try:
            date_str = datetime.now().strftime("%Y%m%d")
            df = ak.stock_zt_pool_zbgc_em(date=date_str)
            return df
        except Exception as e:
            self.log(f"[!] 炸板池抓取失败: {e}")
            return None

    def fetch_indices(self):
        """Fetch major indices"""
        try:
            url = "http://hq.sinajs.cn/list=sh000001,sz399001,sz399006"
            headers = {"Referer": "http://finance.sina.com.cn"}
            
            with requests.Session() as session:
                session.trust_env = False
                resp = session.get(url, headers=headers, timeout=5)
            
            indices = []
            indices_map = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}
            
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
            self.log(f"[!] 指数抓取失败: {e}")
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
            resp = requests.get(url, headers=headers, timeout=5)
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                return data
        except Exception as e:
            self.log(f"[!] 历史数据抓取失败 {code}: {e}")
        
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
            resp = requests.get(url, params=params, timeout=3)
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
            self.log(f"[!] 搜索失败: {e}")
            
        return []

    def get_stock_info(self, code):
        """
        Get basic stock info (name, concept) for adding to watchlist.
        """
        name = "未知股票"
        concept = "自选"
        
        try:
            # Construct EastMoney secid
            raw_code = self._strip_code(code)
            market = '1' if code.startswith('sh') else '0'
            secid = f"{market}.{raw_code}"
                
            em_url = f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f14,f127,f116"
            resp = requests.get(em_url, timeout=3)
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
        if now_ts < self._base_info_next_retry_ts:
            return

        with self._base_info_lock:
            now_ts = time.time()
            if now_ts < self._base_info_next_retry_ts:
                return
            try:
                self.log("[*] 正在从 AKShare 更新股票基础信息（用于流通市值与换手率计算）...")
                df = ak.stock_zh_a_spot_em()

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

                # 条数异常通常意味着上游接口返回结构变化或被限流，避免误判成功后触发全市场兜底扫描。
                if len(valid_rows) < 500:
                    raise ValueError(f"AKShare基础信息条数异常: {len(valid_rows)}")

                self._base_info_df = pd.DataFrame(valid_rows)
                self._base_info_ts = time.time()
                self._base_info_next_retry_ts = 0
                self._base_info_last_error_log_ts = 0
                self.log(f"[*] 股票基础信息更新完成，已加载 {len(self._base_info_df)} 只股票（用于行情补全）。")
            except Exception as e:
                now_ts = time.time()
                self._base_info_next_retry_ts = now_ts + self._base_info_fail_cooldown_sec
                if now_ts - self._base_info_last_error_log_ts >= 60:
                    wait_s = int(self._base_info_fail_cooldown_sec)
                    self.log(f"[!] 从 AKShare 更新基础信息失败: {e}（进入失败冷却，{wait_s}s 后重试）")
                    self._base_info_last_error_log_ts = now_ts

    def _fetch_sina_market_paged(self):
        """
        Fetch market data by iterating through candidate codes.
        Uses hq.sinajs.cn which is not blocked.
        """
        # 1. Ensure base info is available (refresh if older than 1 hour)
        now_ts = time.time()
        if self._base_info_df is None or now_ts - self._base_info_ts > 3600:
            self.update_base_info()
            now_ts = time.time()

        # 2. Prepare list
        if self._base_info_df is not None and not self._base_info_df.empty:
            candidates = self._base_info_df['code'].tolist()
            base_map = self._base_info_df.set_index('code').to_dict('index')
        else:
            if now_ts < self._base_info_next_retry_ts:
                # Base info failed and still in cooldown: avoid scanning huge synthetic universe.
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
        batch_size = 200 # Reduced as requested
        
        self.log(f"[*] 正在通过新浪扫描 {len(candidates)} 只股票（每批200）...")
        
        with requests.Session() as session:
            session.trust_env = False
            headers = {"Referer": "http://finance.sina.com.cn"}
            
            for i in range(0, len(candidates), batch_size):
                batch = candidates[i:i+batch_size]
                url = "http://hq.sinajs.cn/list=" + ",".join(batch)
                
                try:
                    resp = session.get(url, headers=headers, timeout=5)
                    resp.encoding = 'gbk'
                    
                    for line in resp.text.split('\n'):
                        if not line: continue
                        parts = line.split('=')
                        if len(parts) < 2: continue
                        
                        code = parts[0].split('_')[-1]
                        data_str = parts[1].strip('";')
                        if not data_str: continue # Invalid code
                        
                        data = data_str.split(',')
                        if len(data) < 30: continue
                        
                        name = data[0]
                        open_price = float(data[1])
                        prev_close = float(data[2])
                        current = float(data[3])
                        high = float(data[4])
                        low = float(data[5])
                        volume = float(data[8])
                        amount = float(data[9])
                        
                        # Filter out inactive stocks
                        if open_price == 0 and volume == 0:
                            continue 
                            
                        change_percent = 0.0
                        if prev_close > 0:
                            change_percent = ((current - prev_close) / prev_close) * 100
                        
                        # Merge with base info
                        base_data = base_map.get(code, {})
                        circ_shares = base_data.get('circ_shares', 0)
                        
                        # Calculate Turnover
                        turnover = 0.0
                        if circ_shares > 0:
                            turnover = (volume / circ_shares) * 100
                            
                        # Calculate CircMV (Realtime)
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
                            "circ_mv": circ_mv
                        })
                        
                    time.sleep(0.5) # Slow fetch as requested
                    
                except Exception as e:
                    self.log(f"[!] 批次 {i} 抓取失败: {e}")
                    time.sleep(1)
        
        if not valid_stocks:
            return None
            
        return pd.DataFrame(valid_stocks)

    def _fetch_em_market_paged(self):
        """
        Robust paged fetch for EastMoney.
        Fetches ~5500 stocks in pages of 500 to avoid timeouts/blocks.
        """
        all_data = []
        page = 1
        page_size = 500 # Safe size
        max_pages = 20 # Safety break
        
        # Standard headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "http://quote.eastmoney.com/",
            "Connection": "keep-alive"
        }
        
        # Use a session for keep-alive
        with requests.Session() as session:
            session.trust_env = False # No proxy
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
                        "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152"
                    }
                    
                    resp = session.get(url, params=params, headers=headers, timeout=5)
                    data = resp.json()
                    
                    if not data or 'data' not in data or 'diff' not in data['data']:
                        break # No more data or error
                        
                    rows = data['data']['diff']
                    if not rows:
                        break
                        
                    all_data.extend(rows)
                    consecutive_page_failures = 0
                    
                    # If we got fewer rows than page_size, we are done
                    if len(rows) < page_size:
                        break
                        
                    page += 1
                    time.sleep(0.3) # Sleep to be nice
                    
                except Exception as e:
                    self.log(f"[!] 东财第 {page} 页抓取失败: {e}")
                    consecutive_page_failures += 1
                    # Retry once
                    time.sleep(1)
                    try:
                        resp = session.get(url, params=params, headers=headers, timeout=5)
                        data = resp.json()
                        if data and 'data' in data and 'diff' in data['data']:
                            rows = data['data']['diff']
                            all_data.extend(rows)
                            consecutive_page_failures = 0
                            if len(rows) < page_size: break
                            page += 1
                            continue
                    except:
                        pass # Give up on this page
                    
                    if consecutive_page_failures >= 2:
                        self.log("[!] 东财分页连续失败，提前终止本轮抓取")
                        break
                    page += 1 # Move on
                    
        if not all_data:
            return None
            
        # Convert to DataFrame
        df = pd.DataFrame(all_data)
        rename_map = {
            'f12': 'code', 'f14': 'name', 'f2': 'current', 'f3': 'change_percent',
            'f8': 'turnover', 'f20': 'circ_mv', 'f18': 'prev_close',
            'f15': 'high', 'f16': 'low', 'f17': 'open', 'f6': 'amount'
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
            resp = session.get(url, params=params, headers=headers, timeout=10)
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
        today = datetime.now().strftime("%Y%m%d")
        try:
            df = pro.daily(trade_date=today)
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
            self.log(f"[!] Tushare错误: {e}")
            return None

# Global instance
data_provider = DataProvider()
