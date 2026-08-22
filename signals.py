"""
公司量价信号模块 — 日 K 线抓取 + 形态检测

数据源按优先级：Yahoo Finance（批量）→ 东财 → 腾讯（前两者被限流时兜底，
均前复权口径）。成交量统一为"手"口径：Yahoo A 股原始单位为股，入库前 ÷100。
语义参照 price_fetcher：单只失败记入 errors 不阻塞整批。
"""
from datetime import date, datetime, timedelta

import requests
import yfinance as yf

# 批量下载分块大小：太大容易被 Yahoo 限流或单批拖垮
_DOWNLOAD_BATCH = 25

# Yahoo 会对整个 IP 限流（429），东财 K 线接口作为备用数据源。
# 前复权（fqt=1），与 yfinance auto_adjust 口径一致。东财无北交所映射，
# 北交所由腾讯兜底；三源都失败才记 error。
_EM_MARKET = {"SH": "1", "SZ": "0", "HK": "116"}
_EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 腾讯 K 线接口限流最宽松，且覆盖北交所，作为最后兜底
_TX_MARKET = {"SH": "sh", "SZ": "sz", "HK": "hk", "BJ": "bj"}
_TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

SIGNAL_LABELS = {
    "high_vol_up": "放量上涨",
    "low_vol_bottom": "缩量下跌·底部",
    "consolidation": "横盘企稳",
    "low_vol_down": "缩量下跌",
}

DEFAULT_PARAMS = {
    "min_history": 25,          # 基线不足则跳过检测
    "lowvol_ratio": 0.6,        # 缩量下跌：当日量 < 均量 × 0.6
    "consolidation_days": 10,   # 横盘观察窗口（交易日）
    "consolidation_max_range": 0.03,  # 窗口内单日振幅上限
    "consolidation_max_drift": 0.05,  # 窗口内累计涨跌上限
    "consolidation_vol_ratio": 0.8,   # 窗口均量 ≤ 此前均量 × 0.8
    "up_min_pct": 0.03,         # 放量上涨：当日涨幅下限
    "up_vol_ratio": 2.0,        # 放量上涨：量比下限
    "stale_days": 3,            # 最新 K 线距今超过 N 自然日视为停牌/节假日
    # 缩量下跌·底部（潜伏形态）：缩量阴跌 + 股价处底部区域 + 非恐慌性下跌
    "bottom_range_days": 120,       # 底部判定的观察窗口（交易日）
    "bottom_near_low_pct": 0.10,    # 收盘价距窗口最低点 ≤10%
    "bottom_off_high_pct": 0.20,    # 收盘价距窗口最高点回撤 ≥20%（已跌出空间）
    "bottom_max_drop": 0.05,        # 单日跌幅 >5% 视为恐慌杀跌，不算温和阴跌
}


def to_yahoo_symbol(code: str) -> str:
    """内部代码（601872.SH / 000001.SZ / 02400.HK）→ Yahoo 格式。

    港股内部是 5 位（02400.HK），Yahoo 用去前导零后至少 4 位（2400.HK / 0700.HK）。
    """
    code = (code or "").strip().upper()
    if code.endswith(".SH"):
        return code[:-3] + ".SS"
    if code.endswith(".HK"):
        num = (code[:-3].lstrip("0") or "0").zfill(4)
        return f"{num}.HK"
    return code


def _df_to_klines(df) -> list:
    """把单 symbol 的 OHLCV DataFrame 转成 K 线字典列表（升序）"""
    klines = []
    if df is None or df.empty:
        return klines
    closes = df["Close"]
    for idx, row in df.iterrows():
        close = row.get("Close")
        if close is None or str(close) == "nan" or close != close:
            continue
        day = idx.date() if hasattr(idx, "date") else idx
        klines.append({
            "date": str(day),
            "open": float(row["Open"]) if row.get("Open") == row.get("Open") else None,
            "high": float(row["High"]) if row.get("High") == row.get("High") else None,
            "low": float(row["Low"]) if row.get("Low") == row.get("Low") else None,
            "close": float(close),
            "volume": float(row["Volume"]) if row.get("Volume") == row.get("Volume") else None,
        })
    klines.sort(key=lambda k: k["date"])
    return klines


def _normalize_yahoo_volume(code: str, klines: list) -> list:
    """Yahoo A 股 volume 单位是股，东财/腾讯是手（1 手 = 100 股）。
    跨源按 (code, date) 混存时口径不一致会让量比失真百倍，Yahoo 数据
    入库前统一归一为手。港股三源均为股，不动。"""
    code = (code or "").strip().upper()
    if not code.endswith((".SH", ".SZ", ".BJ")):
        return klines
    for k in klines:
        if k.get("volume") is not None:
            k["volume"] = k["volume"] / 100.0
    return klines


def _fetch_single(symbol: str, period: str) -> list:
    try:
        hist = yf.Ticker(symbol).history(period=period, auto_adjust=True)
    except Exception as e:
        raise RuntimeError(f"{symbol}: {e}") from e
    return _df_to_klines(hist)


def _em_secid(code: str):
    code = (code or "").strip().upper()
    num, _, market = code.partition(".")
    prefix = _EM_MARKET.get(market)
    if not prefix or not num:
        return None
    return f"{prefix}.{num}"


def _fetch_em_klines(code: str, limit: int) -> list:
    """东财日 K 线（前复权）。行格式：date,open,close,high,low,volume,amount,振幅"""
    secid = _em_secid(code)
    if not secid:
        raise RuntimeError(f"{code}: 东财无该市场 secid 映射")
    params = {
        "secid": secid, "klt": 101, "fqt": 1,
        "lmt": max(5, int(limit)), "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
    }
    resp = requests.get(_EM_KLINE_URL, params=params, timeout=10,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    klines = []
    for line in data.get("klines") or []:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        klines.append({
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]),
        })
    return klines


def _fetch_tx_klines(code: str, limit: int) -> list:
    """腾讯日 K 线（前复权）。行格式：[date, open, close, high, low, volume, ...]"""
    code = (code or "").strip().upper()
    num, _, market = code.partition(".")
    prefix = _TX_MARKET.get(market)
    if not prefix or not num:
        raise RuntimeError(f"{code}: 腾讯无该市场映射")
    symbol = f"{prefix}{num}"
    params = {"param": f"{symbol},day,,,{max(5, int(limit))},qfq"}
    resp = requests.get(_TX_KLINE_URL, params=params, timeout=10,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    data = ((resp.json() or {}).get("data") or {}).get(symbol) or {}
    rows = data.get("qfqday") or data.get("day") or []
    klines = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        klines.append({
            "date": str(row[0]),
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "volume": float(row[5]),
        })
    return klines


def fetch_daily_klines(codes: list, period_days: int = 12) -> tuple:
    """批量抓取日 K 线。

    返回 ``(klines_by_code, errors)``：
    - klines_by_code: {code: [K线dict升序]}，只包含抓到数据的公司
    - errors: [f"{code}: 原因"]
    优先 Yahoo 批量下载，缺失的降级逐只重试，再缺失的走东财备用源；
    双源都失败才记 error。period_days 用自然日表达；预热建议 260，
    日常增量 12 即可覆盖节假日间隔。
    """
    result = {}
    errors = []
    batch_errors = []
    codes = [c for c in codes if str(c).strip()]
    period = f"{max(2, int(period_days))}d"

    for start in range(0, len(codes), _DOWNLOAD_BATCH):
        batch = codes[start:start + _DOWNLOAD_BATCH]
        symbols = [to_yahoo_symbol(c) for c in batch]
        symbol_to_code = dict(zip(symbols, batch))
        got = {}
        try:
            df = yf.download(
                tickers=symbols, period=period, auto_adjust=True,
                progress=False, threads=True,
            )
            if df is not None and not df.empty:
                if len(symbols) == 1:
                    got[symbols[0]] = _normalize_yahoo_volume(
                        batch[0], _df_to_klines(df))
                else:
                    for symbol in symbols:
                        try:
                            sub = df.xs(symbol, axis=1, level=1)
                        except (KeyError, TypeError):
                            continue
                        got[symbol] = _normalize_yahoo_volume(
                            symbol_to_code[symbol], _df_to_klines(sub))
        except Exception as e:
            batch_errors.append(f"batch({','.join(symbols[:3])}...): Yahoo 批量下载失败 {e}")

        # 批量缺失的 symbol：先逐只 Yahoo，再东财兜底
        for symbol in symbols:
            code = symbol_to_code[symbol]
            if got.get(symbol):
                result[code] = got[symbol]
                continue
            try:
                klines = _normalize_yahoo_volume(code, _fetch_single(symbol, period))
                if klines:
                    result[code] = klines
                    continue
            except Exception:
                pass
            try:
                # 东财/腾讯按交易日数取：自然日 × 5/7 估算，加余量
                em_limit = int(period_days * 5 / 7) + 10
                klines = _fetch_em_klines(code, em_limit)
                if klines:
                    result[code] = klines
                    continue
            except Exception:
                pass
            try:
                klines = _fetch_tx_klines(code, em_limit)
                if klines:
                    result[code] = klines
                    continue
            except Exception as e:
                errors.append(f"{code}: Yahoo/东财/腾讯均失败（{str(e)[:120]}）")
                continue
            errors.append(f"{code}: 无数据")

    if any(code not in result for code in codes):
        errors = batch_errors + errors
    return result, errors


def _avg(values):
    usable = [v for v in values if v is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def detect_signals(code: str, klines: list, params: dict = None) -> tuple:
    """对单只公司的日 K 线（升序）检测形态。

    返回 ``(signals, skip_reason)``：signals 元素为
    ``{"code", "date", "signal_type", "detail"}``；不满足检测前提时
    signals 为空、skip_reason 给出原因（历史不足/停牌）供日志。
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    code = (code or "").strip().upper()

    if len(klines) < p["min_history"]:
        return [], f"历史不足（{len(klines)}<{p['min_history']}）"

    latest = klines[-1]
    try:
        latest_date = datetime.strptime(latest["date"], "%Y-%m-%d").date()
    except (ValueError, TypeError, KeyError):
        return [], "最新K线日期异常"
    if (date.today() - latest_date).days > p["stale_days"]:
        return [], f"最新K线停留在 {latest['date']}（停牌/节假日）"

    prev = klines[-2]
    close, prev_close = latest.get("close"), prev.get("close")
    volume = latest.get("volume")
    if not close or not prev_close:
        return [], "价格数据缺失"
    change = (close - prev_close) / prev_close

    # 前 20 根（不含当日）均量，量比基线
    prior_volumes = [k.get("volume") for k in klines[-21:-1]]
    base_vol = _avg(prior_volumes)

    signals = []
    date_str = latest["date"]
    change_pct = round(change * 100, 2)

    if base_vol and volume is not None:
        vol_ratio = volume / base_vol
        if change < 0 and vol_ratio < p["lowvol_ratio"]:
            signals.append({
                "code": code, "date": date_str, "signal_type": "low_vol_down",
                "detail": f"跌 {abs(change_pct):.2f}%，量比 {vol_ratio:.2f}（前20日均量基准）",
            })
            # 细化形态：缩量阴跌 + 底部区域 + 非恐慌杀跌 —— 利好催化下易反弹
            if change >= -p["bottom_max_drop"]:
                window_b = klines[-p["bottom_range_days"]:]
                lows = [k["low"] for k in window_b if k.get("low")]
                highs = [k["high"] for k in window_b if k.get("high")]
                if lows and highs:
                    min_low, max_high = min(lows), max(highs)
                    near_low = (close - min_low) / min_low
                    off_high = (max_high - close) / max_high
                    if (near_low <= p["bottom_near_low_pct"]
                            and off_high >= p["bottom_off_high_pct"]):
                        signals.append({
                            "code": code, "date": date_str,
                            "signal_type": "low_vol_bottom",
                            "detail": (
                                f"跌 {abs(change_pct):.2f}%，量比 {vol_ratio:.2f}；"
                                f"距{len(window_b)}日低点 {near_low * 100:.1f}%，"
                                f"距高点回撤 {off_high * 100:.1f}%（需人工核实基本面）"
                            ),
                        })
        if change >= p["up_min_pct"] and vol_ratio >= p["up_vol_ratio"]:
            signals.append({
                "code": code, "date": date_str, "signal_type": "high_vol_up",
                "detail": f"涨 {change_pct:.2f}%，量比 {vol_ratio:.2f}",
            })

    # 横盘企稳：近 N 日振幅收窄 + 量能萎缩
    n = p["consolidation_days"]
    if len(klines) >= n + 20:
        window = klines[-n:]
        ok = True
        for i, bar in enumerate(window):
            ref_close = klines[len(klines) - n + i - 1].get("close")
            if not ref_close or bar.get("high") is None or bar.get("low") is None:
                ok = False
                break
            if (bar["high"] - bar["low"]) / ref_close > p["consolidation_max_range"]:
                ok = False
                break
        if ok:
            win_open = klines[-n - 1].get("close")  # 窗口前一日收盘作基准
            win_close = window[-1].get("close")
            drift = abs(win_close - win_open) / win_open if win_open else None
            win_vol = _avg([bar.get("volume") for bar in window])
            before_vol = _avg([k.get("volume") for k in klines[-n - 20:-n]])
            if (drift is not None and drift <= p["consolidation_max_drift"]
                    and win_vol is not None and before_vol
                    and win_vol <= before_vol * p["consolidation_vol_ratio"]):
                vol_pct = round(win_vol / before_vol, 2)
                signals.append({
                    "code": code, "date": date_str, "signal_type": "consolidation",
                    "detail": f"近{n}日振幅收窄、区间涨跌 {round(drift * 100, 2):.2f}%，"
                              f"窗口均量缩至前期 {vol_pct} 倍",
                })

    return signals, None
