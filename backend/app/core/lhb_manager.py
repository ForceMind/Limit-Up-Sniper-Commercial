import akshare as ak
import pandas as pd
import os
import json
import time
import random
import threading
from datetime import datetime, timedelta
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
KLINE_DIR = DATA_DIR / "kline_cache"
LHB_FILE = DATA_DIR / "lhb_history.csv"
SEATS_FILE = DATA_DIR / "vip_seats.json"

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
KLINE_DIR.mkdir(exist_ok=True)

from app.core.profile_builder import build_profiles

class LHBManager:
    def __init__(self):
        self.config = {
            "enabled": False,
            "days": 2,
            "min_amount": 10000000, # 1000万
            "last_update": None
        }
        self.is_syncing = False
        self.hot_money_map = {}
        self.vip_seats = set()
        self._kline_last_fetch_ts = {}
        self._kline_last_attempt_ts = {}
        self._kline_error_window_start = 0.0
        self._kline_error_window_count = 0
        self._kline_error_suppressed = 0
        self._kline_error_window_seconds = 60
        self._kline_error_max_logs = 8
        self._kline_pause_until_ts = 0.0
        self._kline_pause_reason = ""
        self._kline_pause_log_ts = 0.0
        self._kline_fail_window_start = 0.0
        self._kline_fail_window_count = 0
        self._kline_fail_window_seconds = 60
        self._kline_fail_trigger_count = 3
        self._kline_pause_seconds = 1800
        self._kline_pause_seconds_hard = 3600
        self._akshare_request_lock = threading.Lock()
        self._akshare_last_request_ts = 0.0
        self._akshare_min_interval_sec = 1.2
        self.load_config()
        self.load_hot_money_map()
        self.load_vip_seats()

    def _throttle_akshare_request(self, min_interval_sec=None):
        interval = self._akshare_min_interval_sec if min_interval_sec is None else max(0.0, float(min_interval_sec))
        with self._akshare_request_lock:
            now_ts = time.time()
            wait_s = interval - (now_ts - self._akshare_last_request_ts)
            if wait_s > 0:
                time.sleep(wait_s)
            self._akshare_last_request_ts = time.time()

    def load_hot_money_map(self):
        map_path = DATA_DIR / "seat_mappings.json"
        if map_path.exists():
            try:
                # Store mtime to avoid redundant loads
                mtime = map_path.stat().st_mtime
                if hasattr(self, '_map_mtime') and self._map_mtime == mtime:
                    return
                
                with open(map_path, 'r', encoding='utf-8') as f:
                    self.hot_money_map = json.load(f)
                    self._map_mtime = mtime
                    # print(f"[龙虎榜] Hot money map loaded ({len(self.hot_money_map)} entries)")
            except Exception as e:
                print(f"[龙虎榜] 加载游资映射失败: {e}")

    def load_vip_seats(self):
        if not SEATS_FILE.exists():
            self.vip_seats = set()
            return
        try:
            mtime = SEATS_FILE.stat().st_mtime
            if hasattr(self, "_vip_mtime") and self._vip_mtime == mtime:
                return
            with open(SEATS_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                self.vip_seats = {str(x).strip() for x in loaded if str(x).strip()}
            else:
                self.vip_seats = set()
            self._vip_mtime = mtime
        except Exception:
            self.vip_seats = set()

    def find_hot_money_name(self, seat_name):
        """逻辑统一：查找席位对应的游资名称"""
        if not seat_name: return ""
        seat_name = seat_name.strip() # 去除前后空格
        
        # 1. 直接匹配
        name = self.hot_money_map.get(seat_name)
        if name: return name
        
        # 2. 模糊匹配 (关键字) - 增加长度保护，防止匹配单个字
        for k, v in self.hot_money_map.items():
            k_clean = k.strip()
            if len(k_clean) > 1 and k_clean in seat_name:
                return v
        return ""

    def load_config(self):
        config_path = DATA_DIR / "lhb_config.json"
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                    self.config.update(saved)
                    print(f"[龙虎榜] 配置已加载: {self.config}")
            except Exception as e:
                print(f"[龙虎榜] 加载配置失败: {e}")

    def save_config(self):
        config_path = DATA_DIR / "lhb_config.json"
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=2)

    def update_settings(self, enabled, days, min_amount):
        print(f"[龙虎榜] 正在更新设置: enabled={enabled}, days={days}, min_amount={min_amount}")
        self.config['enabled'] = enabled
        self.config['days'] = days
        self.config['min_amount'] = min_amount
        self.save_config()
        
        if enabled:
            # Check if we need to backfill data
            # For simplicity, we trigger a sync if enabled
            # In a real app, we might want to do this asynchronously
            pass

    def has_data_for_today(self):
        """Check if today's data already exists in the local file"""
        if not LHB_FILE.exists():
            return False
        try:
            df = pd.read_csv(LHB_FILE)
            today = datetime.now().strftime('%Y-%m-%d')
            # Check for stock record existence (trade_date column)
            return today in df['trade_date'].astype(str).values
        except:
            return False

    def get_existing_dates(self):
        if not LHB_FILE.exists():
            return []
        try:
            df = pd.read_csv(LHB_FILE, dtype={'trade_date': str})
            return sorted(df['trade_date'].astype(str).unique().tolist(), reverse=True)
        except Exception:
            return []

    def get_trade_dates_between(self, start_date: str, end_date: str):
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
        except Exception:
            return []

        if start_dt > end_dt:
            start_dt, end_dt = end_dt, start_dt

        try:
            self._throttle_akshare_request()
            hist_df = ak.tool_trade_date_hist_sina()
            dates = hist_df['trade_date'].tolist()
            in_range = [d for d in dates if start_dt <= d <= end_dt]
            return [d.strftime('%Y-%m-%d') for d in in_range]
        except Exception:
            return []

    def get_missing_dates(self, start_date: str, end_date: str):
        target_dates = self.get_trade_dates_between(start_date, end_date)
        if not target_dates:
            return []
        existing = set(self.get_existing_dates())
        return [d for d in target_dates if d not in existing]

    def get_summary(self, start_date: str = None, end_date: str = None):
        existing_dates = self.get_existing_dates()
        total_records = 0
        if LHB_FILE.exists():
            try:
                df = pd.read_csv(LHB_FILE)
                total_records = int(len(df))
            except Exception:
                total_records = 0

        missing_dates = []
        if start_date and end_date:
            missing_dates = self.get_missing_dates(start_date, end_date)

        return {
            "is_syncing": bool(self.is_syncing),
            "enabled": bool(self.config.get("enabled")),
            "days": int(self.config.get("days", 0) or 0),
            "min_amount": int(self.config.get("min_amount", 0) or 0),
            "last_update": self.config.get("last_update"),
            "total_records": total_records,
            "available_dates": existing_dates,
            "available_date_count": len(existing_dates),
            "latest_date": existing_dates[0] if existing_dates else None,
            "missing_dates": missing_dates,
            "missing_count": len(missing_dates),
        }

    def fetch_and_update_data(self, logger=None, force_days=None, force_dates=None):
        def log(msg):
            if logger: logger(msg)
            print(msg)

        if self.is_syncing:
            log("[龙虎榜] 同步任务正在进行中，请勿重复操作。")
            return

        # Always reload config before sync to ensure we have latest settings (e.g. from other workers)
        self.load_config()

        if not self.config['enabled'] and not force_dates:
            log("[龙虎榜] 龙虎榜功能未开启，跳过更新。")
            return

        self.is_syncing = True
        try:
            days = force_days if force_days is not None else self.config['days']
            min_amount = self.config['min_amount']
            forced_trade_dates = []
            if force_dates:
                for item in force_dates:
                    try:
                        forced_trade_dates.append(datetime.strptime(str(item), '%Y-%m-%d').date())
                    except Exception:
                        continue
                forced_trade_dates = sorted(set(forced_trade_dates))
            
            if force_dates:
                trade_dates = forced_trade_dates
                log(f"[龙虎榜] 使用范围内缺失日期补齐，同步 {len(trade_dates)} 个交易日。")
                if not trade_dates:
                    log("[龙虎榜] 范围内无有效交易日，跳过同步。")
                    return
            else:
                desc = "最近1个" if days == 1 else f"最近 {days} 个"
                log(f"[龙虎榜] 开始同步{desc}交易日的龙虎榜数据...")

                # 1. Get Trading Dates
                end_date = datetime.now()
                start_date = end_date - timedelta(days=days * 1.5 + 20) # Increase buffer
                
                try:
                    self._throttle_akshare_request()
                    tool_trade_date_hist_sina_df = ak.tool_trade_date_hist_sina()
                    trade_dates = tool_trade_date_hist_sina_df['trade_date'].tolist()
                    # Filter dates
                    trade_dates = [d for d in trade_dates if d >= start_date.date() and d <= end_date.date()]
                    trade_dates = trade_dates[-days:] # Take last N trading days
                except Exception as e:
                    log(f"[龙虎榜] 获取交易日历失败: {e}")
                    return

            # 2. Load existing data
            existing_df = pd.DataFrame()
            if LHB_FILE.exists():
                try:
                    existing_df = pd.read_csv(LHB_FILE, dtype={'stock_code': str, 'trade_date': str})
                    # Ensure consistent string type for key columns
                    existing_df['stock_code'] = existing_df['stock_code'].astype(str)
                    existing_df['trade_date'] = existing_df['trade_date'].astype(str)
                except:
                    pass

            # 3. Iterate dates and fetch LHB
            for date_obj in trade_dates:
                # Always start with empty records for the new date
                new_records = []
                date_str = date_obj.strftime('%Y%m%d')
                date_iso = date_obj.strftime('%Y-%m-%d')
                
                # Check if it is today and before 16:00 (LHB usually starts after 16:00)
                now = datetime.now()
                # date_obj is likely datetime.date, so compare directly or use date_obj if it is date
                check_date = date_obj.date() if hasattr(date_obj, 'date') else date_obj
                
                if check_date == now.date() and now.hour < 16:
                     log(f"[龙虎榜] 今日({date_iso})数据尚未公布(16:00后)，跳过。")
                     continue
                
                # Check if we already have data for this date (Optimization)
                # But user said "if manual range covers missing data, fetch it"
                # So we should check if this date exists in our CSV
                if not existing_df.empty:
                    # Robust check: convert both to string YYYY-MM-DD
                    # existing_df['trade_date'] contains date objects
                    existing_dates = set()
                    for d in existing_df['trade_date'].tolist():
                         if hasattr(d, 'strftime'):
                             existing_dates.add(d.strftime('%Y-%m-%d'))
                         else:
                             existing_dates.add(str(d))
                    
                    if date_iso in existing_dates:
                        # If it's today, we might want to re-fetch to get latest data
                        if date_iso != now.strftime('%Y-%m-%d'):
                            continue

                log(f"[龙虎榜] 正在抓取 {date_iso} 龙虎榜数据...")
                
                try:
                    # akshare: stock_lhb_detail_em (东方财富)
                    self._throttle_akshare_request()
                    lhb_df = ak.stock_lhb_detail_em(start_date=date_str, end_date=date_str)
                    if lhb_df is None or lhb_df.empty:
                        log(f"  - akshare returned empty for {date_str}")
                        continue
                    
                    # Filter by buy amount
                    potential_stocks = lhb_df[lhb_df['龙虎榜买入额'] > min_amount]
                    log(f"  - 发现 {len(potential_stocks)} 只符合金额条件的个股")
                    
                    for _, row in potential_stocks.iterrows():
                        stock_code = str(row['代码']).zfill(6)
                        stock_name = row['名称']
                        
                        # [Modified] Filter: Only keep Main Board (00/60) and ChiNext (30)
                        # Skip STAR (68) and BSE (8/4/9)
                        if stock_code.startswith('68') or \
                           stock_code.startswith('8') or \
                           stock_code.startswith('4') or \
                           stock_code.startswith('9'):
                            # if logger: logger(f"  - 跳过非主板/创业板: {stock_name}({stock_code})")
                            continue

                        # Fetch detail for this stock
                        try:
                            # stock_lhb_stock_detail_em (Get details for specific date)
                            # flag="买入" to get buy seats. We can also get "卖出" if needed.
                            self._throttle_akshare_request()
                            detail_df = ak.stock_lhb_stock_detail_em(symbol=stock_code, date=date_str, flag="买入")
                            if detail_df is None or detail_df.empty:
                                time.sleep(0.2)
                                continue
                            
                            # [Fix] Handle column names mismatch (akshare update or encoding issue)
                            # Assuming standard structure: Index, Name, Buy, Buy%, Sell, Sell%, Net...
                            if len(detail_df.columns) >= 5:
                                # Rename critical columns by index
                                # 1: Seat Name, 2: Buy Amount, 4: Sell Amount
                                # Use rename to avoid SettingWithCopy warning on values
                                new_cols = list(detail_df.columns)
                                new_cols[1] = '营业部名称'
                                new_cols[2] = '买入金额'
                                new_cols[4] = '卖出金额'
                                detail_df.columns = new_cols

                            # Filter seats
                            # Columns: 序号, 营业部名称, 买入金额, 卖出金额, 净买入金额...
                            # Use a lower threshold for individual seats (e.g. 10M) to capture more Hot Money
                            seat_min_amount = 10000000 
                            buy_seats = detail_df[detail_df['买入金额'] > seat_min_amount]
                            
                            if not buy_seats.empty:
                                display_names = []
                                for s in buy_seats['营业部名称'].tolist():
                                    # Check for Hot Money match
                                    hm_name = self.hot_money_map.get(s)
                                    if not hm_name:
                                        # Try partial match
                                        for k, v in self.hot_money_map.items():
                                            if k in s:
                                                hm_name = v
                                                break
                                    
                                    if hm_name:
                                        display_names.append(f"{hm_name}")
                                    else:
                                        display_names.append(s.replace('证券股份有限公司', '').replace('有限责任公司', ''))
                                        
                                if logger: logger(f"  + {stock_name}({stock_code}): 发现 {len(buy_seats)} 个席位 {display_names}")
                            
                            for _, seat in buy_seats.iterrows():
                                seat_name = seat['营业部名称']
                                hm_name = self.hot_money_map.get(seat_name)
                                if not hm_name:
                                    for k, v in self.hot_money_map.items():
                                        if k in seat_name:
                                            hm_name = v
                                            break
                                            
                                new_records.append({
                                    'trade_date': date_iso,
                                    'stock_code': stock_code,
                                    'stock_name': stock_name,
                                    'buyer_seat_name': seat_name,
                                    'buy_amount': seat['买入金额'],
                                    'sell_amount': seat['卖出金额'],
                                    'hot_money': hm_name if hm_name else ''
                                })
                                
                        except Exception as e:
                            if logger: logger(f"  !获取股票详情失败 {stock_code}: {e}")
                            # pass
                        
                        # Sleep between stocks to avoid ban
                        time.sleep(random.uniform(0.3, 0.8))
                            
                    # Sleep between dates
                    time.sleep(random.uniform(0.5, 1.5))
                    
                    # Save incrementally after each date
                    if new_records:
                        try:
                            # Convert new records to DF
                            new_df = pd.DataFrame(new_records)
                            
                            if not existing_df.empty:
                                existing_df['trade_date'] = existing_df['trade_date'].astype(str)
                                combined_df = pd.concat([existing_df, new_df])
                                combined_df = combined_df.drop_duplicates(subset=['trade_date', 'stock_code', 'buyer_seat_name'])
                            else:
                                combined_df = new_df
                            
                            # Sort
                            combined_df = combined_df.sort_values('trade_date', ascending=False)
                            
                            # Save to disk
                            combined_df.to_csv(LHB_FILE, index=False)
                            if logger: logger(f"[龙虎榜] 已保存 {date_iso} 数据 (累计 {len(combined_df)} 条)")
                            
                            # Update in-memory existing_df for next iteration
                            existing_df = combined_df
                            # Clear new_records to avoid re-adding them
                            new_records = []
                            
                            # Generate Report for this date
                            self.generate_daily_report(date_iso, logger)
                            
                        except Exception as save_err:
                            if logger: logger(f"[龙虎榜] 保存数据失败 {date_iso}: {save_err}")

                except Exception as e:
                    if logger: logger(f"[龙虎榜]获取 {date_str} 数据失败: {e}")

            # 4. Final Cleanup and K-line Download
            if not existing_df.empty:
                # Cleanup > 180 days
                cutoff_date = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
                existing_df = existing_df[existing_df['trade_date'] >= cutoff_date]
                existing_df.to_csv(LHB_FILE, index=False)
                
                # Update VIP Seats
                self.update_vip_seats(existing_df)
                
                # Trigger K-line download (idempotent check)
                # [Auto-Trigger] Build Profiles
                try:
                    build_profiles(logger)
                except Exception as e:
                    if logger: logger(f"[龙虎榜] 更新画像失败: {e}")
                
                self.download_kline_data(existing_df, logger)
                
            else:
                if logger: logger("[龙虎榜] 无数据。")
        finally:
            self.is_syncing = False

    def update_vip_seats(self, df):
        # Count appearances in recent data, but keep manually maintained base seats.
        seat_counts = df['buyer_seat_name'].value_counts()
        auto_vip = [str(x).strip() for x in seat_counts[seat_counts >= 3].index.tolist() if str(x).strip()]

        manual_vip = []
        if SEATS_FILE.exists():
            try:
                with open(SEATS_FILE, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    manual_vip = [str(x).strip() for x in loaded if str(x).strip()]
            except Exception:
                manual_vip = []

        vip_seats = sorted(set(manual_vip + auto_vip))
        with open(SEATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(vip_seats, f, ensure_ascii=False, indent=2)
        self.load_vip_seats()

    def download_kline_data(self, df, logger=None):
        """
        Download 1-min K-line data for the specific date and stock
        """
        if df.empty: return
        
        # Unique stock-date combinations
        tasks = df[['stock_code', 'trade_date']].drop_duplicates()
        
        if logger: logger(f"[龙虎榜] 正在同步 {len(tasks)} 个 K线数据文件...")
        
        for _, row in tasks.iterrows():
            code = str(row['stock_code']).zfill(6)
            date_str = str(row['trade_date']) # YYYY-MM-DD
            
            file_name = f"{code}_{date_str}.csv"
            file_path = KLINE_DIR / file_name
            
            if file_path.exists(): continue
            
            try:
                # akshare: stock_zh_a_minute
                # Note: fetching historical 1-min data is tricky with free APIs.
                # akshare's `stock_zh_a_hist_min_em` usually supports recent data.
                # For 90 days ago, it might be hard.
                # Let's try `stock_zh_a_hist_min_em` with adjust='qfq'
                
                # Convert date to format needed? API usually takes start_date
                # But min data API usually just gives "recent N points" or specific range.
                # stock_zh_a_hist_min_em(symbol="000001", start_date="...", end_date="...", period="1", adjust="qfq")
                
                start_dt = date_str + " 09:00:00"
                end_dt = date_str + " 15:00:00"
                
                self._throttle_akshare_request()
                kline = ak.stock_zh_a_hist_min_em(symbol=code, start_date=start_dt, end_date=end_dt, period="1", adjust="qfq")
                
                if kline is not None and not kline.empty:
                    kline.to_csv(file_path, index=False)
                    
                time.sleep(0.2)
            except Exception as e:
                # if logger: logger(f"Failed K-line {code} {date_str}: {e}")
                pass

    def _log_kline_fetch_error(self, code, err):
        now_ts = time.time()
        if now_ts - self._kline_error_window_start >= self._kline_error_window_seconds:
            if self._kline_error_suppressed > 0:
                print(f"[龙虎榜] 已抑制 {self._kline_error_suppressed} 条分时K线抓取错误日志")
            self._kline_error_window_start = now_ts
            self._kline_error_window_count = 0
            self._kline_error_suppressed = 0

        if self._kline_error_window_count < self._kline_error_max_logs:
            self._kline_error_window_count += 1
            print(f"[龙虎榜] 分时K线抓取失败 {code}: {err}")
        else:
            self._kline_error_suppressed += 1

    def _is_hard_network_error(self, err) -> bool:
        text = str(err or "").lower()
        keywords = [
            "remotedisconnected",
            "connection aborted",
            "connection reset",
            "timed out",
            "read timed out",
            "too many requests",
            "forbidden",
            "blocked",
        ]
        return any(k in text for k in keywords)

    def _activate_kline_pause(self, seconds: int, reason: str = ""):
        now_ts = time.time()
        pause_s = max(60, int(seconds or 0))
        until_ts = now_ts + pause_s
        if until_ts > self._kline_pause_until_ts:
            self._kline_pause_until_ts = until_ts
            self._kline_pause_reason = str(reason or "").strip()
        # Reduce duplicate "paused" logs to at most once per minute.
        if now_ts - self._kline_pause_log_ts >= 60:
            remain = int(max(0, self._kline_pause_until_ts - now_ts))
            msg = f"[龙虎榜] 检测到上游异常，暂停K线网络抓取 {remain}s"
            if self._kline_pause_reason:
                msg += f"（原因: {self._kline_pause_reason}）"
            print(msg)
            self._kline_pause_log_ts = now_ts

    def _register_kline_network_error(self, err):
        now_ts = time.time()
        if self._is_hard_network_error(err):
            self._activate_kline_pause(self._kline_pause_seconds_hard, "连接被远端中断/疑似反爬")
            return

        if now_ts - self._kline_fail_window_start >= self._kline_fail_window_seconds:
            self._kline_fail_window_start = now_ts
            self._kline_fail_window_count = 0
        self._kline_fail_window_count += 1
        if self._kline_fail_window_count >= self._kline_fail_trigger_count:
            self._activate_kline_pause(self._kline_pause_seconds, "短时失败过多")
            self._kline_fail_window_count = 0
            self._kline_fail_window_start = now_ts

    def register_external_kline_failure(self, err):
        self._register_kline_network_error(err)

    def is_kline_network_paused(self) -> bool:
        now_ts = time.time()
        return now_ts < self._kline_pause_until_ts

    def get_kline_pause_remaining_seconds(self) -> int:
        now_ts = time.time()
        return int(max(0, self._kline_pause_until_ts - now_ts))

    def get_kline_1min(self, code, date_str, min_refresh_seconds=600, allow_network=True):
        file_path = KLINE_DIR / f"{code}_{date_str}.csv"
        
        # If it's today, we always fetch fresh data to ensure we have the latest minutes
        is_today = date_str == datetime.now().strftime('%Y-%m-%d')
        cache_key = f"{''.join(filter(str.isdigit, str(code)))}_{date_str}"
        now_ts = time.time()
        cached_df = None
        
        if file_path.exists() and not is_today:
            return pd.read_csv(file_path)
        if file_path.exists() and is_today:
            try:
                cached_df = pd.read_csv(file_path)
            except Exception:
                cached_df = None

        if not allow_network:
            return cached_df
        if self.is_kline_network_paused():
            now_ts = time.time()
            if now_ts - self._kline_pause_log_ts >= 60:
                remain = self.get_kline_pause_remaining_seconds()
                print(f"[龙虎榜] K线网络抓取暂停中，剩余 {remain}s")
                self._kline_pause_log_ts = now_ts
            return cached_df

        interval = max(0, int(min_refresh_seconds or 0))
        if is_today and cached_df is not None and not cached_df.empty:
            last_fetch_ts = self._kline_last_fetch_ts.get(cache_key, 0)
            if now_ts - last_fetch_ts < interval:
                return cached_df

        last_attempt_ts = self._kline_last_attempt_ts.get(cache_key, 0)
        if now_ts - last_attempt_ts < interval:
            if cached_df is not None and not cached_df.empty:
                return cached_df
            return None
             
        # Try to fetch if missing or if it's today
        try:
            self._kline_last_attempt_ts[cache_key] = now_ts
            # Format date for akshare
            start_point = datetime.strptime(date_str + " 09:00:00", "%Y-%m-%d %H:%M:%S")
            end_point = datetime.strptime(date_str + " 15:00:00", "%Y-%m-%d %H:%M:%S")
            if is_today:
                now = datetime.now()
                market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
                end_point = min(now, market_close)
                if end_point < start_point:
                    end_point = start_point
            start_dt = start_point.strftime("%Y-%m-%d %H:%M:%S")
            end_dt = end_point.strftime("%Y-%m-%d %H:%M:%S")
            
            # Remove non-digits from code just in case
            clean_code = "".join(filter(str.isdigit, str(code)))
            
            self._throttle_akshare_request()
            kline = ak.stock_zh_a_hist_min_em(symbol=clean_code, start_date=start_dt, end_date=end_dt, period="1", adjust="qfq")
            
            if kline is not None and not kline.empty:
                kline.to_csv(file_path, index=False)
                self._kline_last_fetch_ts[cache_key] = now_ts
                self._kline_fail_window_count = 0
                self._kline_fail_window_start = now_ts
                return kline
        except Exception as e:
            self._log_kline_fetch_error(code, e)
            self._register_kline_network_error(e)

        # Today's fetch may temporarily fail; keep using the last cached file.
        if cached_df is not None and not cached_df.empty:
            return cached_df
        return None

    def get_latest_lhb_info(self, code):
        """
        Get the latest LHB info for a stock, including Hot Money names.
        Returns a string summary or None.
        """
        if not LHB_FILE.exists():
            return None
            
        try:
            df = pd.read_csv(LHB_FILE, dtype={'stock_code': str, 'trade_date': str})
            df['stock_code'] = df['stock_code'].astype(str)
            
            # Filter by code
            stock_lhb = df[df['stock_code'] == str(code)]
            if stock_lhb.empty:
                return None
                
            # Get latest date
            latest_date = stock_lhb['trade_date'].max()
            latest_data = stock_lhb[stock_lhb['trade_date'] == latest_date]
            
            seats = []
            for _, row in latest_data.iterrows():
                seat = row['buyer_seat_name']
                hot_money = row.get('hot_money', '')
                if pd.notna(hot_money) and hot_money:
                    seats.append(f"{hot_money}({seat})")
                else:
                    seats.append(seat)
            
            return {
                "date": latest_date,
                "seats": seats
            }
        except Exception as e:
            print(f"[龙虎榜] 获取龙虎榜信息失败: {e}")
            return None

    def get_daily_data(self, date_str):
        """
        Get all LHB records for a specific date.
        """
        if not LHB_FILE.exists():
            return []
            
        try:
            # 实时重载席位表，确保修改立即生效
            self.load_hot_money_map()
            self.load_vip_seats()
            
            df = pd.read_csv(LHB_FILE, dtype={'stock_code': str, 'trade_date': str})
            # Ensure trade_date is string
            df['trade_date'] = df['trade_date'].astype(str)
            df_day = df[df['trade_date'] == date_str].copy()
            
            if df_day.empty:
                return []
                
            # Handle numeric columns - fill NaN with 0 for calculation safety
            numeric_cols = ['buy_amount', 'sell_amount']
            for col in numeric_cols:
                if col in df_day.columns:
                    df_day[col] = df_day[col].fillna(0)
            
            # Handle string columns - fill NaN with empty string
            str_cols = ['hot_money', 'buyer_seat_name', 'stock_name', 'stock_code']
            for col in str_cols:
                if col in df_day.columns:
                    df_day[col] = df_day[col].fillna("")
            
            # Convert to list of dicts
            records = df_day.to_dict('records')
            
            # Group by stock
            grouped = {}
            for row in records:
                code = str(row['stock_code'])
                if code not in grouped:
                    grouped[code] = {
                        "code": code,
                        "name": row['stock_name'],
                        "seats": []
                    }
                
                seat_name_stripped = str(row['buyer_seat_name']).strip()
                # 优先使用实时映射表，如果没匹配到再使用 CSV 里的旧值
                dynamic_hot_money = self.find_hot_money_name(seat_name_stripped)
                
                grouped[code]['seats'].append({
                    "name": seat_name_stripped,
                    "buy": row['buy_amount'],
                    "sell": row['sell_amount'],
                    "hot_money": dynamic_hot_money or (row['hot_money'] if pd.notna(row['hot_money']) else ""),
                    "is_vip": seat_name_stripped in self.vip_seats
                })
            
            # Convert grouped dict to list
            result = list(grouped.values())
            
            # Sort seats by buy amount desc for each stock
            for stock in result:
                stock['seats'].sort(key=lambda x: x['buy'], reverse=True)
                
                # Calculate total net buy for sorting stocks
                total_net = sum(s['buy'] - s['sell'] for s in stock['seats'])
                stock['total_net_buy'] = total_net
                
            # Sort stocks by total net buy
            result.sort(key=lambda x: x['total_net_buy'], reverse=True)
            
            return result
            
        except Exception as e:
            print(f"[龙虎榜] 获取每日数据失败: {e}")
            return []

    def get_available_dates(self):
        """Get list of dates that have data"""
        if not LHB_FILE.exists(): return []
        try:
            df = pd.read_csv(LHB_FILE)
            return sorted(df['trade_date'].astype(str).unique().tolist(), reverse=True)
        except:
            return []

    def generate_daily_report(self, date_str, logger=None):
        """
        Generate a summary report for a specific date.
        """
        if not LHB_FILE.exists(): return
        
        try:
            df = pd.read_csv(LHB_FILE)
            # Ensure date format matches
            # date_str is usually YYYY-MM-DD
            df_day = df[df['trade_date'] == date_str]
            
            if df_day.empty:
                if logger: logger(f"[LHB Report] {date_str} 无数据。")
                return

            # 1. Top Hot Money
            hot_money_counts = df_day[df_day['hot_money'].notna() & (df_day['hot_money'] != '')]['hot_money'].value_counts()
            
            report = [f"\n=== 龙虎榜日报 {date_str} ==="]
            if not hot_money_counts.empty:
                report.append("【活跃游资】")
                for name, count in hot_money_counts.head(10).items():
                    # Get stocks they bought
                    stocks = df_day[df_day['hot_money'] == name]['stock_name'].unique().tolist()
                    report.append(f"  - {name}: 出手 {count} 次 ({', '.join(stocks)})")
            
            # 2. Top Stocks by Buy Amount (approx sum of seats)
            stock_buys = df_day.groupby('stock_name')['buy_amount'].sum().sort_values(ascending=False)
            report.append("\n【大额榜单】")
            for name, amount in stock_buys.head(5).items():
                report.append(f"  - {name}: 席位合计买入 {amount/100000000:.2f} 亿")
                
            report_str = "\n".join(report)
            if logger: 
                logger(report_str)
            else:
                print(report_str)
                
        except Exception as e:
            print(f"[龙虎榜] 生成报告失败: {e}")

lhb_manager = LHBManager()
