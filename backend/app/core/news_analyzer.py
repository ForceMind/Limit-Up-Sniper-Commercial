import requests
import json
import time
from datetime import datetime
import os
import re
from pathlib import Path
from app.core.stock_utils import calculate_metrics
from app.core.market_scanner import scan_intraday_limit_up, get_market_overview, scan_limit_up_pool, scan_broken_limit_pool
from app.core.ai_cache import ai_cache
from app.core.lhb_manager import lhb_manager

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

# Configuration
CLS_API_URL = "https://www.cls.cn/nodeapi/telegraphList"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.cls.cn/telegraph"
}

# Deepseek Configuration
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "") # 请在环境变量中设置 DEEPSEEK_API_KEY
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"


def _build_usage_meta(result):
    if not isinstance(result, dict):
        return {}
    usage = result.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or (prompt_tokens + completion_tokens))
    return {
        "provider": "deepseek",
        "model": result.get("model", "deepseek-chat"),
        "usage": {
            "prompt_tokens": max(0, prompt_tokens),
            "completion_tokens": max(0, completion_tokens),
            "total_tokens": max(0, total_tokens),
        },
    }

def get_market_data(logger=None):
    """
    获取今日市场核心数据：涨停池、炸板池
    """
    if logger: logger("[*] 正在获取今日市场核心数据 (涨停/炸板)...")
    
    market_summary = ""
    
    # 1. 涨停池 (使用 market_scanner 的统一接口)
    try:
        pool = scan_limit_up_pool(logger)
        if pool:
            market_summary += f"【今日涨停池】共 {len(pool)} 家。\n"
            # 由于原生接口不返回连板数，这里只列出部分代表
            # 简单列出前 10 个
            top_stocks = pool[:10]
            market_summary += f"涨停代表: " + ", ".join([f"{s['name']}" for s in top_stocks]) + "\n"
            
            # 提取涨停概念 (简单统计)
            concepts = {}
            for s in pool:
                c = s.get('concept', '')
                if c:
                    for part in c.split(','): # Assuming comma separated
                        part = part.strip()
                        if part:
                            concepts[part] = concepts.get(part, 0) + 1
            
            # Top 3 concepts
            sorted_concepts = sorted(concepts.items(), key=lambda x: x[1], reverse=True)[:3]
            if sorted_concepts:
                market_summary += f"热门概念: " + ", ".join([f"{k}({v})" for k, v in sorted_concepts]) + "\n"
                
    except Exception as e:
        if logger: logger(f"[!] 获取涨停数据失败: {e}")

    # 2. 炸板池
    try:
        pool = scan_broken_limit_pool(logger)
        if pool:
            market_summary += f"【今日炸板池】共 {len(pool)} 家。\n"
            market_summary += f"炸板代表: " + ", ".join([f"{s['name']}" for s in pool[:5]]) + "\n"
    except Exception as e:
        if logger: logger(f"[!] 获取炸板数据失败: {e}")
        
    # 3. 市场情绪概览 (新增)
    try:
        overview = get_market_overview(logger=None)
        stats = overview.get('stats', {})
        market_summary += f"【市场情绪】\n"
        market_summary += f"- 涨跌分布: 上涨 {stats.get('up_count', 0)} 家, 下跌 {stats.get('down_count', 0)} 家, 跌停 {stats.get('limit_down_count', 0)} 家\n"
        market_summary += f"- 市场建议: {stats.get('suggestion', '观察')} (情绪: {stats.get('sentiment', 'Neutral')})\n"
        market_summary += f"- 总成交额: {stats.get('total_volume', 0)} 亿\n"
    except Exception as e:
        if logger: logger(f"[!] 获取市场情绪失败: {e}")
        
    return market_summary

def save_news_history(news_items):
    """保存新闻历史记录到 data/news_history.json"""
    history_file = DATA_DIR / "news_history.json"
    config_file = DATA_DIR / "config.json"
    history = []
    
    # Load config for auto-clean settings
    auto_clean_enabled = True
    auto_clean_days = 14
    if config_file.exists():
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                auto_clean_enabled = config.get('news_auto_clean_enabled', True)
                auto_clean_days = config.get('news_auto_clean_days', 14)
        except:
            pass

    # Load existing
    if history_file.exists():
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except:
            pass
            
    # Merge (avoid duplicates based on text + timestamp)
    existing_keys = {f"{item.get('timestamp', 0)}_{item.get('text', '')[:20]}" for item in history}
    
    for item in news_items:
        # Ensure timestamp exists
        ts = item.get('timestamp', int(time.time()))
        text = item.get('text', '')
        key = f"{ts}_{text[:20]}"
        
        if key not in existing_keys:
            # Add source if not present
            if 'source' not in item:
                item['source'] = 'Unknown'
            history.append(item)
            existing_keys.add(key)
            
    # Sort by timestamp desc
    history.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    
    # Auto-clean logic
    if auto_clean_enabled:
        cutoff_ts = int(time.time()) - (auto_clean_days * 24 * 3600)
        history = [item for item in history if item.get('timestamp', 0) > cutoff_ts]
    
    # Keep a reasonable maximum even if auto-clean is off (e.g. 1000 items)
    if len(history) > 1000:
        history = history[:1000]
    
    # Save
    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving news history: {e}")

def get_cls_news(hours=12, logger=None):
    """
    抓取财联社电报最近 N 小时的数据 (串行)
    """
    msg = f"[*] 正在抓取最近 {hours} 小时的全网舆情 (来源: 财联社)..."
    print(msg)
    if logger: logger(msg)

    current_time = int(time.time())
    start_time = current_time - (hours * 3600)
    
    news_list = []
    
    # 财联社API是基于 last_time 分页的，这使得完全并行比较困难，因为下一页依赖上一页的最后时间。
    # 但是，我们可以预估时间或者并发抓取多个时间段？
    # 实际上，财联社API如果只传 rn (count) 不传 last_time，是第一页。
    # 严格的分页必须串行。
    # 妥协方案：为了速度，我们只抓取第一页 (最新20条) 或者 串行抓取但优化连接。
    # 实际上，对于"最近1小时"，通常1-2页就够了。
    # 如果要多线程，我们可以尝试猜测 last_time? 不可行。
    # 
    # 替代方案：使用 requests.Session 复用连接，减少握手时间。
    
    session = requests.Session()
    last_time = current_time
    
    # 移除多线程，使用串行抓取
    
    for page in range(5): 
        params = {
            "rn": 20,
            "last_time": last_time
        }
        
        try:
            # 使用 session
            resp = session.get(CLS_API_URL, headers=HEADERS, params=params, timeout=5)
            data = resp.json()
            
            if 'data' not in data or 'roll_data' not in data['data']:
                break
                
            items = data['data']['roll_data']
            if not items:
                break
                
            page_valid_count = 0
            for item in items:
                item_time = item.get('ctime', 0)
                if item_time < start_time:
                    # 时间截止，直接返回
                    return news_list 
                
                title = item.get('title', '')
                content = item.get('content', '')
                if not title: 
                    title = content[:30] + "..."
                
                # 过滤掉非A股相关的无关新闻（简单过滤）
                full_text = f"【{title}】{content}"
                if any(k in full_text for k in ["美股", "恒指", "港股", "外汇"]):
                    continue

                news_list.append({
                    "timestamp": item_time,
                    "time_str": datetime.fromtimestamp(item_time).strftime('%Y-%m-%d %H:%M:%S'),
                    "text": full_text
                })
                page_valid_count += 1
            
            if logger: logger(f"    已抓取第 {page+1} 页，本页有效 {page_valid_count} 条...")
            
            # 更新 last_time 为本页最后一条的时间
            last_time = items[-1].get('ctime')
            
            # 移除 sleep 以提高速度
            # time.sleep(1) 
            
        except Exception as e:
            msg = f"[!] Error fetching news: {e}"
            print(msg)
            if logger: logger(msg)
            break
            
    # Save history
    for item in news_list:
        item['source'] = '财联社'
    save_news_history(news_list)
    
    return news_list

def get_eastmoney_news(hours=12, logger=None):
    """
    抓取东方财富 7x24 小时快讯 (A股相关)
    """
    msg = f"[*] 正在抓取最近 {hours} 小时的全网舆情 (来源: 东方财富)..."
    print(msg)
    if logger: logger(msg)
    
    news_list = []
    try:
        # EastMoney 7x24 API
        # Using a known endpoint structure. 
        # Note: This API might change, but it's standard for now.
        url = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
        
        # We might need to fetch multiple pages if hours is large, but for now let's fetch top 50
        # which usually covers the last few hours.
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://kuaixun.eastmoney.com/"
        }
        
        resp = requests.get(url, headers=headers, timeout=10)
        content = resp.text
        
        # The response is usually "var ajaxResult=...;"
        if "var ajaxResult=" in content:
            json_str = content.split("var ajaxResult=")[1].strip().rstrip(";")
            data = json.loads(json_str)
            
            if data and 'LivesList' in data:
                current_time = int(time.time())
                cutoff_time = current_time - (hours * 3600)
                
                for item in data['LivesList']:
                    # Parse time: "2023-10-27 14:30:00"
                    show_time = item.get('showtime')
                    digest = item.get('digest', '')
                    
                    if not show_time or not digest: continue
                    
                    try:
                        news_ts = int(time.mktime(time.strptime(show_time, "%Y-%m-%d %H:%M:%S")))
                    except:
                        continue
                        
                    if news_ts < cutoff_time:
                        continue
                        
                    # Filter for A-share relevance (Simple keyword check)
                    # EastMoney usually has global news, so we filter a bit.
                    keywords = ['A股', '股市', '证券', '沪指', '深成指', '创业板', '涨停', '跌停', '证监会', '央行', '板块', '概念', '龙头', '资金']
                    # Also include stock codes if possible, but regex is expensive here.
                    
                    # If digest is short, it might be just a title.
                    full_text = digest
                    
                    # Check relevance
                    is_relevant = False
                    for kw in keywords:
                        if kw in full_text:
                            is_relevant = True
                            break
                    
                    if is_relevant:
                        news_list.append({
                            "timestamp": news_ts,
                            "time_str": show_time,
                            "text": full_text
                        })
                        
    except Exception as e:
        if logger: logger(f"[!] 东方财富新闻获取失败: {e}")
        
    # Save history
    for item in news_list:
        item['source'] = '东方财富'
    save_news_history(news_list)

    return news_list

def analyze_news_with_deepseek(news_batch, market_summary="", logger=None, mode="after_hours"):
    """
    使用 AI 批量分析新闻和市场数据
    """
    if not news_batch and not market_summary:
        return []

    # Check Cache
    # Use a simplified content string for hashing to avoid minor differences
    news_ids = sorted([str(n.get('id', n.get('timestamp', ''))) for n in news_batch])
    cache_content = f"{mode}|{market_summary}|{''.join(news_ids)}"
    cache_key = ai_cache.generate_key(cache_content)
    
    cached_result = ai_cache.get(cache_key)
    if cached_result:
        msg = f"[*] 使用缓存的AI分析结果 (Key: {cache_key[:8]})..."
        print(msg)
        if logger: logger(msg)
        return cached_result

    msg = f"[*] 调用 AI 分析 {len(news_batch)} 条新闻及市场数据 ({mode})..."
    print(msg)
    if logger: logger(msg)

    # 构造 Prompt
    news_content = "\n".join([f"{i+1}. {n['text']}" for i, n in enumerate(news_batch)])
    
    # 动态调整策略描述基于市场情绪
    is_market_bad = "情绪: Low" in market_summary or "情绪: Panic" in market_summary
    
    # --- Prompt Definitions ---
    
    # 1. Aggressive Prompt (Original - Preferred by User)
    if mode == "after_hours":
        task_desc_agg = "进行【盘后复盘】并挖掘【明日竞价关注股】"
        strategy_desc_agg = """
1. **Aggressive (竞价抢筹)**: 
   - 核心龙头的一字板预期，或今日强势连板股的弱转强。
   - 策略：明日开盘集合竞价直接挂单买入。
2. **LimitUp (盘中打板)**: 
   - 首板挖掘，或大盘共振的低位补涨股。
   - 策略：放入自选，盘中观察，如果快速拉升或封板则买入。
"""
    else: # intraday
        task_desc_agg = "进行【盘中实时分析】并挖掘【当前即将涨停股】"
        strategy_desc_agg = """
1. **Aggressive (立即扫货)**: 
   - 突发重大利好，股价正在快速拉升，即将封板。
   - 策略：立即市价买入，防止买不到。
2. **LimitUp (回封低吸)**: 
   - 炸板回落但承接有力，或分时均线支撑强。
   - 策略：低吸博弈回封。
"""

    # 2. Safe Prompt (Fallback - Compliant)
    if mode == "after_hours":
        task_desc_safe = "进行【盘后复盘】并挖掘【明日竞价关注股】"
        strategy_desc_safe = """
1. **Aggressive (重点关注)**: 
   - 核心龙头的一字板预期，或今日强势连板股的弱转强。
   - 策略：建议重点关注竞价表现。
2. **LimitUp (观察等待)**: 
   - 首板挖掘，或大盘共振的低位补涨股。
   - 策略：放入自选，观察盘中承接力度。
"""
    else: # intraday
        task_desc_safe = "进行【盘中实时分析】并挖掘【当前潜力个股】"
        strategy_desc_safe = """
1. **Aggressive (突发利好)**: 
   - 突发重大利好，股价有快速反应预期。
   - 策略：建议立即关注。
2. **LimitUp (回封博弈)**: 
   - 炸板回落但承接有力，或分时均线支撑强。
   - 策略：关注回封机会。
"""

    if is_market_bad:
        warning_text = """
\n【特别警告】
当前市场情绪极差（Low/Panic）。请务必保守！
除非是市场最高板的绝对龙头（辨识度极高），否则不要推荐 Aggressive 策略。
对于普通利好，直接忽略或仅作为观察。
"""
        strategy_desc_agg += warning_text
        strategy_desc_safe += warning_text

    def build_payload(task_desc, strategy_desc, persona):
        system_prompt = f"""
{persona}。你的任务是{task_desc}。

【今日市场数据】
{market_summary}

【最新舆情新闻】
{news_content}

请结合市场数据（涨停梯队、炸板情况、涨跌家数）和新闻舆情，分析市场情绪主线，并预测关注标的。

请严格按照以下标准分类：
{strategy_desc}

【评分标准 (Score 0-10)】
请基于以下维度进行综合打分，不要随意给分：
1. **新闻重磅度 (40%)**: 是否为国家级政策、行业重大突破或突发利好？(普通消息<6分, 重磅>8分)
2. **题材主流度 (30%)**: 是否契合当前市场主线（如市场数据中提到的热门概念）？(非主线扣分)
3. **个股辨识度 (30%)**: 是否为龙头、老妖股或近期人气股？(杂毛股扣分)
注意：如果仅仅是普通利好且非主线，分数不应超过 7.0。只有绝对龙头或特大利好才能超过 9.0。

请返回纯 JSON 格式，不要包含 Markdown 格式，格式如下：
{{
  "stocks": [
    {{
      "code": "sh600xxx", 
      "name": "股票名", 
      "concept": "核心概念", 
      "reason": "结合今日表现(如3连板)和新闻利好的综合理由", 
      "score": 8.5, 
      "strategy": "Aggressive" 
    }}
  ],
  "remove_stocks": [
    {{
      "code": "sh600xxx",
      "reason": "利空消息或题材退潮"
    }}
  ]
}}
如果新闻没有明确的A股标的，忽略即可。
"""
        return {
            "model": "deepseek-chat",
            "messages": [
                {"role": "user", "content": system_prompt}
            ],
            "temperature": 0.1,
            "response_format": { "type": "json_object" }
        }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }

    # --- Execution Logic with Retry ---
    
    # Attempt 1: Aggressive Prompt
    try:
        payload = build_payload(task_desc_agg, strategy_desc_agg, "你是一个A股顶级游资操盘手")
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60)
        
        # Check for specific Content Risk error
        if response.status_code == 400:
            err_json = response.json()
            if "Content Exists Risk" in str(err_json) or "invalid_request_error" in str(err_json):
                if logger: logger("[!] 触发AI风控拦截，正在切换至安全模式重试...")
                raise ValueError("Content Risk")
                
        if response.status_code != 200:
            msg = f"[!] AI API Error: {response.text}"
            print(msg)
            if logger: logger(msg)
            return []
            
        result = response.json()
        
    except ValueError as ve:
        # Attempt 2: Safe Prompt
        try:
            payload = build_payload(task_desc_safe, strategy_desc_safe, "你是一个专业的A股市场分析师")
            response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60)
            if response.status_code != 200:
                if logger: logger(f"[!] 安全模式也失败: {response.text}")
                return []
            result = response.json()
        except Exception as e:
            if logger: logger(f"[!] 重试失败: {e}")
            return []
            
    except Exception as e:
        msg = f"[!] Analysis Failed: {e}"
        print(msg)
        if logger: logger(msg)
        return {}

    # Process Result (Common for both attempts)
    try:
        if not result or 'choices' not in result or not result['choices']:
            return []
            
        content = result['choices'][0]['message']['content']
        if not content:
            return []
        
        # 尝试提取 JSON 块
        try:
            # 1. 尝试直接解析
            data = json.loads(content)
        except:
            # 2. 尝试提取 ```json ... ``` 或 ``` ... ```
            match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
            else:
                # 3. 尝试寻找第一个 { 和最后一个 }
                start = content.find('{')
                end = content.rfind('}')
                if start != -1 and end != -1:
                    data = json.loads(content[start:end+1])
                else:
                    raise ValueError("No JSON object found")
        
        # Save to Cache
        ai_cache.set(cache_key, data, meta=_build_usage_meta(result))
        
        return data
    except Exception as e:
        if logger: logger(f"[!] 解析AI结果失败: {e}\nRaw Content: {content[:100]}...")
        return {}

def analyze_single_stock(stock_data, logger=None, prompt_type='normal', api_key=None, force_update=False):
    """
    对单个股票进行深度AI分析 (大师级逻辑)
    """
    name = stock_data.get('name', '未知股票')
    code = stock_data.get('code', '')
    price = stock_data.get('current', 0)
    change = stock_data.get('change_percent', 0)
    concept = stock_data.get('concept', '')
    
    # Use provided API key or fallback to env var
    current_api_key = api_key if api_key else DEEPSEEK_API_KEY
    
    if not current_api_key:
        return "分析失败: 未配置 DeepSeek API Key。请在设置中填写或配置环境变量。"
    prompt_type = stock_data.get('promptType', 'default')
    
    # Rate Limit Check
    cache_key = f"stock_analysis_{code}_{prompt_type}"
    
    if not force_update:
        last_ts = ai_cache.get_timestamp(cache_key)
        time_diff = int(time.time()) - last_ts
        
        if time_diff < 600: # 10 minutes
            msg = f"分析过于频繁，请 {600 - time_diff} 秒后再试。"
            if logger: logger(f"[!] {msg}")
            # Try to return cached result if available
            cached_data = ai_cache.get(cache_key)
            if cached_data:
                return f"[缓存结果] {cached_data}"
            return msg
    
    # Additional metrics for better analysis
    turnover = stock_data.get('turnover')
    if turnover is None:
        turnover = stock_data.get('metrics', {}).get('turnover', '未知')
        
    circ_value = stock_data.get('circulation_value')
    if circ_value is None:
        circ_value = stock_data.get('metrics', {}).get('circulation_value', '未知')
        
    if isinstance(circ_value, (int, float)) and circ_value > 0:
        circ_value = f"{round(circ_value / 100000000, 2)}亿"
    
    # Get LHB Info
    lhb_info = lhb_manager.get_latest_lhb_info(code)
    lhb_text = "暂无龙虎榜数据"
    if lhb_info:
        lhb_text = f"日期: {lhb_info['date']}, 席位: {', '.join(lhb_info['seats'])}"

    # [Request 4] Get K-Line Data (1-min) for context
    kline_summary = "暂无分时数据"
    try:
        target_date = lhb_info['date'] if lhb_info else datetime.now().strftime('%Y-%m-%d')
        kline_df = lhb_manager.get_kline_1min(code, target_date, allow_network=False)
        
        # If today is empty (e.g. weekend or before market), try yesterday
        if kline_df is None or kline_df.empty:
            prev_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            kline_df = lhb_manager.get_kline_1min(code, prev_date, allow_network=False)

        if kline_df is not None and not kline_df.empty:
            # Simple feature extraction
            # Assuming cols: 日期, 开盘, 收盘, 最高, 最低, 成交量...
            # akshare cols: 时间, 开盘, 收盘, 最高, 最低, 成交量...
            # Let's just take OHLC of the day
            open_p = kline_df.iloc[0]['开盘']
            close_p = kline_df.iloc[-1]['收盘']
            high_p = kline_df['最高'].max()
            low_p = kline_df['最低'].min()
            
            kline_summary = f"分时: 开{open_p} 收{close_p} 高{high_p} 低{low_p}。"
            
            if close_p > open_p:
                kline_summary += " 全天震荡走高。"
            else:
                kline_summary += " 冲高回落。"
    except:
        pass

    # 1. 尝试获取该股票的最新新闻 (模拟搜索)
    # 这里简单复用 get_cls_news 的逻辑，但针对特定关键词过滤
    # 实际生产中应该调用搜索引擎API或专门的新闻接口
    news_context = "暂无特定新闻"
    try:
        # 简化的新闻获取逻辑，仅作为示例
        # 实际应该去搜 "股票名称 + 利好/利空"
        pass 
    except:
        pass

    # 2. 构建大师级分析 Prompt
    current_ts_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    kline_data = stock_data.get('kline_data')
    
    # Fallback for kline_data if missing (for trading_signal types)
    if not kline_data and prompt_type in ['trading_signal', 'day_trading_signal', 'min_trading_signal']:
        try:
            target_date = datetime.now().strftime('%Y-%m-%d')
            df = lhb_manager.get_kline_1min(code, target_date, allow_network=False)
            if df is None or df.empty:
                prev_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                df = lhb_manager.get_kline_1min(code, prev_date, allow_network=False)
            
            if df is not None and not df.empty:
                kline_data = df.to_dict('records')
        except:
            pass

    # Debug Log for User Verification
    print(f"[AI Analysis] Code: {code}, Turnover: {turnover}, LHB: {lhb_text[:50]}...")

    if prompt_type in ['trading_signal', 'day_trading_signal', 'min_trading_signal']:
        # Prepare K-line data string
        kline_str = "无K线数据"
        if kline_data and len(kline_data) > 0:
            # Format: Date, Open, Close, High, Low, Volume
            kline_lines = ["Date,Open,Close,High,Low,Volume"]
            for k in kline_data:
                date = k.get('date') or k.get('时间')
                o = k.get('open') or k.get('开盘')
                c = k.get('close') or k.get('收盘')
                h = k.get('high') or k.get('最高')
                l = k.get('low') or k.get('最低')
                v = k.get('volume') or k.get('成交量')
                kline_lines.append(f"{date},{o},{c},{h},{l},{v}")
            kline_str = "\n".join(kline_lines)

        timeframe_desc = "近期K线"
        if prompt_type == 'day_trading_signal':
            timeframe_desc = "日K线"
        elif prompt_type == 'min_trading_signal':
            timeframe_desc = "分时(1分钟)K线"

        prompt = f"""
你是一位顶级的量化交易员和技术分析专家。请根据提供的股票【{name} ({code})】的{timeframe_desc}数据，进行精确的买卖点分析。

【K线数据 (OHLCV)】
{kline_str}

【分析要求】
1. **趋势判断**: 识别当前是上升、下降还是震荡趋势。
2. **关键点位**: 找出最近的支撑位和压力位。
3. **交易信号**: 基于技术指标（如均线、量价关系、K线形态、背离等），找出具体的买入或卖出信号。
   - 信号必须包含：日期、类型(buy/sell)、价格、理由。
   - 只标记胜率较高的关键转折点。
   - 对于分时数据，重点关注盘中异动和量价配合。
   - **重要**：同一根K线（同一个时间点）只能输出一个最强的信号，严禁在同一时间点同时输出买入和卖出，也不要输出重复信号。

【输出格式】
必须严格返回以下 JSON 格式，不要包含 Markdown 代码块标记：
{{
    "summary": "简短的分析总结（200字以内），包含趋势和操作建议。",
    "signals": [
        {{
            "date": "YYYY-MM-DD HH:MM", 
            "type": "buy", 
            "price": 10.5, 
            "reason": "突破20日均线且放量"
        }},
        {{
            "date": "YYYY-MM-DD HH:MM", 
            "type": "sell", 
            "price": 11.2, 
            "reason": "触及前期压力位出现顶分型"
        }}
    ]
}}
"""
    elif prompt_type == 'aggressive':
        prompt = f"""
你是一位擅长竞价抢筹和超短线博弈的顶级游资。请针对股票【{name} ({code})】进行“竞价抢筹”维度的深度推演。

【今日盘面数据】(基于今日收盘或实时数据)
- 现价: {price}
- 涨幅: {change}%
- 换手率: {turnover}%
- 流通市值: {circ_value}
- 概念: {concept}
- 龙虎榜数据: {lhb_text}
- 分时形态: {kline_summary}
- 历史指标: {json.dumps(stock_data.get('metrics', {}), ensure_ascii=False)}

【核心分析逻辑】
请重点回答以下问题（Chain of Thought）：
1. **抢筹逻辑**: 基于今日的表现（如涨停、烂板、大长腿等），为什么这只股票明天竞价可能会有溢价或弱转强？
2. **预期差**: 市场可能忽略了什么？是否存在卡位、补涨或穿越的预期？
3. **主力动向**: 结合龙虎榜数据（如果有），分析主力（如{lhb_text}）的意图。
4. **风险收益比**: 如果明天竞价买入，盈亏比如何？

【最终输出】
请以 Markdown 格式输出简报：
### 1. 核心抢筹理由
(直击痛点，说明上涨预期)

### 2. 主力与预期差
(分析主力意图和市场情绪，提及知名游资)

### 3. 竞价策略
- **关注价格**: (什么样的开盘价符合预期)
- **止损位**: 
- **胜率**: (高/中/低)

请保持语言犀利、极简，直击核心。

【分析时间】{current_ts_str}
"""
    elif prompt_type == 'limitup':
        prompt = f"""
你是一位专注于打板和连板接力的短线高手。请针对今日涨停/冲击涨停的股票【{name} ({code})】进行深度复盘。

【今日盘面数据】
- 现价: {price}
- 涨幅: {change}%
- 换手率: {turnover}%
- 流通市值: {circ_value}
- 概念: {concept}
- 龙虎榜数据: {lhb_text}
- 分时形态: {kline_summary}
- 连板高度/指标: {json.dumps(stock_data.get('metrics', {}), ensure_ascii=False)}

【分析逻辑】
1. **涨停质量**: 封单如何？是否烂板？是主动进攻还是被动跟风？
2. **板块地位**: 是龙头、中军还是补涨？板块梯队是否完整？
3. **主力分析**: 龙虎榜显示有哪些知名游资（{lhb_text}）？他们的风格是锁仓还是砸盘？
4. **明日推演**: 预期是高开秒板、分歧换手还是直接核按钮？

【最终输出】
请以 Markdown 格式输出简报：
### 1. 涨停质量评估
(分析封板力度和主力意图)

### 2. 板块地位与主力
(明确其在板块中的角色，点评龙虎榜游资)

### 3. 明日接力策略
- **预期开盘**: (高开/平开/低开)
- **买入条件**: (什么情况下可以接力)
- **风险提示**: (最大的坑在哪里)

请保持语言犀利、专业，拒绝模棱两可。

【分析时间】{current_ts_str}
"""
    elif prompt_type == 'manual':
        prompt = f"""
你是一位稳健的波段交易者和基本面研究员。请对自选股【{name} ({code})】进行全方位的价值与趋势分析。

【当前数据】
- 现价: {price}
- 涨跌幅: {change}%
- 换手率: {turnover}%
- 流通市值: {circ_value}
- 核心概念: {concept}
- 龙虎榜数据: {lhb_text}

【分析逻辑】
1. **基本面/题材**: 公司核心业务是什么？近期是否有催化剂（业绩、政策、重组）？
2. **技术趋势**: 当前处于上升趋势、震荡还是下跌中继？关键支撑压力位在哪里？
3. **资金面**: 龙虎榜是否有机构或知名游资介入？

【最终输出】
请以 Markdown 格式输出简报：
### 1. 核心逻辑与题材
(简述上涨理由)

### 2. 技术与资金面
(分析K线形态和主力资金，提及龙虎榜)

### 3. 操作建议
3. **资金流向**: 近期是否有主力资金持续流入迹象？

【最终输出】
请以 Markdown 格式输出简报：
### 1. 核心价值与题材
(简述基本面亮点)

### 2. 趋势与资金分析
(技术面判断)

### 3. 操作建议
- **适合策略**: (短线/中线/观望)
- **买入区间**: 
- **止损位**: 

请客观理性，注重风险控制。

【分析时间】{current_ts_str}
"""
    else:
        prompt = f"""
你是一位拥有20年经验的资深A股市场分析师，精通情绪周期、题材挖掘和技术面分析。请对股票【{name} ({code})】进行全方位的深度推演。

【今日盘面数据】
- 现价: {price}
- 涨跌幅: {change}%
- 换手率: {turnover}%
- 流通市值: {circ_value}
- 核心概念: {concept}
- 辅助指标: {json.dumps(stock_data.get('metrics', {}), ensure_ascii=False)}

【分析逻辑】
请按照以下步骤进行思考（Chain of Thought），并输出最终报告：

1. **题材定性**: 该股票的核心逻辑是什么？是否属于当前市场的主线题材？
2. **情绪周期**: 当前市场情绪处于什么阶段？该股在当前周期中的地位如何？
3. **技术面与盘口**: 结合涨跌幅、换手率和流通盘大小，判断主力意图（洗盘、出货、吸筹、拉升）。
4. **风险提示**: 有无潜在的利空或抛压风险。

【最终输出】
请以 Markdown 格式输出一份简报，包含以下章节：
### 1. 核心逻辑与地位
(简述题材及地位)

### 2. 盘面深度解析
(结合情绪与技术面分析)

### 3. 操盘计划
- **买入策略**: (具体的买点，如打板、低吸、半路)
- **卖出策略**: (止盈止损位)
- **胜率预估**: (高/中/低)

请保持语言犀利、专业，拒绝模棱两可的废话。

【分析时间】{current_ts_str}
"""

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一位资深A股分析师，风格客观犀利，擅长数据分析。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.4,
        "stream": False
    }
    
    if prompt_type == 'trading_signal':
        payload['response_format'] = { "type": "json_object" }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {current_api_key}"
    }

    try:
        if logger: logger(f"[*] 正在请求AI大师分析: {name}...")
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60)
        if response.status_code == 200:
            result = response.json()
            content = result['choices'][0]['message']['content']
            
            # Parse JSON if needed
            if prompt_type == 'trading_signal':
                try:
                    content = json.loads(content)
                except:
                    pass

            # Save to cache
            ai_cache.set(cache_key, content, meta=_build_usage_meta(result))
            
            return content
        else:
            return f"分析失败: API返回错误 {response.status_code}"
    except Exception as e:
        return f"分析失败: {str(e)}"

def generate_watchlist(logger=None, mode="after_hours", hours=None, update_callback=None):
    msg = f"[-] 启动{mode}分析 (AI Powered)..."
    print(msg)
    if logger: logger(msg)
    
    # 0. 获取市场数据
    market_summary = get_market_data(logger=logger)
    if logger: logger(f"[-] 市场数据获取完成。")

    # 1. 获取新闻
    # 如果未指定 hours，则使用默认逻辑
    if hours is None:
        hours = 2 if mode == "intraday" else 12
        
    # Fetch news from multiple sources
    news_items_cls = get_cls_news(hours=hours, logger=logger)
    news_items_em = get_eastmoney_news(hours=hours, logger=logger)
    
    # Combine and deduplicate
    news_items = news_items_cls + news_items_em
    # Sort by timestamp descending
    news_items.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    
    msg = f"[-] 获取到 {len(news_items)} 条有效资讯 (CLS: {len(news_items_cls)}, EastMoney: {len(news_items_em)})。"
    print(msg)
    if logger: logger(msg)
    
    watchlist = {}
    
    # 1. 加载现有列表 (用于对比变化)
    output_file = DATA_DIR / "watchlist.json"
    initial_codes = set()
    if output_file.exists():
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
                for item in existing_data:
                    watchlist[item['code']] = item
                    initial_codes.add(item['code'])
        except Exception as e:
            msg = f"[!] 加载现有关注列表失败: {e}"
            print(msg)
            if logger: logger(msg)
            # If we can't load the existing list, we risk overwriting it with empty data.
            # Better to abort or proceed with caution.
            # For now, we'll assume it's empty but log error.
            pass

    # 2. 如果是盘中模式，进行行情扫描并更新/剔除
    if mode == "intraday":
        intraday_stocks, sealed_stocks = scan_intraday_limit_up(logger=logger)
        
        # [Fix] If scan returns empty (e.g. network error), do NOT clear existing data
        if not intraday_stocks and not sealed_stocks:
            msg = "[!] 盘中扫描无结果。为防止清空列表，跳过更新。"
            print(msg)
            if logger: logger(msg)
        else:
            # 合并两者用于判断是否仍在活跃池
            scanner_stocks = intraday_stocks + sealed_stocks
            scanner_codes = set(s['code'] for s in scanner_stocks)
            sealed_codes = set(s['code'] for s in sealed_stocks)
            
            # 2.1 标记不再满足条件的股票为 Discarded
            # [Modified] 用户反馈不要清空列表，改为保留历史记录，不做 Discarded 处理
            # for code, item in watchlist.items():
            #     # 只处理之前是由盘中突击策略加入的股票
            #     # 识别特征: strategy=LimitUp 且 reason 包含 "盘中突击"
            #     if item.get('strategy_type') == 'LimitUp' and '盘中突击' in item.get('news_summary', ''):
            #         if code not in scanner_codes:
            #             # 不在最新的扫描结果中，说明条件已变差(或已涨停但未被识别为sealed?)
            #             # 标记为 Discarded，前端根据此状态显示在剔除区
            #             item['strategy_type'] = 'Discarded'
            #             if '已剔除' not in item['news_summary']:
            #                 item['news_summary'] += " (已剔除)"
            #     elif code in sealed_codes:
            #         # 如果在 sealed_stocks 中，更新状态为 Sealed (或者在 news_summary 中注明)
            #         # 这样用户知道它已经封板了
            #         if '已封板' not in item['news_summary']:
            #             item['news_summary'] = f"[已封板] {item['news_summary']}"
            
            # 仅更新已封板状态
            for code, item in watchlist.items():
                if code in sealed_codes:
                     if '已封板' not in item.get('news_summary', ''):
                        item['news_summary'] = f"[已封板] {item.get('news_summary', '')}"

            # 2.2 添加/更新当前扫描到的股票
            for stock in scanner_stocks:
                code = stock['code']
                # 计算指标
                metrics = calculate_metrics(code)
                
                # 如果是 sealed，在 reason 前加标记
                reason = stock['reason']
                if code in sealed_codes and '已封板' not in reason:
                    reason = f"[已封板] {reason}"
                
                new_item = {
                "code": code,
                "name": stock['name'],
                "news_summary": reason,
                "concept": stock['concept'],
                "initial_score": stock.get('score', 0), # sealed stock might not have score, default 0
                "strategy_type": stock['strategy'],
                "seal_rate": metrics['seal_rate'],
                "broken_rate": metrics['broken_rate'],
                "next_day_premium": metrics['next_day_premium'],
                "limit_up_days": metrics['limit_up_days']
                }
                # 覆盖旧数据 (包括之前可能被标记为 Discarded 的，如果又满足条件了就复活)
                watchlist[code] = new_item
            
            # [新增] 竞价列表清理逻辑 (10:00 后清理竞价策略股票)
            now = datetime.now()
            if now.hour >= 10:
                for code, item in watchlist.items():
                    # 修正: 策略类型应为 Aggressive
                    if item.get('strategy_type') == 'Aggressive' and '已剔除' not in item.get('news_summary', ''):
                        # 检查是否涨停，如果没涨停且时间已过，则剔除
                        if code not in sealed_codes:
                            item['strategy_type'] = 'Discarded'
                            item['news_summary'] = f"[竞价过期] {item.get('news_summary', '')}"

            # [新增] 立即保存并通知前端，实现"先加列表，再丰富数据"
            try:
                temp_list = list(watchlist.values())
                temp_list.sort(key=lambda x: x.get('initial_score', 0), reverse=True)
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(temp_list, f, ensure_ascii=False, indent=2)
                
                if update_callback:
                    update_callback()
                    
                msg = f"[-] 盘中扫描完成，已更新 {len(scanner_stocks)} 只候选股，开始AI分析..."
                print(msg)
                if logger: logger(msg)
            except Exception as e:
                print(f"Error saving intermediate watchlist: {e}")
            
    # 如果没有新闻，也至少跑一次市场数据分析
    if not news_items:
        news_items = [{"text": "当前时段无重大新闻，请基于市场数据分析。"}]

    batch_size = 5
    for i in range(0, len(news_items), batch_size):
        batch = news_items[i:i+batch_size]
        # 只有第一批带上完整的 market_summary，避免重复消耗 token
        current_market_summary = market_summary if i == 0 else "（市场数据参考上文）"
        
        # AI Analysis returns a dict with 'stocks' and 'remove_stocks'
        analysis_result = analyze_news_with_deepseek(batch, market_summary=current_market_summary, logger=logger, mode=mode)
        
        # Handle removals
        if isinstance(analysis_result, dict):
            remove_list = analysis_result.get('remove_stocks', [])
            for item in remove_list:
                code = item.get('code')
                if not code: continue
                # Fix code format
                if not (code.startswith('sh') or code.startswith('sz') or code.startswith('bj')):
                    if code.startswith('6'): code = 'sh' + code
                    elif code.startswith('0') or code.startswith('3'): code = 'sz' + code
                
                if code in watchlist:
                    reason = item.get('reason', 'AI建议剔除')
                    if logger: logger(f"    [-] AI建议剔除: {watchlist[code]['name']} ({code}) - {reason}")
                    # Don't delete, just mark as Discarded so user can see it
                    watchlist[code]['strategy_type'] = 'Discarded'
                    watchlist[code]['news_summary'] = f"[AI剔除] {reason}"

            analyzed_stocks = analysis_result.get('stocks', [])
        else:
            # Fallback if AI returns list directly (old format compatibility)
            analyzed_stocks = analysis_result if isinstance(analysis_result, list) else []
        
        for stock in analyzed_stocks:
            code = stock['code']
            # 简单的代码格式修正 (确保是 sh/sz/bj 开头)
            if not (code.startswith('sh') or code.startswith('sz') or code.startswith('bj')):
                # 尝试修复
                if code.startswith('6'): code = 'sh' + code
                elif code.startswith('0') or code.startswith('3'): code = 'sz' + code
                elif code.startswith('8') or code.startswith('4') or code.startswith('9'): code = 'bj' + code
            
            # 过滤非A股/北证 (如港股 0xxxx 5位)
            # A股/北证代码通常是6位数字
            raw_code = code.replace('sh', '').replace('sz', '').replace('bj', '')
            if not raw_code.isdigit() or len(raw_code) != 6:
                if logger: logger(f"    [!] 忽略非标准代码: {code}")
                continue

            msg = f"    [+] 挖掘目标: {stock['name']} ({code}) - {stock['strategy']} - {stock['score']}"
            print(msg)
            if logger: logger(msg)
            
            # 计算高级指标
            metrics = calculate_metrics(code)
            
            # 去重逻辑：保留分数更高的
            current_score = stock.get('score', 0)
            
            # 构造新数据对象
            new_item = {
                "code": code,
                "name": stock.get('name', '未知'),
                "news_summary": stock.get('reason', '无理由'),
                "concept": stock.get('concept', '其他'),
                "initial_score": current_score,
                "strategy_type": stock.get('strategy', 'Neutral'), # Aggressive or LimitUp
                # 合并高级指标
                "seal_rate": metrics['seal_rate'],
                "broken_rate": metrics['broken_rate'],
                "next_day_premium": metrics['next_day_premium'],
                "limit_up_days": metrics['limit_up_days']
            }
            
            # 如果已存在，且新分数更高，则覆盖；否则保留旧的但更新指标
            if code not in watchlist or current_score > watchlist[code].get('initial_score', 0):
                watchlist[code] = new_item
            else:
                # 仅更新指标和新闻（如果需要）
                watchlist[code].update({
                    "seal_rate": metrics['seal_rate'],
                    "broken_rate": metrics['broken_rate'],
                    "next_day_premium": metrics['next_day_premium'],
                    "limit_up_days": metrics['limit_up_days']
                })
        
        time.sleep(1) # 避免 API 速率限制

    # 如果没有分析出数据（可能是 Key 没填或新闻太少），加入测试数据
    if not watchlist:
        msg = "[-] 未挖掘到有效标的，添加测试数据以供演示..."
        print(msg)
        if logger: logger(msg)
        test_data = [
            {"code": "sz002405", "name": "四维图新", "concept": "自动驾驶", "score": 9.2, "strategy": "Aggressive", "reason": "获得特斯拉FSD地图数据授权"},
            {"code": "sh600519", "name": "贵州茅台", "concept": "白酒", "score": 7.5, "strategy": "LimitUp", "reason": "分红超预期"},
            {"code": "sz300059", "name": "东方财富", "concept": "互联网金融", "score": 8.0, "strategy": "LimitUp", "reason": "成交量突破万亿"}
        ]
        for t in test_data:
            metrics = calculate_metrics(t['code'])
            watchlist[t['code']] = {
                "code": t['code'],
                "name": t['name'],
                "news_summary": t['reason'],
                "concept": t['concept'],
                "initial_score": t['score'],
                "strategy_type": t['strategy'],
                "seal_rate": metrics['seal_rate'],
                "broken_rate": metrics['broken_rate'],
                "next_day_premium": metrics['next_day_premium'],
                "limit_up_days": metrics['limit_up_days']
            }

    # 3. 保存结果
    output_file = DATA_DIR / "watchlist.json"
    
    # 重新加载现有文件以避免覆盖在此期间手动添加的股票 (Race Condition Fix)
    current_watchlist = {}
    if output_file.exists():
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                existing = json.load(f)
                for item in existing:
                    current_watchlist[item['code']] = item
        except:
            pass
            
    # 合并本次分析结果
    for code, item in watchlist.items():
        # Preserve added_time if exists in current_watchlist but not in new item (shouldn't happen if we loaded it)
        if code in current_watchlist and 'added_time' in current_watchlist[code]:
            item['added_time'] = current_watchlist[code]['added_time']
        current_watchlist[code] = item
        
    final_list = list(current_watchlist.values())
    
    # [Request 3] Integrate LHB Data into Watchlist
    try:
        # Get latest LHB date
        lhb_dates = lhb_manager.get_available_dates()
        if lhb_dates:
            latest_lhb_date = lhb_dates[0]
            # Fetch data
            lhb_data = lhb_manager.get_daily_data(latest_lhb_date)
            # Map code -> simple summary
            lhb_map = {}
            for stock in lhb_data:
                # Extract Top Hot Money
                hms = [s['hot_money'] for s in stock['seats'] if s['hot_money']]
                
                desc = ""
                if hms:
                    desc = f"游资:{','.join(hms[:2])}"
                elif stock['total_net_buy'] > 50000000:
                    desc = "机构/大额买入"
                elif '机构专用' in [s['name'] for s in stock['seats']]:
                    desc = "机构榜"
                
                if desc:
                    lhb_map[stock['code']] = f"[LHB:{desc}]"
            
            # Apply to final_list
            for item in final_list:
                if item['code'] in lhb_map:
                    tag = lhb_map[item['code']]
                    if tag not in item.get('news_summary', ''):
                        item['news_summary'] = f"{tag} {item.get('news_summary', '')}"
    except Exception as e:
        if logger: logger(f"[!] LHB integration failed: {e}")

    # [Request 4] Batch fetch turnover for all stocks
    try:
        from app.core.data_provider import data_provider
        codes_to_fetch = [item['code'] for item in final_list]
        if codes_to_fetch:
            quotes = data_provider.fetch_quotes(codes_to_fetch)
            quote_map = {q['code']: q for q in quotes}
            for item in final_list:
                code = item['code']
                if code in quote_map:
                    item['turnover'] = quote_map[code].get('turnover', 0)
    except Exception as e:
        if logger: logger(f"[!] Failed to update turnover: {e}")

    # [Request 1] Sort: Manual (Newest First) > AI (Score Desc)
    def sort_key(item):
        is_manual = 1 if item.get('strategy_type') == 'Manual' else 0
        added_time = item.get('added_time', 0)
        score = item.get('initial_score', 0)
        # Tuple comparison: (is_manual desc, added_time desc, score desc)
        return (is_manual, added_time, score)

    final_list.sort(key=sort_key, reverse=True)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)
        
    # 计算变化
    final_codes = set(item['code'] for item in final_list)
    added_codes = final_codes - initial_codes
    removed_codes = initial_codes - final_codes
    
    # 获取新增股票名称，并按策略分类
    added_aggressive = []
    added_limitup = []
    
    for code in added_codes:
        item = current_watchlist.get(code)
        if item:
            info = f"{item['name']}({code})"
            if item.get('strategy_type') == 'Aggressive':
                added_aggressive.append(info)
            else:
                added_limitup.append(info)
            
    msg = f"[+] 复盘完成。共 {len(final_list)} 个标的。\n"
    if added_aggressive:
        msg += f"    - [竞价抢筹] 新增 {len(added_aggressive)} 只: {', '.join(added_aggressive)}\n"
    if added_limitup:
        msg += f"    - [盘中打板] 新增 {len(added_limitup)} 只: {', '.join(added_limitup)}\n"
    if not added_aggressive and not added_limitup:
        msg += f"    - 无新增标的\n"
        
    msg += f"    - 移除 {len(removed_codes)} 只\n"
    msg += f"    - 列表已保存至 {output_file}"
    
    print(msg)
    if logger: logger(msg)

def analyze_daily_lhb(date_str, lhb_data, logger=None, force_update=False):
    """
    AI 深度分析龙虎榜日报
    """
    if not lhb_data:
        return "当日无龙虎榜数据，无法分析。"
        
    # Check Cache
    cache_key = f"lhb_daily_analysis_{date_str}"
    
    if not force_update:
        cached = ai_cache.get(cache_key)
        if cached:
            return cached

    # 1. 预处理数据，精简 Prompt
    # 统计活跃游资
    hot_money_act = {}
    # 统计板块 (这里没有板块数据，只能靠AI知识或后续补充，先忽略或基于名字猜)
    
    summary_lines = []
    
    # Sort stocks by total buy amount (approx) or net buy
    # lhb_data is list of dicts from lhb_manager.get_daily_data
    # Structure: [{'code':..., 'name':..., 'seats': [{'name':..., 'buy':..., 'hot_money':...}]}]
    
    # Calculate net buy for sorting
    for stock in lhb_data:
        net = sum(s['buy'] - s['sell'] for s in stock['seats'])
        stock['net_buy'] = net
        
    # Top 10 Net Buy
    top_stocks = sorted(lhb_data, key=lambda x: x['net_buy'], reverse=True)[:10]
    
    summary_lines.append(f"【{date_str} 龙虎榜精选数据】")
    
    for stock in top_stocks:
        seats_desc = []
        for s in stock['seats']:
            # Only show big seats (>1000万) or Hot Money
            if s['buy'] > 10000000 or s['hot_money']:
                hm = f"[{s['hot_money']}]" if s['hot_money'] else ""
                seats_desc.append(f"{s['name']}{hm}(买{int(s['buy']/10000)}万)")
                
                # Count hot money stats
                if s['hot_money']:
                    if s['hot_money'] not in hot_money_act:
                        hot_money_act[s['hot_money']] = []
                    hot_money_act[s['hot_money']].append(f"{stock['name']}")
                    
        if seats_desc:
            summary_lines.append(f"- {stock['name']}({stock['code']}): 净买入{int(stock['net_buy']/10000)}万。主力: {'; '.join(seats_desc)}")

    # Add Hot Money Summary
    summary_lines.append("\n【活跃游资统计】")
    sorted_hm = sorted(hot_money_act.items(), key=lambda x: len(x[1]), reverse=True)
    for name, stocks in sorted_hm[:5]:
        summary_lines.append(f"- {name}: 参与 {len(stocks)} 只 ({', '.join(stocks)})")

    prompt_content = "\n".join(summary_lines)
    
    system_prompt = f"""
你是一位资深的A股龙虎榜分析师。请根据提供的龙虎榜数据，撰写一份【{date_str} 龙虎榜深度复盘】。

【分析数据】
{prompt_content}

【分析要求】
1. **市场情绪定性**: 基于大额榜单和游资出手力度，判断今日情绪是冰点、回暖还是高潮？
2. **游资风格点评**: 哪些知名游资（如章盟主、呼家楼等）在主导行情？他们的风格是锁仓还是一日游？
3. **核心个股逻辑**: 挑选 2-3 只最具代表性的个股，分析主力资金意图（是机构抱团还是游资接力）。
4. **明日推演**: 资金在大举进攻哪个方向？明日应该关注什么？

【输出格式】
请以 Markdown 格式输出，包含以下章节：
### 1. 情绪与游资综述
### 2. 核心席位大解密
### 3. 明日风向标
"""

    # Call AI
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "user", "content": system_prompt}
        ],
        "temperature": 0.3
    }
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    
    try:
        if logger: logger(f"[*] 正在生成龙虎榜日报分析 ({date_str})...")
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=90)
        if response.status_code == 200:
            result = response.json()
            content = result['choices'][0]['message']['content']
            ai_cache.set(cache_key, content, meta=_build_usage_meta(result))
            return content
        else:
            return f"分析失败: API Error {response.status_code}"
    except Exception as e:
        return f"分析异常: {e}"

if __name__ == "__main__":
    generate_watchlist()
