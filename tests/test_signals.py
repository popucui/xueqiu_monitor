import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import app as app_mod
import database
import signals


def _dates(n, end=None):
    """返回最近 n 个自然日（含 end，默认今天）的 YYYY-MM-DD 升序列表"""
    end = end or date.today()
    return [(end - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d") for i in range(n)]


def _flat_klines(n=60, close=10.0, volume=1000.0, end=None):
    return [
        {"date": d, "open": close, "high": close * 1.005, "low": close * 0.995,
         "close": close, "volume": volume}
        for d in _dates(n, end)
    ]


class ToYahooSymbolTests(unittest.TestCase):
    def test_sh_suffix_becomes_ss(self):
        self.assertEqual("601872.SS", signals.to_yahoo_symbol("601872.SH"))

    def test_sz_kept(self):
        self.assertEqual("000001.SZ", signals.to_yahoo_symbol("000001.SZ"))

    def test_hk_yahoo_uses_four_digit_code(self):
        self.assertEqual("2400.HK", signals.to_yahoo_symbol("02400.HK"))
        self.assertEqual("0700.HK", signals.to_yahoo_symbol("00700.HK"))
        self.assertEqual("0152.HK", signals.to_yahoo_symbol("0152.HK"))
        self.assertEqual("0001.HK", signals.to_yahoo_symbol("00001.HK"))

    def test_uppercase_and_strip(self):
        self.assertEqual("600519.SS", signals.to_yahoo_symbol(" 600519.sh "))


class DetectSignalsTests(unittest.TestCase):
    def test_low_vol_down(self):
        klines = _flat_klines(60)
        last = klines[-1]
        last.update({"close": 9.8, "volume": 200.0})  # 跌 2%，量 0.2 倍
        sigs, skip = signals.detect_signals("600001.SH", klines)
        self.assertIsNone(skip)
        types = [s["signal_type"] for s in sigs]
        self.assertIn("low_vol_down", types)
        self.assertNotIn("high_vol_up", types)

    def test_high_vol_up(self):
        klines = _flat_klines(60)
        klines[-1].update({"close": 10.6, "volume": 3000.0})  # 涨 6%，量比 3
        sigs, skip = signals.detect_signals("600001.SH", klines)
        self.assertIsNone(skip)
        types = [s["signal_type"] for s in sigs]
        self.assertIn("high_vol_up", types)

    def test_consolidation(self):
        # 前 40 日放量，近 20 日窄幅横盘且缩量
        klines = _flat_klines(40, volume=2000.0)
        flat = _flat_klines(20, volume=500.0,
                            end=date.today())
        # 拼接：前段截止昨天之前，避免日期重叠
        klines = _flat_klines(40, volume=2000.0,
                              end=date.today() - timedelta(days=20))
        klines.extend(flat)
        sigs, skip = signals.detect_signals("600001.SH", klines)
        self.assertIsNone(skip)
        types = [s["signal_type"] for s in sigs]
        self.assertIn("consolidation", types)

    def test_low_vol_bottom(self):
        # 高位 20 → 跌至 10 附近缩量阴跌，处于底部区域
        dates = _dates(130)
        klines = []
        for i, d in enumerate(dates[:-1]):
            if i < 60:
                klines.append({"date": d, "open": 20, "high": 20.2, "low": 19.8,
                               "close": 20.0, "volume": 1000.0})
            else:
                klines.append({"date": d, "open": 10.2, "high": 10.25, "low": 10.15,
                               "close": 10.2, "volume": 1000.0})
        klines.append({"date": dates[-1], "open": 10.2, "high": 10.2, "low": 10.1,
                       "close": 10.16, "volume": 300.0})  # 跌 0.39%，量比 0.3
        sigs, skip = signals.detect_signals("600001.SH", klines)
        self.assertIsNone(skip)
        types = [s["signal_type"] for s in sigs]
        self.assertIn("low_vol_down", types)
        self.assertIn("low_vol_bottom", types)

    def test_low_vol_bottom_not_near_low(self):
        # 缩量下跌但股价在高位，不应触发底部形态
        klines = _flat_klines(130, close=20.0)
        klines[-1].update({"close": 19.9, "volume": 300.0})
        sigs, skip = signals.detect_signals("600001.SH", klines)
        self.assertIsNone(skip)
        types = [s["signal_type"] for s in sigs]
        self.assertIn("low_vol_down", types)
        self.assertNotIn("low_vol_bottom", types)

    def test_normal_bar_no_signal(self):
        klines = _flat_klines(60)
        klines[-1].update({"close": 10.05, "volume": 1100.0})
        sigs, skip = signals.detect_signals("600001.SH", klines)
        self.assertIsNone(skip)
        self.assertEqual([], sigs)

    def test_short_history_skipped(self):
        klines = _flat_klines(10)
        sigs, skip = signals.detect_signals("600001.SH", klines)
        self.assertEqual([], sigs)
        self.assertIn("历史不足", skip)

    def test_stale_kline_skipped(self):
        klines = _flat_klines(60, end=date.today() - timedelta(days=10))
        sigs, skip = signals.detect_signals("600001.SH", klines)
        self.assertEqual([], sigs)
        self.assertIn("停牌", skip)


class CompanyDbTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path_patch = patch.object(
            database, "_DB_PATH", str(Path(self.temp_dir.name) / "test.db"),
        )
        self.db_path_patch.start()
        database.init_db()

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_sqlite_busy_timeout_is_30s(self):
        conn = database.get_conn()
        try:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
            self.assertEqual(30000, int(row[0]))
        finally:
            conn.close()

    def test_add_duplicate_company_returns_false(self):
        self.assertTrue(database.add_company_stock("02400.HK", "心动公司", "HK"))
        self.assertFalse(database.add_company_stock("02400.HK", "心动公司", "HK"))

    def test_set_focus_and_watchlist_order(self):
        database.add_company_stock("02400.HK", "心动公司", "HK")
        database.add_company_stock("601872.SH", "招商轮船", "A")
        self.assertTrue(database.set_company_focus("601872.SH", True))
        rows = database.get_company_watchlist()
        self.assertEqual("601872.SH", rows[0]["code"])  # 重点在前
        self.assertEqual(1, rows[0]["is_focus"])

    def test_latest_kline_price_rounded(self):
        database.upsert_klines("002241.SZ", [{
            "date": "2026-08-21", "open": 23.399999618530273,
            "high": 23.5, "low": 23.3, "close": 23.399999618530273, "volume": 1,
        }])
        stored = database.get_klines("002241.SZ")[-1]["close"]
        self.assertEqual(23.4, stored)
        latest = database.get_latest_klines()["002241.SZ"]
        self.assertEqual(23.4, latest["close"])

    def test_upsert_klines_idempotent(self):
        klines = _flat_klines(5)
        self.assertEqual(5, database.upsert_klines("02400.HK", klines))
        self.assertEqual(5, database.upsert_klines("02400.HK", klines))
        self.assertEqual(5, len(database.get_klines("02400.HK")))
        # upsert 覆盖旧值
        updated = dict(klines[-1], close=99.0)
        database.upsert_klines("02400.HK", [updated])
        self.assertEqual(99.0, database.get_klines("02400.HK")[-1]["close"])

    def test_save_signals_dedup(self):
        sig = {"code": "02400.HK", "date": "2026-08-07", "signal_type": "high_vol_up",
               "detail": "x"}
        self.assertEqual(1, len(database.save_signals([sig])))
        self.assertEqual([], database.save_signals([sig]))

    def test_get_signals_focus_before_type_priority(self):
        database.add_company_stock("02400.HK", "心动公司", "HK", is_focus=True)
        database.add_company_stock("601872.SH", "招商轮船", "A", is_focus=False)
        database.save_signals([
            {"code": "601872.SH", "date": "2026-08-21", "signal_type": "high_vol_up",
             "detail": "非重点放量"},
            {"code": "02400.HK", "date": "2026-08-21", "signal_type": "low_vol_down",
             "detail": "重点缩量"},
            {"code": "02400.HK", "date": "2026-08-21", "signal_type": "consolidation",
             "detail": "重点横盘"},
        ])
        rows = database.get_signals("2026-08-21")
        codes = [row["code"] for row in rows]
        self.assertEqual(["02400.HK", "02400.HK", "601872.SH"], codes)
        self.assertEqual(
            ["consolidation", "low_vol_down"],
            [row["signal_type"] for row in rows if row["code"] == "02400.HK"],
        )

    def test_has_recent_signal_window(self):
        database.save_signals([
            {"code": "02400.HK", "date": "2026-08-01", "signal_type": "high_vol_up",
             "detail": "x"},
        ])
        self.assertTrue(database.has_recent_signal(
            "02400.HK", "high_vol_up", 7, "2026-08-07"))
        self.assertFalse(database.has_recent_signal(
            "02400.HK", "high_vol_up", 3, "2026-08-07"))
        self.assertFalse(database.has_recent_signal(
            "02400.HK", "low_vol_down", 7, "2026-08-07"))


class CompanyApiTests(unittest.TestCase):
    def test_add_invalid_code_returns_400(self):
        response = app_mod.app.test_client().post(
            "/api/company-stocks",
            json={"code": "BAD", "name": "x", "market": "A"},
        )
        self.assertEqual(400, response.status_code)

    def test_add_market_suffix_mismatch_returns_400(self):
        response = app_mod.app.test_client().post(
            "/api/company-stocks",
            json={"code": "02400.HK", "name": "x", "market": "A"},
        )
        self.assertEqual(400, response.status_code)

    def test_add_duplicate_returns_409(self):
        with patch.object(app_mod.database, "add_company_stock", return_value=False):
            response = app_mod.app.test_client().post(
                "/api/company-stocks",
                json={"code": "02400.HK", "name": "心动公司", "market": "HK"},
            )
        self.assertEqual(409, response.status_code)

    def test_import_endpoint(self):
        added = []

        def add_stock(code, name, market, is_focus=False):
            if code == "02400.HK":
                return False
            added.append(code)
            return True

        with patch.object(app_mod.database, "add_company_stock", side_effect=add_stock):
            response = app_mod.app.test_client().post(
                "/api/company-stocks/import",
                json={"items": [
                    {"code": "601872.SH", "name": "招商轮船", "market": "A"},
                    {"code": "02400.HK", "name": "心动公司", "market": "HK"},
                    {"code": "INVALID", "name": "x", "market": "A"},
                ]},
            )
        data = response.get_json()
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, data["added"])
        self.assertEqual(1, data["skipped"])
        self.assertEqual(1, len(data["invalid"]))


class VolumeNormalizationTests(unittest.TestCase):
    def test_a_share_volume_divided_by_100(self):
        klines = [{"date": "2026-08-15", "close": 10.0, "volume": 51783300.0}]
        out = signals._normalize_yahoo_volume("601872.SH", klines)
        self.assertAlmostEqual(517833.0, out[0]["volume"])

    def test_bj_also_normalized(self):
        klines = [{"date": "2026-08-15", "close": 10.0, "volume": 100000.0}]
        out = signals._normalize_yahoo_volume("832566.BJ", klines)
        self.assertAlmostEqual(1000.0, out[0]["volume"])

    def test_hk_volume_unchanged(self):
        klines = [{"date": "2026-08-15", "close": 30.0, "volume": 1689123.0}]
        out = signals._normalize_yahoo_volume("02400.HK", klines)
        self.assertAlmostEqual(1689123.0, out[0]["volume"])

    def test_none_volume_kept(self):
        klines = [{"date": "2026-08-15", "close": 10.0, "volume": None}]
        out = signals._normalize_yahoo_volume("600519.SH", klines)
        self.assertIsNone(out[0]["volume"])


class FetchKlinesVolumeUnitTests(unittest.TestCase):
    @staticmethod
    def _ohlc(close):
        return {"Open": close * 0.99, "High": close * 1.01,
                "Low": close * 0.98, "Close": close}

    def _multi_ticker_df(self):
        import pandas as pd
        fields = ["Open", "High", "Low", "Close", "Volume"]
        tickers = ["601872.SS", "2400.HK"]
        cols = pd.MultiIndex.from_product([fields, tickers])
        row = []
        for field in fields:
            row.extend([
                self._ohlc(10.0).get(field, 51783300.0),
                self._ohlc(30.0).get(field, 1689123.0),
            ])
        return pd.DataFrame(
            [row], index=pd.DatetimeIndex([pd.Timestamp("2026-08-15")]), columns=cols)

    def test_batch_multi_symbol_normalized(self):
        with patch.object(signals.yf, "download",
                          return_value=self._multi_ticker_df()):
            result, errors = signals.fetch_daily_klines(
                ["601872.SH", "02400.HK"], period_days=12)
        self.assertEqual([], errors)
        self.assertAlmostEqual(517833.0, result["601872.SH"][0]["volume"])
        self.assertAlmostEqual(1689123.0, result["02400.HK"][0]["volume"])

    def test_batch_single_symbol_normalized(self):
        import pandas as pd
        df = pd.DataFrame(
            [{**self._ohlc(10.0), "Volume": 51783300.0}],
            index=pd.DatetimeIndex([pd.Timestamp("2026-08-15")]),
        )
        with patch.object(signals.yf, "download", return_value=df):
            result, errors = signals.fetch_daily_klines(["601872.SH"], period_days=12)
        self.assertEqual([], errors)
        self.assertAlmostEqual(517833.0, result["601872.SH"][0]["volume"])

    def test_single_fetch_fallback_normalized(self):
        with patch.object(signals.yf, "download", side_effect=RuntimeError("429")), \
             patch.object(signals, "_fetch_single", return_value=[
                 {"date": "2026-08-15", "open": 9.9, "high": 10.1, "low": 9.8,
                  "close": 10.0, "volume": 51783300.0}]):
            result, errors = signals.fetch_daily_klines(["601872.SH"], period_days=12)
        self.assertEqual([], errors)
        self.assertAlmostEqual(517833.0, result["601872.SH"][0]["volume"])


class SignalScanTests(unittest.TestCase):
    def test_all_fetch_failed_returns_error(self):
        with patch.object(app_mod.database, "get_company_watchlist",
                          return_value=[{"code": "02400.HK", "name": "心动公司",
                                         "market": "HK", "is_focus": 1}]), \
             patch.object(app_mod.database, "get_kline_counts", return_value={}), \
             patch.object(app_mod.signals, "fetch_daily_klines",
                          return_value=({}, ["02400.HK: 无数据"])), \
             patch.object(app_mod, "_deliver_pending_signal_notifications", return_value=[]):
            result = app_mod.do_scan_signals()
        self.assertEqual("error", result["status"])

    def test_empty_watchlist_ok(self):
        with patch.object(app_mod.database, "get_company_watchlist", return_value=[]), \
             patch.object(app_mod, "_deliver_pending_signal_notifications", return_value=[]):
            result = app_mod.do_scan_signals()
        self.assertEqual("ok", result["status"])


if __name__ == "__main__":
    unittest.main()
