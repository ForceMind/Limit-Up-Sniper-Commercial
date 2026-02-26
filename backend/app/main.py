from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, BackgroundTasks, Query, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from app.db.database import get_db
import requests
import json
import os
import shutil
import asyncio
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional
from pydantic import BaseModel
from app.core.news_analyzer import generate_watchlist, analyze_single_stock, analyze_daily_lhb
from app.core.market_scanner import scan_limit_up_pool, scan_broken_limit_pool, get_market_overview
from app.core.stock_utils import calculate_metrics, is_trading_time, is_market_open_day
from app.core.data_provider import data_provider
from app.core.lhb_manager import lhb_manager
from app.core.ai_cache import ai_cache
from app.core.config_manager import SYSTEM_CONFIG, save_config, DEFAULT_SCHEDULE
from app.core.ws_hub import ws_hub
from app.core.runtime_logs import add_runtime_log
from app.core.operation_log import log_user_operation
from app.api import auth, admin, payment
from app.db import database, models
from app.dependencies import get_current_user, check_ai_permission, check_raid_permission, check_review_permission, check_data_permission, QuotaLimitExceeded, UpgradeRequired
from app.core import user_service
from app.core import watchlist_stats

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Ensure data dir exists
DATA_DIR.mkdir(exist_ok=True)

# Initialize DB Tables
database.Base.metadata.create_all(bind=database.engine)

app = FastAPI()
SERVER_VERSION = "v2.5.0"

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(payment.router, prefix="/api/payment", tags=["payment"])

# CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源，生产环境应限制为前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = BASE_DIR.parent / "frontend"
ADMIN_INDEX_FILE = FRONTEND_DIR / "admin" / "index.html"
USER_OP_LOG_SKIP_PREFIXES = (
    "/api/stocks",
    "/api/indices",
    "/api/limit_up_pool",
    "/api/intraday_pool",
    "/api/market_sentiment",
    "/api/lhb/status",
    "/api/status",
    "/api/config",
    "/api/admin/logs/system",
    "/api/admin/logs/user_ops",
    "/api/admin/monitor/ai_cache",
    "/api/admin/monitor/ai_cache/item",
    "/api/admin/login",
    "/api/admin/logout",
    "/api/admin/update_password",
    "/api/admin/panel_path",
    "/api/admin/users/reset_password",
    "/api/admin/users/add_time",
    "/api/admin/orders/approve",
    "/api/admin/config",
    "/api/auth/login_user",
    "/api/auth/register",
    "/api/auth/apply_trial",
)


def _client_ip_from_request(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return ""


def _should_log_api_path(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    return not any(path.startswith(prefix) for prefix in USER_OP_LOG_SKIP_PREFIXES)


def _resolve_actor(path: str, request: Request) -> str:
    if path.startswith("/api/admin/"):
        return "admin"
    device_id = (request.headers.get("X-Device-ID") or "").strip()
    if device_id.startswith("guest") or device_id.startswith("visitor_"):
        return "guest"
    if device_id:
        return "user"
    return "anonymous"


@app.middleware("http")
async def admin_panel_custom_path_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        return await call_next(request)

    admin_path = admin.get_admin_panel_path()
    normalized = path.rstrip("/") or "/"

    if admin_path != "/admin" and (normalized == "/admin" or normalized.startswith("/admin/")):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    if ADMIN_INDEX_FILE.exists():
        if normalized == admin_path and path.endswith("/") and path != "/":
            return RedirectResponse(url=admin_path, status_code=307)
        if normalized == admin_path or normalized.startswith(admin_path + "/"):
            return FileResponse(str(ADMIN_INDEX_FILE))

    return await call_next(request)


@app.middleware("http")
async def user_operation_logger(request: Request, call_next):
    path = request.url.path
    method = request.method
    start_ts = time.time()

    try:
        response = await call_next(request)
    except Exception as e:
        if _should_log_api_path(path):
            log_user_operation(
                "api_call",
                status="failed",
                actor=_resolve_actor(path, request),
                method=method,
                path=path,
                ip=_client_ip_from_request(request),
                username=(request.headers.get("X-User-Name") or "").strip(),
                device_id=(request.headers.get("X-Device-ID") or "").strip(),
                detail=f"exception={str(e)}",
            )
        raise

    if _should_log_api_path(path):
        status_code = int(getattr(response, "status_code", 0) or 0)
        latency_ms = int((time.time() - start_ts) * 1000)
        log_user_operation(
            "api_call",
            status="success" if status_code < 400 else "failed",
            actor=_resolve_actor(path, request),
            method=method,
            path=path,
            ip=_client_ip_from_request(request),
            username=(request.headers.get("X-User-Name") or "").strip(),
            device_id=(request.headers.get("X-Device-ID") or "").strip(),
            detail=f"status_code={status_code}, latency_ms={latency_ms}",
            extra={"status_code": status_code, "latency_ms": latency_ms},
        )

    return response

@app.get("/api/status")
async def get_system_status():
    """获取系统状态 (交易日/时间)"""
    return {
        "status": "success",
        "is_trading_time": is_trading_time(),
        "is_market_open_day": is_market_open_day(),
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "server_version": SERVER_VERSION
    }

@app.get("/api/news_history/clear")
async def clear_news_history(range: str = "all", user: models.User = Depends(check_data_permission)):
    """清理新闻历史
    range: all, before_today, before_3d, before_7d
    """
    history_file = DATA_DIR / "news_history.json"
    if not history_file.exists():
        return {"status": "success", "message": "No history to clear"}
        
    try:
        if range == "all":
            new_history = []
        else:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
            
            now_ts = int(time.time())
            if range == "before_today":
                # Today 00:00:00
                cutoff_ts = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            elif range == "before_3d":
                cutoff_ts = now_ts - (3 * 24 * 3600)
            elif range == "before_7d":
                cutoff_ts = now_ts - (7 * 24 * 3600)
            else:
                cutoff_ts = 0
                
            new_history = [item for item in history if item.get('timestamp', 0) >= cutoff_ts]
            
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(new_history, f, ensure_ascii=False, indent=2)
            
        return {"status": "success", "message": f"History cleared with range: {range}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def load_watchlist():
    """加载复盘生成的关注列表"""
    file_path = DATA_DIR / "watchlist.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data
        except:
            return []
    return []

def save_watchlist(data):
    """保存关注列表"""
    file_path = DATA_DIR / "watchlist.json"
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving watchlist: {e}")

def load_favorites():
    """加载自选股列表 (长期关注)"""
    file_path = DATA_DIR / "favorites.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_favorites(data):
    """保存自选股列表"""
    file_path = DATA_DIR / "favorites.json"
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving favorites: {e}")

def clean_watchlist():
    """
    Clean up watchlist (AI generated only)
    """
    global watchlist_data
    
    if not watchlist_data:
        return

    seen = set()
    new_list = []
    
    # Keep unique codes
    for item in watchlist_data:
        if item['code'] not in seen:
            seen.add(item['code'])
            new_list.append(item)
            
    # Limit size
    if len(new_list) > 100:
        new_list = new_list[:100]
        
    # [新增] 每日清理逻辑: 如果是新的一天(9:00前)，清理掉昨天的"已剔除"或"过期"数据
    # 这里简单判断: 如果列表里有数据，且当前时间是 08:30-09:15 之间，且数据是旧的(added_time < today_start)，则清理
    # 为了简化，我们只清理明确标记为 Discarded 的
    final_list = []
    for item in new_list:
        if item.get('strategy_type') == 'Discarded':
             # 如果是 Discarded，检查是否是今天生成的? 
             # 实际上用户问"什么时候彻底删除"，我们可以定义为: 每次分析前(clean_watchlist被调用时)
             # 如果状态是 Discarded，直接丢弃，不保留在列表中
             continue
        final_list.append(item)
    
    watchlist_data = final_list
    save_watchlist(watchlist_data)
    reload_watchlist_globals()

def reload_watchlist_globals():
    """重新加载全局变量"""
    global watchlist_data, watchlist_map, WATCH_LIST, favorites_data, favorites_map
    watchlist_data = load_watchlist()
    watchlist_map = {item['code']: item for item in watchlist_data}
    
    favorites_data = load_favorites()
    favorites_map = {item['code']: item for item in favorites_data}
    
    # WATCH_LIST includes both
    WATCH_LIST = list(set(list(watchlist_map.keys()) + list(favorites_map.keys())))

@app.get("/api/news_history")
async def get_news_history(user: models.User = Depends(check_data_permission)):
    """获取新闻历史记录"""
    history_file = DATA_DIR / "news_history.json"
    if history_file.exists():
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return {"status": "success", "data": data}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "success", "data": []}

# 全局变量
watchlist_data = load_watchlist()
watchlist_map = {item['code']: item for item in watchlist_data}
favorites_data = load_favorites()
favorites_map = {item['code']: item for item in favorites_data}
WATCH_LIST = list(set(list(watchlist_map.keys()) + list(favorites_map.keys())))
limit_up_pool_data = []
broken_limit_pool_data = []
intraday_pool_data = [] # New global for fast intraday pool
ANALYSIS_CACHE = {} # Cache for AI analysis results: {code: {content: str, timestamp: float}}

def load_analysis_cache():
    """Load AI analysis cache from disk"""
    global ANALYSIS_CACHE
    file_path = DATA_DIR / "analysis_cache.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                ANALYSIS_CACHE = json.load(f)
        except:
            pass

def save_analysis_cache():
    """Save AI analysis cache to disk"""
    try:
        file_path = DATA_DIR / "analysis_cache.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(ANALYSIS_CACHE, f, ensure_ascii=False)
    except:
        pass

def cleanup_analysis_cache(max_age_days=7):
    """清理超过指定天数的分析缓存"""
    global ANALYSIS_CACHE
    now = time.time()
    max_age_seconds = max_age_days * 86400
    initial_count = len(ANALYSIS_CACHE)
    
    ANALYSIS_CACHE = {
        k: v for k, v in ANALYSIS_CACHE.items() 
        if now - v.get('timestamp', 0) < max_age_seconds
    }
    
    if len(ANALYSIS_CACHE) < initial_count:
        save_analysis_cache()
        print(f"Cleanup: Removed {initial_count - len(ANALYSIS_CACHE)} expired analysis cache entries.")

async def update_intraday_pool():
    global intraday_pool_data
    # ... (Implementation of scan)
    pass 
    # Placeholder, actual logic is in endpoints or separate scanner calls

@app.get("/api/status")
async def get_status():
    return {
        "status": "success",
        "is_trading": is_trading_time(),
        "is_market_open_day": is_market_open_day(),
        "server_time": datetime.now().strftime("%H:%M:%S"),
        "server_version": SERVER_VERSION
    }

@app.get("/api/add_watchlist")
async def add_to_watchlist_api(code: str, name: str, reason: str = "手动添加", user: models.User = Depends(check_data_permission)):
    global favorites_data, watchlist_map
    
    # Check if exists in favorites
    for item in favorites_data:
        if item['code'] == code:
            return {"status": "exists", "msg": "已在自选列表中"}
            
    # Try to preserve existing info if it was in AI list
    existing_info = watchlist_map.get(code, {})
    
    concept = existing_info.get("concept", "")
    # If concept is missing, try to fetch it
    if not concept:
        try:
            info = await asyncio.to_thread(data_provider.fetch_stock_info, code)
            concept = info.get('concept', '')
        except:
            pass

    new_item = {
        "code": code,
        "name": name,
        "reason": reason,
        "strategy_type": "Manual",
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "concept": concept,
        "initial_score": existing_info.get("initial_score", 0)
    }
    
    # Insert at top
    favorites_data.insert(0, new_item)
    save_favorites(favorites_data)
    reload_watchlist_globals()
    
    return {"status": "ok", "msg": "添加成功"}

@app.get("/api/remove_watchlist")
async def remove_from_watchlist_api(code: str, user: models.User = Depends(check_data_permission)):
    global favorites_data, watchlist_data
    
    removed = False
    
    # Remove from favorites
    original_len = len(favorites_data)
    favorites_data = [item for item in favorites_data if item['code'] != code]
    if len(favorites_data) < original_len:
        save_favorites(favorites_data)
        removed = True
        
    # Remove from AI watchlist
    original_len_w = len(watchlist_data)
    watchlist_data = [item for item in watchlist_data if item['code'] != code]
    if len(watchlist_data) < original_len_w:
        save_watchlist(watchlist_data)
        removed = True
    
    if removed:
        reload_watchlist_globals()
        return {"status": "ok", "msg": "删除成功"}
        
    return {"status": "error", "msg": "未找到该股票"}


def load_market_pools():
    """Load market pools from disk"""
    global limit_up_pool_data, broken_limit_pool_data, intraday_pool_data
    file_path = DATA_DIR / "market_pools.json"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                limit_up_pool_data = data.get("limit_up", [])
                broken_limit_pool_data = data.get("broken", [])
                intraday_pool_data = data.get("intraday", [])
        except:
            pass

def save_market_pools():
    """Save market pools to disk"""
    try:
        file_path = DATA_DIR / "market_pools.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump({
                "limit_up": limit_up_pool_data,
                "broken": broken_limit_pool_data,
                "intraday": intraday_pool_data
            }, f, ensure_ascii=False)
    except:
        pass

# Load caches on startup
load_market_pools()
load_analysis_cache()

from app.core.stock_utils import calculate_metrics, is_trading_time, is_market_open_day

async def update_market_pools_task():
    global limit_up_pool_data, broken_limit_pool_data
    loop = asyncio.get_event_loop()
    while True:
        try:
            # 1. Limit Up Pool
            new_limit_up = await loop.run_in_executor(None, scan_limit_up_pool)
            if new_limit_up is not None: 
                 # Enrich with metrics immediately to prevent flickering
                enriched_pool = []
                for stock in new_limit_up:
                    try:
                        code = stock['code']
                        # Calculate metrics
                        metrics = calculate_metrics(code)
                        stock.update(metrics) # Merges seal_rate, broken_rate, etc.
                        
                        # Enrich reason/info from Watchlist if available
                        if code in watchlist_map:
                            wl_item = watchlist_map[code]
                            # Priority: Manual Reason > AI Reason > Scanner Reason
                            # Note: scanner uses 'reason' for "X连板"
                            # If manual/AI reason exists, maybe we append or replace? 
                            # User wants "reason" visible.
                            ai_reason = wl_item.get('reason') or wl_item.get('news_summary')
                            if ai_reason:
                                stock['reason'] = f"{stock.get('reason','')} | {ai_reason}"
                                stock['news_summary'] = ai_reason
                        
                        enriched_pool.append(stock)
                    except Exception as e:
                        print(f"Error enriching stock {stock.get('code')}: {e}")
                        enriched_pool.append(stock) # Add anyway
                
                limit_up_pool_data = enriched_pool
            
            # 2. Broken Pool
            new_broken = await loop.run_in_executor(None, scan_broken_limit_pool)
            if new_broken is not None:
                broken_limit_pool_data = new_broken
                
            await loop.run_in_executor(None, save_market_pools)
        except Exception as e:
            print(f"Pool update error: {e}")
        
        await asyncio.sleep(20) # Increase interval slightly to allow for metrics calc

async def update_intraday_pool_task():
    """Fast loop for intraday scanner"""
    global intraday_pool_data, limit_up_pool_data, watchlist_data, watchlist_map, WATCH_LIST
    from app.core.market_scanner import scan_intraday_limit_up
    loop = asyncio.get_event_loop()
    while True:
        try:
            # Only run during trading hours (approx) and weekdays
            now = datetime.now()
            if now.weekday() < 5 and 9 <= now.hour < 15:
                result = await loop.run_in_executor(None, scan_intraday_limit_up)
                if result:
                    intraday_stocks, sealed_stocks = result
                    intraday_pool_data = intraday_stocks
                    
                    # [Fix] 合并到关注列表，确保它们出现在主表且不会因为涨速下降而消失
                    changed = False
                    for s in intraday_stocks:
                        if s['code'] not in watchlist_map:
                            new_item = {
                                "code": s['code'],
                                "name": s['name'],
                                "concept": s['concept'],
                                "news_summary": s['reason'], # 统一使用 news_summary
                                "strategy_type": "LimitUp",
                                "added_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "initial_score": s.get('score', 0)
                            }
                            watchlist_data.append(new_item)
                            watchlist_map[s['code']] = new_item
                            changed = True
                    
                    if changed:
                        # 更新全局监控列表并保存
                        WATCH_LIST = list(set(list(watchlist_map.keys()) + list(favorites_map.keys())))
                        await loop.run_in_executor(None, save_watchlist, watchlist_data)
                    
                    # [New] 竞价列表清理逻辑 (10:00 后清理竞价策略股票)
                    if now.hour >= 10:
                        cleanup_changed = False
                        sealed_codes = {s['code'] for s in limit_up_pool_data}
                        for item in watchlist_data:
                            if item.get('strategy_type') == 'Aggressive' and '已剔除' not in item.get('news_summary', ''):
                                if item['code'] not in sealed_codes:
                                    item['strategy_type'] = 'Discarded'
                                    item['news_summary'] = f"[竞价过期] {item.get('news_summary', '')}"
                                    cleanup_changed = True
                        
                        if cleanup_changed:
                            await loop.run_in_executor(None, save_watchlist, watchlist_data)
                    
                    # Merge sealed stocks into limit_up_pool_data if not already present
                    if sealed_stocks:
                        existing_codes = {s['code'] for s in limit_up_pool_data}
                        for s in sealed_stocks:
                            if s['code'] not in existing_codes:
                                limit_up_pool_data.append(s)
                                existing_codes.add(s['code'])
            
            # Normal sleep
            await asyncio.sleep(10)
            
        except Exception as e:
            print(f"Intraday scan error: {e}")
            # Sleep longer on error to avoid hammering
            await asyncio.sleep(60)

if not WATCH_LIST:
    WATCH_LIST = ['sh600519', 'sz002405', 'sz300059']

def refresh_watchlist():
    """刷新全局监控列表"""
    global watchlist_data, watchlist_map, WATCH_LIST
    watchlist_data = load_watchlist()
    watchlist_map = {item['code']: item for item in watchlist_data}
    WATCH_LIST = list(watchlist_map.keys())
    if not WATCH_LIST:
        WATCH_LIST = ['sh600519', 'sz002405', 'sz300059']

@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    channel = (websocket.query_params.get("channel") or "logs").strip().lower()
    device_id = (websocket.query_params.get("device_id") or "").strip()
    if channel not in ("logs", "notify"):
        channel = "logs"
    if channel == "notify" and not device_id:
        channel = "logs"

    await ws_hub.register(websocket, channel=channel, device_id=device_id)
    try:
        while True:
            await websocket.receive_text()  # Keep connection open
    except WebSocketDisconnect:
        await ws_hub.unregister(websocket, channel=channel, device_id=device_id)

@app.get("/api/search")
async def search_stock(q: str, user: models.User = Depends(check_data_permission)):
    """
    搜索股票 (支持代码、拼音、名称)
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: data_provider.search_stock(q))


def normalize_stock_code(code: str):
    if not code:
        return ""
    raw = code.strip().lower()
    if raw.startswith("sh") or raw.startswith("sz") or raw.startswith("bj"):
        return raw
    if len(raw) == 6 and raw.isdigit():
        if raw.startswith("6"):
            return f"sh{raw}"
        if raw.startswith("0") or raw.startswith("3"):
            return f"sz{raw}"
        if raw.startswith("8") or raw.startswith("4") or raw.startswith("9"):
            return f"bj{raw}"
    return raw


class FavoriteStatRequest(BaseModel):
    code: str


@app.get("/api/favorites/quotes")
async def api_favorite_quotes(codes: str = "", user: models.User = Depends(check_data_permission)):
    code_list = [normalize_stock_code(c) for c in codes.split(",") if c.strip()]
    code_list = [c for c in code_list if c]
    if not code_list:
        return []

    unique_codes = list(dict.fromkeys(code_list))
    try:
        raw_stocks = data_provider.fetch_quotes(unique_codes)
    except Exception:
        return []

    enriched = []
    for stock in raw_stocks:
        code = stock.get("code", "")
        if not code:
            continue
        metrics = calculate_metrics(code)
        stock.update(metrics)
        stock["is_favorite"] = True
        stock["strategy"] = "Manual"
        stock["news_summary"] = stock.get("news_summary") or "本地自选"
        enriched.append(stock)
    return enriched


@app.post("/api/watchlist/stat/add")
async def add_watchlist_stat(payload: FavoriteStatRequest, user: models.User = Depends(get_current_user)):
    code = normalize_stock_code(payload.code)
    if not code:
        return {"status": "error", "message": "Invalid code"}
    watchlist_stats.add_favorite_stat(str(user.id), code)
    return {"status": "success"}


@app.post("/api/watchlist/stat/remove")
async def remove_watchlist_stat(payload: FavoriteStatRequest, user: models.User = Depends(get_current_user)):
    code = normalize_stock_code(payload.code)
    if not code:
        return {"status": "error", "message": "Invalid code"}
    watchlist_stats.remove_favorite_stat(str(user.id), code)
    return {"status": "success"}

@app.post("/api/add_stock")
async def add_stock(code: str, user: models.User = Depends(check_data_permission)):
    """手动添加股票到监控列表"""
    global watchlist_data, watchlist_map, WATCH_LIST
    
    code = code.lower().strip()
    
    # 自动补全前缀
    if len(code) == 6 and code.isdigit():
        if code.startswith('6'):
            code = f"sh{code}"
        elif code.startswith('0') or code.startswith('3'):
            code = f"sz{code}"
        elif code.startswith('8') or code.startswith('4') or code.startswith('9'):
            code = f"bj{code}"
        else:
            # 默认为 sh (或者报错)
            pass
    
    # 简单的格式校验
    if not (code.startswith('sh') or code.startswith('sz') or code.startswith('bj')):
        return {"status": "error", "message": "Invalid code format"}
        
    # 如果已存在，强制更新为 Manual 策略
    if code in watchlist_map:
        watchlist_map[code]['strategy_type'] = 'Manual'
        watchlist_map[code]['news_summary'] = '手动添加 (覆盖)'
        # Save
        try:
            file_path = DATA_DIR / "watchlist.json"
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(watchlist_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving watchlist: {e}")
        return {"status": "success", "message": "Updated to Manual"}
        
    # 计算高级指标
    metrics = calculate_metrics(code)
    
    # 获取股票详细信息 (名称 + 行业/概念)
    name, concept = data_provider.get_stock_info(code)
    
    # 添加新股票
    new_item = {
        "code": code,
        "name": name, 
        "news_summary": "Manual Add",
        "concept": concept,
        "initial_score": 5, # 默认中等分数
        "strategy_type": "Manual",
        "seal_rate": metrics['seal_rate'],
        "broken_rate": metrics['broken_rate'],
        "next_day_premium": metrics['next_day_premium'],
        "limit_up_days": metrics['limit_up_days'],
        "added_time": time.time() # Record add time for sorting
    }
    
    # Check if exists to update instead of append (though we handled map above, list needs care)
    existing_idx = -1
    for i, item in enumerate(watchlist_data):
        if item['code'] == code:
            existing_idx = i
            break
            
    if existing_idx >= 0:
        watchlist_data[existing_idx] = new_item
    else:
        watchlist_data.insert(0, new_item) # Prepend to list
        
    watchlist_map[code] = new_item
    if code not in WATCH_LIST:
        WATCH_LIST.append(code)
        
    # 保存到文件
    try:
        file_path = DATA_DIR / "watchlist.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(watchlist_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving watchlist: {e}")
        
    return {"status": "success"}

# 使用全局 Queue 来传递日志
import queue
log_queue = queue.Queue()

async def log_broadcaster():
    """从队列读取日志并广播"""
    while True:
        try:
            # 非阻塞获取
            msg = log_queue.get_nowait()
            await ws_hub.broadcast_log(msg)
        except queue.Empty:
            await asyncio.sleep(0.1)

def update_limit_up_pool_task():
    """更新已涨停股票池"""
    global limit_up_pool_data
    try:
        # Scan
        pool = scan_limit_up_pool()
        
        enriched_pool = []
        for stock in pool:
            code = stock['code']
            
            # Calculate metrics (Historical)
            metrics = calculate_metrics(code)
            
            # Check watchlist for reason
            reason = "市场强势涨停"
            if code in watchlist_map:
                reason = watchlist_map[code].get('news_summary', '自选股涨停')
            
            stock['seal_rate'] = metrics['seal_rate']
            stock['broken_rate'] = metrics['broken_rate']
            stock['next_day_premium'] = metrics['next_day_premium']
            stock['limit_up_days'] = metrics['limit_up_days']
            stock['reason'] = reason
            # Ensure concept and associated are present (from scanner)
            if 'concept' not in stock:
                stock['concept'] = '-'
            if 'associated' not in stock:
                stock['associated'] = stock.get('concept', '-')
            
            enriched_pool.append(stock)
            
        limit_up_pool_data = enriched_pool
    except Exception as e:
        print(f"Error updating limit up pool: {e}")

@app.on_event("startup")
async def startup_event():
    # Load caches
    load_analysis_cache()
    
    # Update base info (CircMV etc) on startup
    print("Startup: Updating base stock info...")
    add_runtime_log("Startup: Updating base stock info...")
    await asyncio.to_thread(data_provider.update_base_info)
    
    asyncio.create_task(log_broadcaster())
    # Start background scheduler
    asyncio.create_task(scheduler_loop())
    # Start market pool updater
    asyncio.create_task(update_market_pools_task())
    # Start fast intraday scanner
    asyncio.create_task(update_intraday_pool_task())
    # Start periodic cleanup task
    asyncio.create_task(periodic_cleanup_task())
    
    # 启动时立即执行一次盘中扫描，确保列表不为空
    print("Startup: Running initial intraday scan...")
    add_runtime_log("Startup: Running initial intraday scan...")
    asyncio.create_task(run_initial_scan())

async def periodic_cleanup_task():
    """定期清理缓存文件"""
    while True:
        try:
            print("Running periodic cleanup...")
            # 1. 清理 AI 分析缓存 (7天)
            cleanup_analysis_cache(max_age_days=7)
            # 2. 清理 AI 原始数据缓存 (7天)
            ai_cache.cleanup(max_age_seconds=7 * 86400)
            
            # 每 24 小时运行一次
            await asyncio.sleep(86400)
        except Exception as e:
            print(f"Cleanup task error: {e}")
            await asyncio.sleep(3600)

async def run_initial_scan():
    """启动时立即运行一次扫描"""
    try:
        # 等待几秒确保其他组件就绪
        await asyncio.sleep(2)
        # 仅在交易日且配置开启时执行初始扫描
        if is_market_open_day() and SYSTEM_CONFIG["auto_analysis_enabled"]:
            await asyncio.to_thread(execute_analysis, "intraday")
            print("Startup: Initial scan completed.")
            # Update last run time to prevent immediate re-run by scheduler
            SYSTEM_CONFIG["last_run_time"] = time.time()
        else:
            print("启动: 跳过初始扫描 (非交易日或已禁用).")
    except Exception as e:
        print(f"初始扫描错误: {e}")

@app.get("/api/config")
async def get_config(user: models.User = Depends(check_data_permission)):
    return SYSTEM_CONFIG

class ConfigUpdate(BaseModel):
    auto_analysis_enabled: bool
    use_smart_schedule: bool
    fixed_interval_minutes: int
    schedule_plan: Optional[List[dict]] = None
    news_auto_clean_enabled: Optional[bool] = True
    news_auto_clean_days: Optional[int] = 14

@app.post("/api/config")
async def update_config(config: ConfigUpdate, user: models.User = Depends(check_data_permission)):
    global SYSTEM_CONFIG
    SYSTEM_CONFIG["auto_analysis_enabled"] = config.auto_analysis_enabled
    SYSTEM_CONFIG["use_smart_schedule"] = config.use_smart_schedule
    SYSTEM_CONFIG["fixed_interval_minutes"] = config.fixed_interval_minutes
    if config.schedule_plan:
        SYSTEM_CONFIG["schedule_plan"] = config.schedule_plan
    
    if config.news_auto_clean_enabled is not None:
        SYSTEM_CONFIG["news_auto_clean_enabled"] = config.news_auto_clean_enabled
    if config.news_auto_clean_days is not None:
        SYSTEM_CONFIG["news_auto_clean_days"] = config.news_auto_clean_days
    
    # [Fix] Reset last_run_time to now to prevent immediate scan if interval was reduced
    # This ensures the next scan happens AFTER the interval, not immediately.
    SYSTEM_CONFIG["last_run_time"] = time.time()
    
    save_config() # Persist changes
    return {"status": "success", "config": SYSTEM_CONFIG}

async def scheduler_loop():
    """Background scheduler for periodic tasks"""
    print("Starting background scheduler...")
    last_pool_update_time = 0
    
    # Startup Check: If watchlist was updated recently (< 1 hour), skip immediate analysis
    # Check file modification time of watchlist.json
    try:
        watchlist_path = DATA_DIR / "watchlist.json"
        if watchlist_path.exists():
            mtime = watchlist_path.stat().st_mtime
            if time.time() - mtime < 3600:
                print("Watchlist updated recently (<1h), skipping immediate analysis on startup.")
                # Set last_run_time to mtime so scheduler thinks it just ran
                SYSTEM_CONFIG["last_run_time"] = mtime
    except Exception as e:
        print(f"Startup check failed: {e}")

    while True:
        try:
            current_timestamp = time.time()
            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute
            
            # Weekend Check (Saturday=5, Sunday=6)
            if not is_market_open_day():
                # Sleep longer on weekends
                await asyncio.sleep(3600)
                continue
                
            # --- Schedule Logic ---
            interval_seconds = 3600 # Default 1h
            lookback_hours = 1
            mode = "after_hours"
            
            # Reset active rule index
            SYSTEM_CONFIG["active_rule_index"] = -1
            
            if SYSTEM_CONFIG["use_smart_schedule"]:
                current_time_str = now.strftime("%H:%M")
                matched_rule = None
                
                # Default fallback
                interval_seconds = 3600
                mode = "after_hours"

                for index, rule in enumerate(SYSTEM_CONFIG.get("schedule_plan", DEFAULT_SCHEDULE)):
                    start = rule["start"]
                    end = rule["end"]
                    
                    # Check if time is in range
                    in_range = False
                    if start <= end:
                        if start <= current_time_str < end:
                            in_range = True
                    else: # Cross midnight (e.g. 23:00 to 06:00)
                        if start <= current_time_str or current_time_str < end:
                            in_range = True
                            
                    if in_range:
                        matched_rule = rule
                        SYSTEM_CONFIG["active_rule_index"] = index
                        break
                
                if matched_rule:
                    interval_seconds = matched_rule["interval"] * 60
                    mode = matched_rule["mode"]
                    if matched_rule["mode"] == "none":
                        interval_seconds = 999999
                
                lookback_hours = max(0.25, interval_seconds / 3600)

                # Special Trigger: Force run at 15:15 if last run was intraday (before 15:15)
                if current_hour == 15 and current_minute >= 15:
                    last_run_dt = datetime.fromtimestamp(SYSTEM_CONFIG["last_run_time"]) if SYSTEM_CONFIG["last_run_time"] > 0 else datetime.fromtimestamp(0)
                    # If last run was today but before 15:15
                    if last_run_dt.date() == now.date() and (last_run_dt.hour < 15 or (last_run_dt.hour == 15 and last_run_dt.minute < 15)):
                        interval_seconds = 0 # Force run
            else:
                # Manual Interval
                interval_seconds = SYSTEM_CONFIG["fixed_interval_minutes"] * 60
                lookback_hours = SYSTEM_CONFIG["fixed_interval_minutes"] / 60
                # Simple mode logic for manual
                if (current_hour > 9 or (current_hour == 9 and current_minute >= 30)) and current_hour < 15:
                    mode = "intraday"
                else:
                    mode = "after_hours"

            # Safety check: If last_run_time is in the future, reset it
            # [Moved here to ensure interval_seconds is defined]
            if SYSTEM_CONFIG["last_run_time"] > current_timestamp:
                print(f"Resetting future last_run_time: {SYSTEM_CONFIG['last_run_time']} -> {current_timestamp}")
                SYSTEM_CONFIG["last_run_time"] = current_timestamp - interval_seconds # Force run if needed

            # Update Next Run Time for UI
            # If we just ran (last_run_time is very close to now), next run is now + interval
            # If we haven't run in a while, next run is effectively "now" (pending execution)
            if SYSTEM_CONFIG["last_run_time"] == 0:
                SYSTEM_CONFIG["next_run_time"] = current_timestamp
            else:
                # Calculate next run based on last run
                next_run = SYSTEM_CONFIG["last_run_time"] + interval_seconds
                # If next run is in the past (overdue), show it as now
                if next_run < current_timestamp:
                    SYSTEM_CONFIG["next_run_time"] = current_timestamp
                else:
                    SYSTEM_CONFIG["next_run_time"] = next_run

            # Task 1: Analysis
            if SYSTEM_CONFIG["auto_analysis_enabled"]:
                if current_timestamp - SYSTEM_CONFIG["last_run_time"] >= interval_seconds:
                    # Special check to avoid running during the 15:00-15:15 gap (only in smart mode)
                    should_run = True
                    if SYSTEM_CONFIG["use_smart_schedule"] and (current_hour == 15 and current_minute < 15):
                        should_run = False
                    
                    if should_run:
                        try:
                            mode_cn = "盘后复盘" if mode == "after_hours" else "盘中突击"
                            SYSTEM_CONFIG["current_status"] = f"正在运行 {mode_cn}..."
                            # Update last_run_time BEFORE execution to prevent loop on error
                            SYSTEM_CONFIG["last_run_time"] = current_timestamp
                            
                            # Recalculate next run time immediately after update
                            SYSTEM_CONFIG["next_run_time"] = current_timestamp + interval_seconds
                            
                            thread_logger(f">>> 触发定时分析: {mode}, 周期{interval_seconds/60:.0f}分, 回溯{lookback_hours}小时")
                            await asyncio.to_thread(execute_analysis, mode, lookback_hours)
                        except Exception as e:
                            print(f"Scheduler error: {e}")
                        finally:
                            SYSTEM_CONFIG["current_status"] = "空闲中"
            else:
                SYSTEM_CONFIG["current_status"] = "已暂停"
            
            # Task 2: Refresh Quotes (Every 3 seconds)
            # Only during trading hours or shortly after
            if now.weekday() < 5 and (9 <= current_hour < 16):
                try:
                    stocks = await asyncio.to_thread(get_stock_quotes)
                except Exception as e:
                    pass
                
            # Task 3: Update Limit Up Pool (Every 30 seconds)
            # [Fix] Disabled to avoid conflict with update_market_pools_task
            # if current_timestamp - last_pool_update_time >= 30:
            #     # Only during trading hours
            #     if is_trading_time():
            #         await asyncio.to_thread(update_limit_up_pool_task)
            #         last_pool_update_time = current_timestamp

            # Task 4: LHB Sync (Daily at 18:00)
            if is_market_open_day() and now.hour == 18 and now.minute == 0 and now.second < 10:
                if lhb_manager.config['enabled'] and not lhb_manager.is_syncing:
                    # [Modified] Only run if today's data is missing
                    if not lhb_manager.has_data_for_today():
                        thread_logger(f"[LHB] 启动定时同步任务 (18:00)...")
                        loop = asyncio.get_event_loop()
                        loop.run_in_executor(None, lhb_manager.fetch_and_update_data, thread_logger)
                    else:
                        thread_logger(f"[LHB] 今日数据已存在，跳过定时任务。")
                    # Sleep to avoid multiple triggers
                    await asyncio.sleep(60)

            await asyncio.sleep(5) # Check every 5 seconds
            
        except Exception as e:
            print(f"Scheduler loop crashed: {e}")
            await asyncio.sleep(60) # Sleep and retry

def thread_logger(msg):
    """线程安全的 logger"""
    add_runtime_log(msg)
    log_queue.put(msg)

# Global Cache Timer
LAST_ANALYSIS_TIME = {
    "mid_day": datetime.min,
    "after_hours": datetime.min
}
ANALYSIS_TRIGGER_LOCKS = {
    "mid_day": asyncio.Lock(),
    "after_hours": asyncio.Lock()
}

@app.post("/api/analyze")
async def run_analysis(
    background_tasks: BackgroundTasks, 
    mode: str = Query("after_hours"), 
    user: models.User = Depends(get_current_user), 
    db: Session = Depends(lambda: next(database.get_db()))
):
    """触发复盘分析"""
    
    # 1. 权限检查 & 扣费
    limit_type = 'raid' if mode in ["intraday", "intraday_monitor"] else 'review'
    
    if limit_type == 'raid':
        await check_raid_permission(user, skip_quota=True)
    else:
        await check_review_permission(user, skip_quota=True)
    
    # 2. 缓存检查 (5分钟) + 并发互斥，避免重复扣费和重复触发
    cache_key = "mid_day" if limit_type == 'raid' else "after_hours"
    lock = ANALYSIS_TRIGGER_LOCKS[cache_key]
    async with lock:
        last_time = LAST_ANALYSIS_TIME.get(cache_key, datetime.min)
        seconds_since_last = int((datetime.now() - last_time).total_seconds())
        if seconds_since_last < 300:
            thread_logger(f"[Cache] {mode} 5分钟缓存命中，复用最近结果（{seconds_since_last}s 前）")
            return {
                "status": "success",
                "cached": True,
                "seconds_since_last": seconds_since_last,
                "message": f"Returning cached {mode} data (updated {seconds_since_last}s ago)"
            }

        # 3. 执行新的分析
        if limit_type == 'raid':
            await check_raid_permission(user)
        else:
            await check_review_permission(user)
        user_service.consume_quota(db, user, limit_type)
        LAST_ANALYSIS_TIME[cache_key] = datetime.now()
    
    # Run in background to avoid blocking
    background_tasks.add_task(execute_analysis, mode)
    
    return {"status": "success", "cached": False, "message": f"{mode} analysis started in background"}

def execute_analysis(mode="after_hours", hours=None):
    try:
        mode_name = "盘后复盘" if mode == "after_hours" else "盘中突击"
        thread_logger(f">>> 开始执行{mode_name}任务 (回溯{hours if hours else '默认'}小时)...")
        
        # Clean watchlist before analysis (remove old/irrelevant)
        clean_watchlist()
        
        generate_watchlist(logger=thread_logger, mode=mode, hours=hours, update_callback=refresh_watchlist)
        refresh_watchlist()
        # Reload globals so /api/stocks returns new data
        reload_watchlist_globals()
        thread_logger(f">>> {mode_name}任务完成，列表已更新 ({len(WATCH_LIST)} 个标的)。")
    except Exception as e:
        thread_logger(f"!!! 分析任务出错: {e}")
        print(f"Analysis Error: {e}")


def get_stock_quotes():
    """
    获取股票行情，使用统一的 DataProvider
    """
    if not WATCH_LIST:
        return []
        
    try:
        # Fetch raw quotes
        raw_stocks = data_provider.fetch_quotes(WATCH_LIST)
        
        # Build quick maps for enrichment: /api/stocks should inherit seats/flow value from pool scanners.
        limit_up_map = {s.get("code"): s for s in (limit_up_pool_data or []) if s.get("code")}
        intraday_map = {s.get("code"): s for s in (intraday_pool_data or []) if s.get("code")}
        market_map = {}
        try:
            market_df = data_provider.fetch_all_market_data()
            if market_df is not None and not market_df.empty:
                for _, row in market_df.iterrows():
                    mcode = str(row.get("code", "")).strip()
                    if not mcode:
                        continue
                    try:
                        market_map[mcode] = float(row.get("circ_mv", 0) or 0)
                    except Exception:
                        market_map[mcode] = 0.0
        except Exception:
            market_map = {}
        
        # Enrich with strategy info
        enriched_stocks = []
        for stock in raw_stocks:
            code = stock['code']
            
            stock['is_favorite'] = False
            ai_strategy = "Neutral"
            
            # 1. Check AI Watchlist for strategy & info
            if code in watchlist_map:
                ai_info = watchlist_map[code]
                ai_strategy = ai_info.get("strategy_type", "Neutral")
                
                # Use AI info as base
                stock['initial_score'] = ai_info.get("initial_score", 0)
                stock['concept'] = ai_info.get("concept", stock.get('concept', '-'))
                # 兼容 reason 和 news_summary
                ai_curr_reason = ai_info.get("reason", ai_info.get("news_summary", ""))
                stock['reason'] = ai_curr_reason if ai_curr_reason else stock.get('reason', '')
                stock['news_summary'] = stock['reason'] # 确保前端能读取到详细逻辑
                
                stock['seal_rate'] = ai_info.get("seal_rate", 0)
                stock['broken_rate'] = ai_info.get("broken_rate", 0)
                stock['next_day_premium'] = ai_info.get("next_day_premium", 0)
                stock['limit_up_days'] = ai_info.get("limit_up_days", 0)
                stock['added_time'] = ai_info.get("added_time", 0)

                # Check for "Resurrection" (Weak to Strong)
                if ai_strategy == "Discarded":
                    # Determine limit threshold (10% or 20%)
                    clean_code = code.replace('sz', '').replace('sh', '')
                    is_20cm = clean_code.startswith('30') or clean_code.startswith('68')
                    limit_threshold = 19.5 if is_20cm else 9.5
                    
                    current_change = stock.get('change_percent', 0)
                    
                    if current_change >= limit_threshold:
                        ai_strategy = "LimitUp" # Promote to LimitUp view
                        # Prepend reason if not already there
                        if "[弱转强]" not in stock['reason']:
                            stock['reason'] = f"[弱转强] {stock['reason']}"

            # 2. Check Favorites
            if code in favorites_map:
                fav_info = favorites_map[code]
                stock['is_favorite'] = True
                
                # If NOT in AI list, use Favorite info
                if code not in watchlist_map:
                    stock['concept'] = fav_info.get("concept", stock.get('concept', '-'))
                    stock['reason'] = fav_info.get("reason", "手动添加")
                    stock['initial_score'] = fav_info.get("initial_score", 0)
                    stock['added_time'] = fav_info.get("added_time", 0)
                    # Keep other metrics 0 or default
            
            # Set strategy to AI strategy (so it appears in AI columns)
            # Frontend will handle 'is_favorite' for Manual column
            stock['strategy'] = ai_strategy

            # Enrich likely seats and circulation value from intraday / limit-up pools.
            seat_src = intraday_map.get(code) or limit_up_map.get(code)
            if seat_src:
                if seat_src.get("likely_seats"):
                    stock["likely_seats"] = seat_src.get("likely_seats")
                if (not stock.get("circulation_value")) and seat_src.get("circulation_value"):
                    stock["circulation_value"] = seat_src.get("circulation_value")
                if not stock.get("concept") and seat_src.get("concept"):
                    stock["concept"] = seat_src.get("concept")

            # Final fallback for circulation value from all-market snapshot.
            if not stock.get("circulation_value"):
                circ_mv = market_map.get(code, 0)
                if circ_mv:
                    stock["circulation_value"] = circ_mv
            
            enriched_stocks.append(stock)
            
        return enriched_stocks
    except Exception as e:
        print(f"Error fetching quotes: {e}")
        return []

@app.post("/api/watchlist/remove")
async def remove_from_watchlist(request: Request, user: models.User = Depends(check_data_permission)):
    """从自选列表中移除股票"""
    global watchlist_data, watchlist_map, WATCH_LIST, favorites_data, favorites_map
    try:
        data = await request.json()
        code = data.get("code")
        list_type = data.get("type", "all") # 'favorite', 'ai', or 'all' (default for backward compat)
        
        removed = False
        if code:
            # Remove from favorites
            if (list_type == 'favorite' or list_type == 'all') and code in favorites_map:
                favorites_data = [item for item in favorites_data if item['code'] != code]
                save_favorites(favorites_data)
                removed = True
                
            # Remove from watchlist
            if (list_type == 'ai' or list_type == 'all') and code in watchlist_map:
                watchlist_data = [item for item in watchlist_data if item['code'] != code]
                save_watchlist(watchlist_data)
                removed = True
                
            if removed:
                reload_watchlist_globals()
                return {"status": "success", "message": f"Removed {code}"}
                
        return {"status": "error", "message": "Stock not found"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/stocks")
async def api_stocks(user: models.User = Depends(check_data_permission)):
    return get_stock_quotes()

@app.get("/api/indices")
async def api_indices(user: models.User = Depends(check_data_permission)):
    """快速获取大盘指数"""
    return data_provider.fetch_indices()

@app.get("/api/limit_up_pool")
async def api_limit_up_pool(user: models.User = Depends(check_data_permission)):
    return {
        "limit_up": limit_up_pool_data,
        "broken": broken_limit_pool_data
    }

@app.get("/api/intraday_pool")
async def api_intraday_pool(user: models.User = Depends(check_data_permission)):
    """直接获取盘中打板扫描结果 (优先返回缓存)"""
    global intraday_pool_data
    if intraday_pool_data:
        return intraday_pool_data
        
    # Fallback if empty (e.g. startup)
    from app.core.market_scanner import scan_intraday_limit_up
    loop = asyncio.get_event_loop()
    stocks = await loop.run_in_executor(None, scan_intraday_limit_up)
    if stocks:
        intraday_pool_data = stocks
    return stocks

@app.get("/api/market_sentiment")
async def api_market_sentiment(user: models.User = Depends(check_data_permission)):
    """获取大盘情绪数据"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_market_overview)

class StockAnalysisRequest(BaseModel):
    code: str
    name: str
    current: float
    change_percent: float
    concept: str = ""
    turnover: Optional[float] = None
    circulation_value: Optional[float] = None
    metrics: dict = {}
    promptType: str = "default"
    force: bool = False # Force re-analysis
    apiKey: Optional[str] = None # Optional API Key for standalone mode

@app.post("/api/analyze_stock")
async def api_analyze_stock(request: StockAnalysisRequest, user: models.User = Depends(get_current_user), db = Depends(lambda: next(database.get_db()))):
    """
    调用AI分析单个股票 (支持缓存)
    """
    await check_ai_permission(user)
    user_service.consume_quota(db, user, 'ai')
    stock_data = request.dict()
    api_key = stock_data.get('apiKey')
    code = stock_data.get('code')
    force = stock_data.get('force', False)
    prompt_type = request.promptType
    
    # Construct composite cache key
    cache_key = f"{code}_{prompt_type}"
    
    # Check Cache
    if not force and cache_key in ANALYSIS_CACHE:
        cache_entry = ANALYSIS_CACHE[cache_key]
        cache_time = datetime.fromtimestamp(cache_entry['timestamp'])
        now = datetime.now()
        
        # Expiry Logic
        is_expired = False
        
        # 1. Basic Date Check: If cache is from yesterday, it's expired
        if cache_time.date() < now.date():
            is_expired = True
        
        # 2. Specific Logic based on promptType
        if not is_expired:
            if prompt_type == 'min_trading_signal':
                # Minute analysis: Expire if new trading day starts (e.g. after 9:30)
                # If cache is from before 9:30 today, and now is after 9:30, expire it?
                # Or simply: Minute analysis is valid for the day, but if we are in a new day (handled by date check), it's gone.
                # User requirement: "分时的分析新的交易日开盘后可以删除"
                # This implies if I analyze at 10:00, and now it's 11:00, it's still valid.
                # But if I analyze yesterday, it's invalid (handled by date).
                # What if I analyze at 9:00 (pre-market)? It might be invalid after 9:30.
                if cache_time.hour < 9 and cache_time.minute < 30 and (now.hour > 9 or (now.hour == 9 and now.minute >= 30)):
                    is_expired = True
            
            elif prompt_type == 'day_trading_signal':
                # Day analysis: "Wait until trading day is no longer visible" -> Keep for the whole day.
                # Do NOT expire at 15:00.
                pass
            
            else:
                # Default logic for other types: Expire at 15:00
                if cache_time.hour < 15 and now.hour >= 15:
                    is_expired = True
            
        if not is_expired:
            return {"status": "success", "analysis": cache_entry['content'], "cached": True}

    loop = asyncio.get_event_loop()
    # Pass promptType explicitly or let analyze_single_stock handle it from stock_data
    # Pass api_key if provided
    result = await loop.run_in_executor(None, lambda: analyze_single_stock(stock_data, prompt_type=prompt_type, api_key=api_key))
    
    # Update Cache
    if result and not result.startswith("分析失败"):
        ANALYSIS_CACHE[cache_key] = {
            "content": result,
            "timestamp": time.time()
        }
        # Persist cache to disk
        save_analysis_cache()
        
    return {"status": "success", "analysis": result, "cached": False}

# --- LHB API ---
class LHBConfigRequest(BaseModel):
    enabled: bool
    days: int
    min_amount: int

@app.get("/api/lhb/config")
async def get_lhb_config(user: models.User = Depends(check_data_permission)):
    lhb_manager.load_config()
    return lhb_manager.config

@app.post("/api/lhb/config")
async def update_lhb_config(config: LHBConfigRequest, user: models.User = Depends(check_data_permission)):
    lhb_manager.update_settings(config.enabled, config.days, config.min_amount)
    return {"status": "ok", "config": lhb_manager.config}

@app.post("/api/lhb/sync")
async def sync_lhb_data(background_tasks: BackgroundTasks, days: Optional[int] = Query(None), user: models.User = Depends(check_data_permission)):
    """Trigger LHB sync in background"""
    if lhb_manager.is_syncing:
        return {"status": "error", "message": "同步任务正在进行中"}
        
    background_tasks.add_task(lhb_manager.fetch_and_update_data, logger=thread_logger, force_days=days)
    return {"status": "ok", "message": "龙虎榜同步已启动"}

@app.get("/api/lhb/status")
async def get_lhb_status(user: models.User = Depends(check_data_permission)):
    return {"is_syncing": lhb_manager.is_syncing}

@app.get("/api/lhb/dates")
async def get_lhb_dates(user: models.User = Depends(check_data_permission)):
    return lhb_manager.get_available_dates()

@app.get("/api/lhb/history")
async def get_lhb_history(date: str, user: models.User = Depends(check_data_permission)):
    return lhb_manager.get_daily_data(date)

class LHBAnalyzeRequest(BaseModel):
    date: str
    force: bool = False

@app.post("/api/lhb/analyze")
async def analyze_lhb_daily_api(req: LHBAnalyzeRequest, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Run in thread pool
    await check_review_permission(user)
    user_service.consume_quota(db, user, 'review')
    
    loop = asyncio.get_event_loop()
    # Fetch data first
    data = lhb_manager.get_daily_data(req.date)
    result = await loop.run_in_executor(None, lambda: analyze_daily_lhb(req.date, data, force_update=req.force))
    return {"status": "ok", "result": result}

@app.get("/api/lhb/analysis")
async def get_lhb_analysis_api(date: str, user: models.User = Depends(get_current_user)):
    """获取已有的AI复盘结果 (如有)"""
    await check_review_permission(user)
    cache_key = f"lhb_daily_analysis_{date}"
    cached = ai_cache.get(cache_key)
    return {"status": "ok", "analysis": cached}

@app.get("/api/data/backup")
async def download_data_backup(user: models.User = Depends(check_data_permission)):
    """
    Pack 'data' directory into a zip file and return for download
    """
    try:
        # Create a zip file in a temporary location or memory?
        # shutil.make_archive creates a file on disk.
        # We can create it in parent dir, stream it, then delete it.
        
        base_name = BASE_DIR / "backup_data"
        root_dir = BASE_DIR / "data"
        
        if not root_dir.exists():
            return {"status": "error", "message": "Data directory not found"}
            
        # This creates backup_data.zip
        zip_path = shutil.make_archive(str(base_name), 'zip', str(root_dir))
        
        filename = f"sniper_data_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        
        def iterfile():
            with open(zip_path, mode="rb") as file_like:
                yield from file_like
            # Clean up after streaming?
            # StreamingResponse background task can handle cleanup
            os.remove(zip_path)

        return StreamingResponse(
            iterfile(),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Trigger reload for metrics update

class AnalyzeRequest(BaseModel):
    code: str
    name: str
    turnover: Optional[float] = None
    circulation_value: Optional[float] = None
    promptType: str = "normal"
    force: bool = False
    kline_data: Optional[List] = None

@app.post("/api/lhb/fetch")
async def fetch_lhb_data(background_tasks: BackgroundTasks, user: models.User = Depends(check_data_permission)):
    """手动触发龙虎榜数据抓取"""
    if lhb_manager.is_syncing:
        return {"status": "error", "message": "同步任务正在进行中，请稍后再试"}
    
    # [Modified] Use thread_logger to broadcast logs to WebSocket
    # Force sync only 1 day (Today/Latest) for manual trigger
    background_tasks.add_task(lhb_manager.fetch_and_update_data, logger=thread_logger, force_days=1)
    return {"status": "success"}

@app.post("/api/analyze/stock")
async def analyze_stock_manual(request: AnalyzeRequest, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """手动触发个股AI分析"""
    # 扣除次数
    await check_ai_permission(user)
    user_service.consume_quota(db, user, 'ai')
    
    stock_data = {
        "code": request.code,
        "name": request.name,
        "promptType": request.promptType,
        "current": 0,
        "change_percent": 0,
        "turnover": request.turnover,
        "circulation_value": request.circulation_value,
        "kline_data": request.kline_data
    }
    
    try:
        # If turnover/circ_mv missing, try to fetch from market data
        if stock_data['turnover'] is None or stock_data['circulation_value'] is None:
            df = data_provider.fetch_all_market_data()
            if df is not None and not df.empty:
                clean_req_code = "".join(filter(str.isdigit, request.code))
                for _, row in df.iterrows():
                    row_code = str(row['code'])
                    clean_row_code = "".join(filter(str.isdigit, row_code))
                    if clean_req_code == clean_row_code:
                        stock_data['current'] = float(row['current'])
                        stock_data['change_percent'] = float(row['change_percent'])
                        if stock_data['turnover'] is None:
                            stock_data['turnover'] = float(row.get('turnover', 0))
                        if stock_data['circulation_value'] is None:
                            stock_data['circulation_value'] = float(row.get('circ_mv', 0))
                        break
    except:
        pass

    result = analyze_single_stock(stock_data, force_update=request.force)
    return {"status": "success", "result": result}

@app.get("/api/stock/kline")
async def get_stock_kline(code: str, type: str = "1min", user: models.User = Depends(check_data_permission)):
    """获取个股K线数据"""
    try:
        clean_code = "".join(filter(str.isdigit, code))
        if type == "1min":
            date_str = datetime.now().strftime('%Y-%m-%d')
            df = lhb_manager.get_kline_1min(clean_code, date_str)
            if df is None or df.empty:
                 date_str = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                 df = lhb_manager.get_kline_1min(clean_code, date_str)
            
            if df is not None and not df.empty:
                return {"status": "success", "data": df.to_dict('records')}
        elif type == "day":
            import akshare as ak
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')
            df = ak.stock_zh_a_hist(symbol=clean_code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
            if df is not None and not df.empty:
                # Rename columns to match standard
                df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume"})
                return {"status": "success", "data": df.to_dict('records')}
                
    except Exception as e:
        return {"status": "error", "message": str(e)}
    
    return {"status": "error", "message": "No data found"}

@app.get("/api/stock/ai_markers")
async def get_ai_markers(code: str, type: str = None, user: models.User = Depends(check_data_permission)):
    """获取个股的AI分析历史标记"""
    # Determine priority based on type
    keys = []
    if type == 'day':
        keys.append(f"stock_analysis_{code}_day_trading_signal")
    elif type == '1min' or type == 'min':
        keys.append(f"stock_analysis_{code}_min_trading_signal")
    else:
        # Only use fallbacks if no type specified
        keys.extend([
            f"stock_analysis_{code}_min_trading_signal", 
            f"stock_analysis_{code}_day_trading_signal", 
            f"stock_analysis_{code}_trading_signal", 
            f"stock_analysis_{code}_normal"
        ])
    
    # Remove duplicates while preserving order
    unique_keys = []
    seen = set()
    for k in keys:
        if k not in seen:
            unique_keys.append(k)
            seen.add(k)
    
    for key in unique_keys:
        data = ai_cache.get(key)
        if data:
            # Check expiry logic for retrieval too
            # Although ai_cache might not store timestamp in 'get', we can check if we have timestamp in cache
            # But ai_cache implementation details are in core/ai_cache.py. 
            # Assuming simple retrieval for now.
            
            # If it's a string (JSON string or plain text), try to parse it
            if isinstance(data, str):
                try:
                    # Check if it looks like JSON
                    if data.strip().startswith('{'):
                        parsed = json.loads(data)
                        return {"status": "success", "markers": [{"date": datetime.now().strftime('%Y-%m-%d'), "data": parsed}]}
                except:
                    pass
            
            return {"status": "success", "markers": [{"date": datetime.now().strftime('%Y-%m-%d'), "data": data}]}
            
    return {"status": "success", "markers": []}

# --- Static Files Deployment ---
# Serve frontend from root URL
# Must be last to not override API routes
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
