"""股息率与股东回报展示用指标。

当前先提供一个明确标注的界面 demo。真实版本应把这里的演示记录替换为
结构化的资本回报事件数据源，而不是把公告标题中的数字直接当成分红或回购。
"""

# 仅用于让 UI demo 有可见内容；这些数值不是投资数据，不应作为真实行情使用。
_DEMO_DIVIDENDS = {
    "02400.HK": 0.90,
    "601872.SH": 0.70,
    "002027.SZ": 0.32,
    "002202.SZ": 0.18,
    "002352.SZ": 0.80,
    "00883.HK": 1.10,
    "01428.HK": 0.30,
    "01816.HK": 0.10,
    "02382.HK": 0.90,
    "02688.HK": 0.80,
}

# TTM 已执行回购金额 / 期间平均市值（百分比）。仅用于演示“回购收益率”
# 的展示位置和合计逻辑，不代表真实回购数据；公告额度不会直接计入。
_DEMO_BUYBACK_YIELDS = {
    "02400.HK": 0.45,
    "601872.SH": 0.65,
    "002027.SZ": 1.20,
    "002202.SZ": 0.30,
    "002352.SZ": 0.80,
    "00883.HK": 0.95,
    "01428.HK": 0.50,
    "01816.HK": 0.30,
    "02382.HK": 0.40,
    "02688.HK": 0.70,
}

DEMO_AS_OF = "2026-09-21"


def _metrics_from_events(code: str, close: float, events: list[dict]) -> dict | None:
    """从已执行事件计算单家公司指标；没有有效事件时返回 None。"""
    rows = [
        event for event in events
        if str(event.get("code") or "").strip().upper() == code
        and str(event.get("status") or "").strip().lower() == "executed"
    ]
    if not rows:
        return None

    dividend = 0.0
    buyback_amount = 0.0
    market_caps = []
    dividend_seen = False
    buyback_seen = False
    sources = set()
    for event in rows:
        event_type = str(event.get("event_type") or "").strip().lower()
        try:
            amount = float(event.get("amount"))
        except (TypeError, ValueError):
            continue
        if amount < 0:
            continue
        sources.add(str(event.get("source") or "结构化事件").strip())
        if event_type == "dividend":
            # dividend.amount 的口径是每股现金分红。
            dividend += amount
            dividend_seen = True
        elif event_type == "buyback":
            try:
                market_cap = float(event.get("market_cap"))
            except (TypeError, ValueError):
                market_cap = 0.0
            if market_cap <= 0:
                continue
            buyback_amount += amount
            market_caps.append(market_cap)
            buyback_seen = True

    if not dividend_seen and not buyback_seen:
        return None
    dividend_yield = round(dividend / close * 100, 2) if dividend_seen and close > 0 else None
    buyback_yield = None
    if buyback_seen and market_caps:
        average_market_cap = sum(market_caps) / len(market_caps)
        buyback_yield = round(buyback_amount / average_market_cap * 100, 2)
    parts = [value for value in (dividend_yield, buyback_yield) if value is not None]
    return {
        "ttm_dividend": round(dividend, 3) if dividend_seen else None,
        "ttm_yield_pct": dividend_yield,
        "ttm_buyback_yield_pct": buyback_yield,
        "shareholder_yield_pct": round(sum(parts), 2) if parts else None,
        "ttm_status": "actual",
        "ttm_source": ", ".join(sorted(sources)) or "结构化事件",
    }


def get_ttm_metrics(
    watchlist: list[dict], latest_prices: dict, events: list[dict] | None = None,
    as_of: str | None = None,
) -> dict:
    """返回 {code: TTM 指标}，供公司列表 demo 使用。

    TTM 口径为近 365 天已实施现金分红合计 / 最新收盘价。demo 中每家公司
    只有一笔示例普通分红和一笔已执行回购，故重点是展示 UI 形态，而非提供
    真实财务数据。股东回报率 = 股息率 + 回购收益率。
    """
    result = {}
    events = events or []
    display_as_of = as_of or DEMO_AS_OF
    for item in watchlist:
        code = str(item.get("code") or "").strip().upper()
        close = (latest_prices.get(code) or {}).get("close")
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue

        actual = _metrics_from_events(code, close, events)
        if actual is not None:
            actual.update({
                "ttm_as_of": display_as_of,
                "ttm_window_days": 365,
            })
            result[code] = actual
            continue

        # 已有真实事件但字段不足时宁可显示无数据，也不能退回演示值。
        if any(str(event.get("code") or "").strip().upper() == code for event in events):
            continue

        dividend = _DEMO_DIVIDENDS.get(code)
        buyback_yield = _DEMO_BUYBACK_YIELDS.get(code)
        if dividend is None or buyback_yield is None:
            continue
        dividend = float(dividend)
        buyback_yield = float(buyback_yield)
        dividend_yield = round(dividend / close * 100, 2)
        buyback_yield = round(buyback_yield, 2)
        result[code] = {
            "ttm_dividend": round(dividend, 3),
            "ttm_yield_pct": dividend_yield,
            "ttm_buyback_yield_pct": buyback_yield,
            "shareholder_yield_pct": round(dividend_yield + buyback_yield, 2),
            "ttm_as_of": display_as_of,
            "ttm_status": "demo",
            "ttm_source": "演示数据",
            "ttm_window_days": 365,
        }
    return result
