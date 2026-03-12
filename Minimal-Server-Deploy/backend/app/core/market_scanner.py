import pandas as pd
from datetime import datetime

from app.core.data_provider import data_provider
from app.core.seat_matcher import matcher

# Keep switch for compatibility; board filtering is now handled by is_main_or_gem_stock.
FILTER_BSE = False


def _digits6(code) -> str:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) > 6:
        digits = digits[-6:]
    return digits


def is_bse_stock(code):
    text = str(code or "").strip().lower()
    digits = _digits6(text)
    return text.startswith("bj") or digits.startswith(("8", "4", "92"))


def is_20cm_stock(code):
    digits = _digits6(code)
    return digits.startswith(("30", "68"))


def is_main_or_gem_stock(code):
    digits = _digits6(code)
    if len(digits) != 6:
        return False
    if digits.startswith(("8", "4", "92", "68")):
        return False
    return digits.startswith(("0", "3", "6"))


def _to_full_code(code):
    digits = _digits6(code)
    if len(digits) != 6:
        return str(code or "").strip().lower()
    if digits.startswith("6"):
        return f"sh{digits}"
    if digits.startswith(("0", "3")):
        return f"sz{digits}"
    if digits.startswith(("8", "4", "9")):
        return f"bj{digits}"
    return f"sz{digits}"


def _safe_float(value, default=0.0):
    try:
        text = str(value or "").strip()
        if not text or text in {"--", "None", "nan", "null"}:
            return float(default)
        return float(text.replace(",", ""))
    except Exception:
        return float(default)


def _safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _pick(row, keys, default=None):
    if not isinstance(row, dict):
        return default
    for key in keys:
        if key in row:
            value = row.get(key)
            if value is not None and str(value).strip() != "":
                return value
    return default


def _calc_likely_seats(market_cap, volume, limit_up_days, code):
    try:
        df_min = data_provider.fetch_intraday_data(code)
    except Exception:
        df_min = None

    price_hist = []
    avg_vol = 1
    if df_min is not None and not df_min.empty:
        try:
            price_hist = df_min["close"].tolist()
        except Exception:
            price_hist = []
        try:
            avg_vol = float(df_min["volume"].mean() or 1)
        except Exception:
            avg_vol = 1

    if avg_vol <= 0:
        avg_vol = 1

    try:
        return matcher.match(
            {
                "time": datetime.now(),
                "price_history": price_hist,
                "volume": _safe_float(volume, 0),
                "avg_volume": avg_vol,
                "market_cap": _safe_float(market_cap, 0),
                "limit_up_days": max(1, _safe_int(limit_up_days, 1)),
            }
        )
    except Exception:
        return []


def scan_intraday_limit_up(logger=None):
    if logger:
        logger("[*] 开始扫描盘中异动股...")

    df = data_provider.fetch_all_market_data()
    if df is None or df.empty:
        if logger:
            logger("[!] 全市场行情为空，盘中扫描跳过")
        return [], []

    candidates = []
    for _, row in df.iterrows():
        code = _digits6(row.get("code", ""))
        if len(code) != 6:
            continue
        if not is_main_or_gem_stock(code):
            continue
        if FILTER_BSE and is_bse_stock(code):
            continue

        name = str(row.get("name", "") or "").strip()
        if "ST" in name:
            continue

        current = _safe_float(row.get("current"), 0)
        prev_close = _safe_float(row.get("prev_close"), 0)
        if current <= 0 or prev_close <= 0:
            continue

        change_percent = _safe_float(row.get("change_percent"), 0)
        speed = _safe_float(row.get("speed"), 0)
        turnover = _safe_float(row.get("turnover"), 0)
        circ_mv = _safe_float(row.get("circ_mv"), 0)
        volume = _safe_float(row.get("volume"), 0)

        is_20cm = is_20cm_stock(code)
        threshold = 15.0 if is_20cm else 5.0
        if change_percent < threshold or speed < 3.0:
            continue

        ratio = 1.2 if is_20cm else 1.1
        limit_up_price = round(prev_close * ratio, 2)

        candidates.append(
            {
                "code": _to_full_code(code),
                "name": name or code,
                "current": round(current, 2),
                "change_percent": round(change_percent, 2),
                "speed": round(speed, 2),
                "turnover": round(turnover, 2),
                "circ_mv": circ_mv,
                "volume": volume,
                "limit_up_price": limit_up_price,
            }
        )

    if not candidates:
        return [], []

    detailed_quotes = data_provider.fetch_quotes([x["code"] for x in candidates]) or []
    detail_map = {str(q.get("code", "")): q for q in detailed_quotes if isinstance(q, dict)}

    intraday_stocks = []
    sealed_stocks = []
    for cand in candidates:
        detail = detail_map.get(cand["code"]) or {}
        is_sealed = bool(detail.get("is_limit_up", False))
        if not is_sealed and cand["current"] >= cand["limit_up_price"] - 0.01:
            is_sealed = True

        if is_sealed:
            likely_seats = _calc_likely_seats(
                market_cap=cand.get("circ_mv", 0),
                volume=cand.get("volume", 0),
                limit_up_days=1,
                code=cand["code"],
            )
            sealed_stocks.append(
                {
                    "code": cand["code"],
                    "name": cand["name"],
                    "current": cand["current"],
                    "change_percent": cand["change_percent"],
                    "time": str(detail.get("time", "") or "-"),
                    "concept": "盘中涨停",
                    "associated": "盘中涨停",
                    "reason": "涨停",
                    "strategy": "LimitUp",
                    "circulation_value": cand["circ_mv"],
                    "turnover": cand["turnover"],
                    "limit_up_days": 1,
                    "likely_seats": likely_seats,
                }
            )
            continue

        likely_seats = _calc_likely_seats(
            market_cap=cand.get("circ_mv", 0),
            volume=cand.get("volume", 0),
            limit_up_days=1,
            code=cand["code"],
        )
        intraday_stocks.append(
            {
                "code": cand["code"],
                "name": cand["name"],
                "concept": "盘中异动",
                "associated": "盘中异动",
                "reason": f"盘中突击: 涨幅{cand['change_percent']}%, 涨速{cand['speed']}%",
                "score": 8.0 + (cand["speed"] * 0.5),
                "strategy": "LimitUp",
                "circulation_value": cand["circ_mv"],
                "turnover": cand["turnover"],
                "likely_seats": likely_seats,
                "limit_up_days": 1,
            }
        )
    return intraday_stocks, sealed_stocks


def scan_limit_up_pool(logger=None):
    if logger:
        logger("[*] 正在扫描涨停股池...")

    df = data_provider.fetch_limit_up_pool()
    if df is None or df.empty:
        if logger:
            logger("[!] 涨停股池为空")
        return []

    found = []
    for _, row_raw in df.iterrows():
        row = row_raw.to_dict() if hasattr(row_raw, "to_dict") else dict(row_raw)
        code_raw = _pick(row, ["code", "dm", "代码"], "")
        code_digits = _digits6(code_raw)
        if len(code_digits) != 6:
            continue
        if not is_main_or_gem_stock(code_digits):
            continue
        if FILTER_BSE and is_bse_stock(code_digits):
            continue

        full_code = _to_full_code(code_digits)
        name = str(_pick(row, ["name", "mc", "名称"], code_digits) or code_digits).strip()
        if "ST" in name:
            continue

        current = round(_safe_float(_pick(row, ["current", "p", "最新价"], 0), 0), 2)
        change_percent = round(_safe_float(_pick(row, ["change_percent", "zf", "涨跌幅"], 0), 0), 2)
        turnover = round(_safe_float(_pick(row, ["turnover", "hs", "换手率"], 0), 0), 2)
        circ_mv = _safe_float(_pick(row, ["circulation_value", "lt", "流通市值"], 0), 0)
        amount = _safe_float(_pick(row, ["amount", "cje", "成交额"], 0), 0)
        volume = _safe_float(_pick(row, ["volume", "v", "成交量"], 0), 0)
        limit_days = max(1, _safe_int(_pick(row, ["limit_up_days", "lbc", "连板数"], 1), 1))
        concept = str(_pick(row, ["concept", "tj", "所属行业"], "") or "").strip()
        first_seal_time = str(_pick(row, ["time", "fbt", "首次封板时间"], "-") or "-").strip()

        likely_seats = _calc_likely_seats(
            market_cap=circ_mv,
            volume=volume if volume > 0 else amount,
            limit_up_days=limit_days,
            code=full_code,
        )

        found.append(
            {
                "code": full_code,
                "name": name,
                "current": current,
                "change_percent": change_percent,
                "time": first_seal_time or "-",
                "concept": concept,
                "associated": concept,
                "reason": f"{limit_days}连板" if limit_days > 1 else "首板",
                "strategy": "LimitUp",
                "circulation_value": circ_mv,
                "turnover": turnover,
                "limit_up_days": limit_days,
                "likely_seats": likely_seats,
            }
        )

    return found


def scan_broken_limit_pool(logger=None):
    if logger:
        logger("[*] 正在扫描炸板股池...")

    df = data_provider.fetch_broken_limit_pool()
    if df is None or df.empty:
        return []

    found = []
    for _, row_raw in df.iterrows():
        row = row_raw.to_dict() if hasattr(row_raw, "to_dict") else dict(row_raw)
        code_raw = _pick(row, ["code", "dm", "代码"], "")
        code_digits = _digits6(code_raw)
        if len(code_digits) != 6:
            continue
        if not is_main_or_gem_stock(code_digits):
            continue
        if FILTER_BSE and is_bse_stock(code_digits):
            continue

        full_code = _to_full_code(code_digits)
        name = str(_pick(row, ["name", "mc", "名称"], code_digits) or code_digits).strip()
        if "ST" in name:
            continue

        found.append(
            {
                "code": full_code,
                "name": name,
                "current": round(_safe_float(_pick(row, ["current", "p", "最新价"], 0), 0), 2),
                "change_percent": round(_safe_float(_pick(row, ["change_percent", "zf", "涨跌幅"], 0), 0), 2),
                "time": str(_pick(row, ["time", "fbt", "首次封板时间"], "-") or "-").strip(),
                "high": round(_safe_float(_pick(row, ["high", "ztp", "涨停价"], 0), 0), 2),
                "concept": str(_pick(row, ["concept", "tj", "所属行业"], "") or "").strip(),
                "associated": str(_pick(row, ["associated", "tj", "所属行业"], "") or "").strip(),
                "amplitude": round(_safe_float(_pick(row, ["amplitude", "zs", "振幅"], 0), 0), 2),
                "circulation_value": _safe_float(_pick(row, ["circulation_value", "lt", "流通市值"], 0), 0),
                "turnover": round(_safe_float(_pick(row, ["turnover", "hs", "换手率"], 0), 0), 2),
                "limit_up_days": max(1, _safe_int(_pick(row, ["limit_up_days", "lbc", "连板数"], 1), 1)),
                "broken_count": _safe_int(_pick(row, ["broken_count", "zbc", "炸板次数"], 0), 0),
            }
        )
    return found


def scan_broken_limit_pool_fallback(logger=None):
    return scan_broken_limit_pool(logger)


def scan_limit_up_pool_fallback(logger=None):
    return scan_limit_up_pool(logger)


def get_market_overview(logger=None, allow_non_trading_probe: bool = False):
    overview = {
        "indices": [],
        "stats": {
            "limit_up_count": 0,
            "limit_down_count": 0,
            "broken_count": 0,
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "total_volume": 0,
            "sentiment": "Neutral",
            "suggestion": "观察",
        },
    }

    try:
        if logger:
            logger("[*] 获取大盘指数...")
        indices = data_provider.fetch_indices() or []
        overview["indices"] = indices
        total_vol = 0.0
        for idx in indices:
            idx_name = str((idx or {}).get("name", "")).strip()
            if idx_name in {"上证指数", "深证成指"}:
                total_vol += _safe_float((idx or {}).get("amount"), 0)
        overview["stats"]["total_volume"] = round(total_vol, 2)
    except Exception as e:
        if logger:
            logger(f"[!] 获取指数失败: {e}")

    try:
        df = data_provider.fetch_all_market_data(allow_non_trading_probe=allow_non_trading_probe)
        if df is None or df.empty:
            if logger:
                logger("[!] 全市场行情为空，情绪统计跳过")
        else:
            work_df = df.copy()
            work_df["code"] = work_df["code"].map(_digits6)
            work_df = work_df[work_df["code"].map(is_main_or_gem_stock)]
            if not work_df.empty:
                work_df["change_percent"] = pd.to_numeric(work_df["change_percent"], errors="coerce").fillna(0)
                up_count = int((work_df["change_percent"] > 0).sum())
                down_count = int((work_df["change_percent"] < 0).sum())
                flat_count = int((work_df["change_percent"] == 0).sum())
                overview["stats"]["up_count"] = up_count
                overview["stats"]["down_count"] = down_count
                overview["stats"]["flat_count"] = flat_count

                limit_up_count = 0
                broken_count = 0
                for _, row in work_df.iterrows():
                    code = _digits6(row.get("code", ""))
                    current = _safe_float(row.get("current"), 0)
                    prev_close = _safe_float(row.get("prev_close"), 0)
                    high = _safe_float(row.get("high"), 0)
                    if current <= 0 or prev_close <= 0:
                        continue
                    ratio = 1.2 if is_20cm_stock(code) else 1.1
                    limit_price = round(prev_close * ratio, 2)
                    if current >= limit_price - 0.01:
                        limit_up_count += 1
                    elif high >= limit_price - 0.01 and current < limit_price - 0.01:
                        broken_count += 1
                overview["stats"]["limit_up_count"] = int(limit_up_count)
                overview["stats"]["broken_count"] = int(broken_count)

                limit_down_df = data_provider.fetch_limit_down_pool()
                if limit_down_df is not None and not limit_down_df.empty:
                    overview["stats"]["limit_down_count"] = int(len(limit_down_df))
                else:
                    # Fallback estimate if limit-down pool unavailable.
                    limit_down_count = 0
                    for _, row in work_df.iterrows():
                        code = _digits6(row.get("code", ""))
                        cp = _safe_float(row.get("change_percent"), 0)
                        threshold = -19.5 if code.startswith("30") else -9.5
                        if cp <= threshold:
                            limit_down_count += 1
                    overview["stats"]["limit_down_count"] = int(limit_down_count)
    except Exception as e:
        if logger:
            logger(f"[!] 获取市场情绪失败: {e}")

    zt_count = int(overview["stats"].get("limit_up_count", 0) or 0)
    up_count = int(overview["stats"].get("up_count", 0) or 0)
    down_count = int(overview["stats"].get("down_count", 0) or 0)

    sh_change = 0.0
    for idx in overview.get("indices", []):
        if str((idx or {}).get("name", "")).strip() == "上证指数":
            sh_change = _safe_float((idx or {}).get("change"), 0)
            break

    sentiment = "Neutral"
    suggestion = "观察"
    if zt_count > 45 and sh_change > 0 and up_count > down_count:
        sentiment = "High"
        suggestion = "积极打板"
    elif zt_count < 15 or sh_change < -0.8 or down_count > (up_count * 1.5):
        sentiment = "Low"
        suggestion = "谨慎出手"
    else:
        sentiment = "Neutral"
        suggestion = "去弱留强"

    if sh_change < -1.5 or (sh_change < -0.5 and down_count > 3500):
        sentiment = "Panic"
        suggestion = "空仓避险"

    overview["stats"]["sentiment"] = sentiment
    overview["stats"]["suggestion"] = suggestion
    return overview
