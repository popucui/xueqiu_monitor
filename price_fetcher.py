"""
商品价格抓取模块
数据源：Yahoo Finance (yfinance)

覆盖品种：
  BZ=F   布伦特原油期货
  CL=F   WTI 原油期货（中东原油定价参考；Dubai/Oman 无免费实时源）
  GC=F   黄金期货（COMEX）
  ^GVZ   CBOE 黄金波动率指数
"""

from datetime import datetime, timezone, timedelta
import yfinance as yf

_TICKERS = {
    "brent":  ("BZ=F",  "布伦特原油",     "USD/桶"),
    "wti":    ("CL=F",  "WTI原油(中东参考)", "USD/桶"),
    "gold":   ("GC=F",  "黄金",           "USD/盎司"),
    "gvz":    ("^GVZ",  "GVZ黄金波动率",   "点"),
}

# UTC+8
_CST = timezone(timedelta(hours=8))


def _pct(cur, prev):
    if prev and prev != 0:
        return round((cur - prev) / abs(prev) * 100, 2)
    return None


def fetch_prices() -> dict:
    """
    返回结构：
    {
      "brent": {"symbol": "BZ=F", "name": "...", "unit": "...",
                "price": 95.2, "prev_close": 96.1, "change_pct": -0.94,
                "fetched_at": "2026-04-11T08:30:00+08:00"},
      ...
      "_errors": [...]
    }
    """
    result = {"_errors": []}
    now_cst = datetime.now(_CST).isoformat(timespec="seconds")

    for key, (symbol, name, unit) in _TICKERS.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")
            if hist.empty or len(hist) < 1:
                result["_errors"].append(f"{symbol}: 无数据")
                continue

            closes = hist["Close"].dropna()
            price = round(float(closes.iloc[-1]), 2)
            prev_close = round(float(closes.iloc[-2]), 2) if len(closes) >= 2 else None
            change_pct = _pct(price, prev_close)

            result[key] = {
                "symbol":     symbol,
                "name":       name,
                "unit":       unit,
                "price":      price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "fetched_at": now_cst,
            }
        except Exception as e:
            result["_errors"].append(f"{symbol}: {e}")

    return result
