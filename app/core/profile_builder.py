import pandas as pd
import numpy as np
import json
import os
from datetime import datetime, timedelta
from sklearn.linear_model import LinearRegression
from scipy.stats import zscore

# from app.core.lhb_manager import lhb_manager # REMOVE: Avoid Circular Import

# 假设的数据文件路径
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
LHB_FILE = os.path.join(DATA_DIR, "lhb_history.csv")
PROFILE_FILE = os.path.join(DATA_DIR, "seat_profiles.json")

def get_kline_1min(code, date):
    """
    获取1分钟K线数据 (从本地缓存)
    """
    # Dynamic import to break circular dependency
    from app.core.lhb_manager import lhb_manager
    return lhb_manager.get_kline_1min(code, date)

def extract_features(row):
    """
    提取单次上榜的特征
    """
    code = str(row['stock_code'])
    date = str(row['trade_date'])
    
    # 1. 获取当日分钟线
    df_kline = get_kline_1min(code, date)
    if df_kline is None or df_kline.empty:
        return None
    
    # Ensure time column is datetime
    if '时间' in df_kline.columns:
        df_kline['time'] = pd.to_datetime(df_kline['时间'])
        df_kline['close'] = df_kline['收盘']
        df_kline['volume'] = df_kline['成交量']
    elif 'day' in df_kline.columns: # akshare format sometimes
        df_kline['time'] = pd.to_datetime(df_kline['day'])
    
    # 2. 找到封板时刻 T_seal (假设为当日最高价首次出现的时间)
    limit_price = df_kline['close'].max()
    # Filter for limit price
    seal_candidates = df_kline[df_kline['close'] >= limit_price - 0.01]
    if seal_candidates.empty: return None
    
    seal_row = seal_candidates.iloc[0]
    t_seal_idx = df_kline.index.get_loc(seal_row.name)
    t_seal_time = seal_row['time'] # datetime object
    
    # 特征1: time_feature (距离9:30的分钟数)
    market_open = t_seal_time.replace(hour=9, minute=30, second=0)
    time_feature = (t_seal_time - market_open).total_seconds() / 60
    
    # 特征2: slope_feature (封板前5分钟斜率)
    start_idx = max(0, t_seal_idx - 5)
    slice_5min = df_kline.iloc[start_idx:t_seal_idx]
    if len(slice_5min) < 2:
        slope_feature = 0
    else:
        y = slice_5min['close'].values.reshape(-1, 1)
        X = np.arange(len(y)).reshape(-1, 1)
        reg = LinearRegression().fit(X, y)
        slope_feature = reg.coef_[0][0]
        
    # 特征3: volume_feature (封板分钟成交量 / 过去30分钟均量)
    vol_start_idx = max(0, t_seal_idx - 30)
    vol_slice = df_kline.iloc[vol_start_idx:t_seal_idx]
    avg_vol = vol_slice['volume'].mean() if not vol_slice.empty else 1
    current_vol = seal_row['volume']
    volume_feature = current_vol / avg_vol if avg_vol > 0 else 0
    
    # 特征4: cap_feature (流通市值 - 需外部数据，此处假设row中有)
    cap_feature = row.get('circulation_market_cap', 100) # 默认100亿
    
    # 特征5: board_feature (几连板 - 需外部数据)
    board_feature = row.get('limit_up_days', 1)
    
    return {
        'time': time_feature,
        'slope': slope_feature,
        'vol_ratio': volume_feature,
        'cap': cap_feature,
        'board': board_feature
    }

def build_profiles(logger=None):
    if logger: logger("[Profile] 开始更新游资画像...")
    else: print("开始构建游资画像...")
    
    if not os.path.exists(LHB_FILE):
        msg = f"错误: 未找到龙虎榜数据文件 {LHB_FILE}"
        if logger: logger(msg)
        else: print(msg)
        return

    df = pd.read_csv(LHB_FILE)
    
    profiles = {}
    grouped = df.groupby('buyer_seat_name')
    
    count = 0
    for name, group in grouped:
        if len(group) < 3: continue # Min 3 appearances
        
        features_list = []
        for _, row in group.iterrows():
            feat = extract_features(row)
            if feat: features_list.append(feat)
            
        if not features_list: continue
        
        df_feat = pd.DataFrame(features_list)
        
        # Calculate weights (inverse variance?) - Simplified for now
        weights = [0.2, 0.2, 0.2, 0.2, 0.2]
        
        # Generate description based on features
        avg_time = df_feat['time'].mean()
        avg_board = df_feat['board'].mean()
        
        desc = []
        if avg_time < 30: desc.append("早盘")
        elif avg_time > 200: desc.append("尾盘")
        
        if avg_board < 1.5: desc.append("首板")
        elif avg_board > 3: desc.append("高标")
        
        desc_str = "/".join(desc) if desc else "综合"
        
        profiles[name] = {
            'features': df_feat.mean().to_dict(),
            'std': df_feat.std().to_dict(),
            'weights': weights,
            'count': len(df_feat),
            'desc': desc_str
        }
        count += 1
    
    with open(PROFILE_FILE, 'w', encoding='utf-8') as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
        
    msg = f"画像构建完成，共生成 {count} 个席位画像，已保存至 {PROFILE_FILE}"
    if logger: logger(f"[Profile] {msg}")
    else: print(msg)

if __name__ == "__main__":
    build_profiles()
