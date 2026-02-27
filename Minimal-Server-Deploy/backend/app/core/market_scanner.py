import pandas as pd
from datetime import datetime
from app.core.data_provider import data_provider
from app.core.seat_matcher import matcher

# 临时过滤北证股票 (8开头, 4开头, 92开头)
FILTER_BSE = False

def is_bse_stock(code):
    """检查是否为北证股票"""
    code = str(code)
    return code.startswith('8') or code.startswith('4') or code.startswith('92') or code.startswith('bj')

def is_20cm_stock(code):
    """检查是否为创业板(30)或科创板(68)"""
    code = str(code)
    return code.startswith('30') or code.startswith('68')

def scan_intraday_limit_up(logger=None):
    """
    扫描盘中即将涨停的股票
    """
    if logger: logger("[*] 开始扫描盘中异动股...")
    
    df = data_provider.fetch_all_market_data()
    
    if df is None or df.empty:
        if logger: logger("[!] 所有接口均失败，无法获取行情数据")
        return [], []
        
    intraday_stocks = []
    sealed_stocks = []
    
    # 1. 初步筛选 (使用批量数据)
    candidates = []
    
    try:
        for _, row in df.iterrows():
            try:
                code = str(row['code'])
                name = str(row['name'])
                current = float(row['current'])
                change_percent = float(row['change_percent'])
                prev_close = float(row['prev_close'])
                
                # Handle missing/zero values safely
                speed = round(float(row.get('speed', 0)), 2)
                turnover = round(float(row.get('turnover', 0)), 2)
                circ_mv = round(float(row.get('circ_mv', 0)), 2)
                volume = float(row.get('volume', 0))
                change_percent = round(change_percent, 2)
                current = round(current, 2)
                
                # 0. 过滤北证
                if FILTER_BSE and is_bse_stock(code): continue
                
                # 0.1 过滤非A股
                if not code.isdigit() or len(code) != 6: continue

                # 1. 动态涨幅过滤
                is_20cm = is_20cm_stock(code)
                limit_ratio = 1.2 if is_20cm else 1.1
                limit_up_price = round(prev_close * limit_ratio, 2)
                
                # Format code
                if is_bse_stock(code):
                    full_code = f"bj{code}"
                else:
                    full_code = f"sh{code}" if code.startswith('6') else f"sz{code}"

                # 初步筛选: 涨幅 > 5% (20cm > 15%)
                if is_20cm:
                    if change_percent < 15.0: continue
                else:
                    if change_percent < 5.0: continue
                
                # 涨速过滤 > 3%
                if speed < 3.0: continue
                    
                if 'ST' in name: continue
                
                candidates.append({
                    "code": full_code,
                    "name": name,
                    "current": current,
                    "change_percent": change_percent,
                    "speed": speed,
                    "turnover": turnover,
                    "circ_mv": circ_mv,
                    "volume": volume,
                    "limit_up_price": limit_up_price
                })
                    
            except Exception as inner_e:
                continue 
                
    except Exception as e:
        if logger: logger(f"[!] 处理行情数据失败: {e}")
        
    # 2. 二次确认 (使用 Sina 接口获取详细买卖盘，判断是否封板)
    if not candidates:
        return [], []
        
    candidate_codes = [c['code'] for c in candidates]
    # 批量获取详细行情
    detailed_quotes = data_provider.fetch_quotes(candidate_codes)
    detailed_map = {q['code']: q for q in detailed_quotes}
    
    for cand in candidates:
        full_code = cand['code']
        detail = detailed_map.get(full_code)
        
        is_sealed = False
        
        if detail:
            # 使用详细行情中的封板判断 (基于卖一量)
            if detail.get('is_limit_up', False):
                is_sealed = True
            # Double check with price
            elif cand['current'] >= cand['limit_up_price'] - 0.01:
                # 如果价格到了涨停价，且没有详细数据或者详细数据没说封板，
                # 但我们还是倾向于认为是封板，除非卖一量很大。
                # 但 fetch_quotes 已经做了 strict check (ask1_vol == 0)
                # 所以如果 fetch_quotes 返回 False，那就是没封住 (炸板或烂板)
                pass
        else:
            # Fallback to simple price check if detail fetch failed
            if cand['current'] >= cand['limit_up_price'] - 0.01:
                is_sealed = True
        
        if is_sealed:
            sealed_stocks.append({
                "code": full_code,
                "name": cand['name'],
                "current": cand['current'],
                "change_percent": cand['change_percent'],
                "time": "-", 
                "concept": "盘中涨停",
                "reason": "涨停",
                "strategy": "LimitUp",
                "circulation_value": cand['circ_mv'],
                "turnover": cand['turnover']
            })
        else:
            # 游资画像匹配
            likely_seats = []
            if cand['speed'] > 3.0:
                try:
                    # Fetch real intraday data for slope calculation
                    df_min = data_provider.fetch_intraday_data(full_code)
                    price_hist = []
                    avg_vol = 1
                    
                    if df_min is not None and not df_min.empty:
                        price_hist = df_min['close'].tolist()
                        avg_vol = df_min['volume'].mean()
                        
                    stock_data_for_matcher = {
                        'time': datetime.now(),
                        'price_history': price_hist, 
                        'volume': cand.get('volume', 0),
                        'avg_volume': avg_vol, 
                        'market_cap': cand.get('circ_mv', 0),
                        'limit_up_days': 1 
                    }
                    likely_seats = matcher.match(stock_data_for_matcher)
                except Exception as e:
                    if logger: logger(f"匹配错误: {e}")

            intraday_stocks.append({
                "code": full_code,
                "name": cand['name'],
                "concept": "盘中异动",
                "reason": f"盘中突击: 涨幅{cand['change_percent']}%, 涨速{cand['speed']}%",
                "score": 8.0 + (cand['speed'] * 0.5),
                "strategy": "LimitUp",
                "circulation_value": cand['circ_mv'],
                "turnover": cand['turnover'],
                "likely_seats": likely_seats
            })
            
            if logger and cand['speed'] > 1.0: 
                logger(f"    [+] 发现异动: {cand['name']} 涨幅:{cand['change_percent']}% 涨速:{cand['speed']}%")
        
    return intraday_stocks, sealed_stocks

def scan_limit_up_pool(logger=None):
    """
    扫描已涨停的股票
    """
    if logger: logger("[*] 正在扫描已涨停股票...")
    
    try:
        df = data_provider.fetch_limit_up_pool()
        
        if df is None or df.empty:
            if logger: logger("[!] 涨停池无数据")
            return []
            
        found_stocks = []
        
        for _, row in df.iterrows():
            code = str(row['代码'])
            name = str(row['名称'])
            change_percent = round(float(row['涨跌幅']), 2)
            current = round(float(row['最新价']), 2)
            turnover = round(float(row['换手率']), 2)
            circ_mv = round(float(row['流通市值']), 2) if '流通市值' in row else 0
            # Try to get volume/amount
            volume = float(row.get('成交量', 0))
            amount = float(row.get('成交额', 0))
            
            limit_days = int(row['连板数'])
            industry = str(row['所属行业'])
            first_seal_time = str(row['首次封板时间']) 
            
            if FILTER_BSE and is_bse_stock(code): continue

            formatted_time = first_seal_time
            if len(first_seal_time) == 6:
                formatted_time = f"{first_seal_time[:2]}:{first_seal_time[2:4]}:{first_seal_time[4:]}"
            
            if is_bse_stock(code):
                full_code = f"bj{code}"
            else:
                full_code = f"sh{code}" if code.startswith('6') else f"sz{code}"
            
            # Calculate likely seats
            likely_seats = []
            try:
                # Fetch real intraday data for slope calculation
                df_min = data_provider.fetch_intraday_data(full_code)
                price_hist = []
                avg_vol = 1
                
                if df_min is not None and not df_min.empty:
                    price_hist = df_min['close'].tolist()
                    avg_vol = df_min['volume'].mean()

                # Construct minimal data for matcher
                stock_data_for_matcher = {
                    'time': datetime.now(),
                    'price_history': price_hist, 
                    'volume': volume, 
                    'avg_volume': avg_vol,
                    'market_cap': circ_mv,
                    'limit_up_days': limit_days,
                    'amount': amount
                }
                likely_seats = matcher.match(stock_data_for_matcher)
            except Exception as e:
                if logger: logger(f"匹配席位失败 {name}: {e}")

            found_stocks.append({
                "code": full_code,
                "name": name,
                "current": current,
                "change_percent": change_percent,
                "time": formatted_time,
                "concept": industry,
                "associated": industry,
                "reason": f"{limit_days}连板" if limit_days > 1 else "首板",
                "strategy": "LimitUp",
                "circulation_value": circ_mv,
                "turnover": turnover,
                "limit_up_days": limit_days,
                "likely_seats": likely_seats
            })
            
        return found_stocks

    except Exception as e:
        if logger: logger(f"[!] 涨停池接口失败: {e}")
        return []

def scan_broken_limit_pool(logger=None):
    """
    扫描炸板股票
    """
    if logger: logger("[*] 正在扫描炸板股票...")
    
    found_stocks = []
    try:
        df = data_provider.fetch_broken_limit_pool()
        
        if df is None or df.empty:
            return []
            
        for _, row in df.iterrows():
            code = str(row['代码'])
            name = str(row['名称'])
            change_percent = round(float(row['涨跌幅']), 2)
            current = round(float(row['最新价']), 2)
            high = round(float(row['涨停价']), 2)
            turnover = round(float(row['换手率']), 2) if '换手率' in row else 0
            circ_mv = float(row['流通市值']) if '流通市值' in row else 0
            
            if FILTER_BSE and is_bse_stock(code): continue
            if 'ST' in name: continue
            
            if is_bse_stock(code):
                full_code = f"bj{code}"
            else:
                full_code = f"sh{code}" if code.startswith('6') else f"sz{code}"
            
            raw_time = str(row['首次封板时间'])
            formatted_time = raw_time
            if len(raw_time) == 6:
                formatted_time = f"{raw_time[:2]}:{raw_time[2:4]}:{raw_time[4:]}"
            elif len(raw_time) == 5: 
                formatted_time = f"0{raw_time[:1]}:{raw_time[1:3]}:{raw_time[3:]}"
            
            found_stocks.append({
                "code": full_code,
                "name": name,
                "current": current,
                "change_percent": change_percent,
                "time": formatted_time, 
                "high": high,
                "concept": str(row['所属行业']),
                "associated": str(row['所属行业']),
                "amplitude": round(float(row['振幅']), 2) if '振幅' in row else 0,
                "circulation_value": circ_mv,
                "turnover": turnover
            })
    except Exception as e:
        if logger: logger(f"[!] 扫描炸板池失败: {e}")
        
    return found_stocks

def scan_broken_limit_pool_fallback(logger=None):
    return scan_broken_limit_pool(logger)

def scan_limit_up_pool_fallback(logger=None):
    return scan_limit_up_pool(logger)

def get_market_overview(logger=None):
    """
    获取大盘情绪数据: 指数、成交量、涨跌家数、涨停炸板数
    """
    overview = {
        "indices": [],
        "stats": {
            "limit_up_count": 0,
            "limit_down_count": 0,
            "broken_count": 0,
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "total_volume": 0, # 亿
            "sentiment": "Neutral",
            "suggestion": "观察"
        }
    }
    
    # 1. 获取指数数据
    try:
        if logger: logger("[*] 获取大盘指数...")
        indices = data_provider.fetch_indices()
        overview["indices"] = indices
        
        # Calculate total volume (Shanghai + Shenzhen only, exclude ChiNext as it's in Shenzhen)
        total_vol = 0
        for idx in indices:
            if idx['name'] in ["上证指数", "深证成指"]:
                total_vol += idx['amount']
        
        overview["stats"]["total_volume"] = round(total_vol, 2)
        
    except Exception as e:
        if logger: logger(f"[!] 获取指数失败: {e}")

    # 2. 获取涨跌分布 & 情绪数据 (使用全市场数据计算，更稳定)
    try:
        df = data_provider.fetch_all_market_data()
        
        if df is not None and not df.empty:
            df['change_percent'] = pd.to_numeric(df['change_percent'], errors='coerce').fillna(0)
            
            up = len(df[df['change_percent'] > 0])
            down = len(df[df['change_percent'] < 0])
            flat = len(df[df['change_percent'] == 0])
            limit_down = len(df[df['change_percent'] < -9.5])
            
            overview["stats"]["up_count"] = up
            overview["stats"]["down_count"] = down
            overview["stats"]["flat_count"] = flat
            overview["stats"]["limit_down_count"] = limit_down

            # 计算涨停和炸板数量
            limit_up_count = 0
            broken_count = 0
            
            for _, row in df.iterrows():
                try:
                    code = str(row['code'])
                    current = float(row['current'])
                    prev_close = float(row['prev_close'])
                    high = float(row.get('high', 0))
                    
                    if current == 0 or prev_close == 0: continue

                    # 过滤北证 (8/4/92开头 或 bj开头)
                    if is_bse_stock(code): continue

                    # 判断涨跌停价格
                    is_20cm = code.startswith('30') or code.startswith('68')
                    limit_ratio = 1.2 if is_20cm else 1.1
                    limit_price = round(prev_close * limit_ratio, 2)
                    
                    # 判定涨停
                    if current >= limit_price:
                        limit_up_count += 1
                    # 判定炸板 (最高价曾触及涨停，但当前价未封板)
                    elif high >= limit_price and current < limit_price:
                        broken_count += 1
                except:
                    continue
            
            overview["stats"]["limit_up_count"] = limit_up_count
            overview["stats"]["broken_count"] = broken_count

        else:
            if logger: logger("[!] 无法获取全市场数据，涨跌分布统计失败")
            
    except Exception as e:
        if logger: logger(f"[!] 获取涨跌分布失败: {e}")

    # 4. 计算情绪
    zt_count = overview["stats"]["limit_up_count"]
    up_count = overview["stats"]["up_count"]
    down_count = overview["stats"]["down_count"]
    
    sh_change = 0
    for idx in overview["indices"]:
        if idx["name"] == "上证指数":
            sh_change = idx["change"]
            break
            
    sentiment = "Neutral"
    suggestion = "观察"
    
    # 更加严格的判断逻辑
    if zt_count > 45 and sh_change > 0 and up_count > down_count:
        sentiment = "High"
        suggestion = "积极打板"
    elif zt_count < 15 or sh_change < -0.8 or down_count > (up_count * 1.5):
        sentiment = "Low"
        suggestion = "谨慎出手"
    else:
        sentiment = "Neutral"
        suggestion = "去弱留强"
        
    # 恐慌盘判断
    if sh_change < -1.5 or (sh_change < -0.5 and down_count > 3500):
        sentiment = "Panic"
        suggestion = "空仓避险"
        
    overview["stats"]["sentiment"] = sentiment
    overview["stats"]["suggestion"] = suggestion
    
    return overview
