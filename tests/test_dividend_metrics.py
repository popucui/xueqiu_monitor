import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import dividend_metrics
import database


class DividendMetricTests(unittest.TestCase):
    def test_demo_ttm_is_dividend_over_latest_close(self):
        rows = dividend_metrics.get_ttm_metrics(
            [{"code": "02400.HK"}, {"code": "99999.HK"}],
            {"02400.HK": {"close": 40.0}},
        )
        self.assertEqual(2.25, rows["02400.HK"]["ttm_yield_pct"])
        self.assertEqual(0.45, rows["02400.HK"]["ttm_buyback_yield_pct"])
        self.assertEqual(2.70, rows["02400.HK"]["shareholder_yield_pct"])
        self.assertEqual("demo", rows["02400.HK"]["ttm_status"])
        self.assertNotIn("99999.HK", rows)

    def test_missing_price_is_not_a_zero_yield(self):
        rows = dividend_metrics.get_ttm_metrics(
            [{"code": "02400.HK"}], {},
        )
        self.assertEqual({}, rows)

    def test_actual_events_override_demo_and_sum_shareholder_return(self):
        rows = dividend_metrics.get_ttm_metrics(
            [{"code": "02400.HK"}],
            {"02400.HK": {"close": 40.0}},
            [
                {"code": "02400.HK", "event_type": "dividend", "status": "executed", "amount": 0.80},
                {"code": "02400.HK", "event_type": "buyback", "status": "executed",
                 "amount": 2.0, "market_cap": 200.0},
                {"code": "02400.HK", "event_type": "buyback", "status": "cancelled",
                 "amount": 100.0, "market_cap": 100.0},
            ],
        )
        self.assertEqual("actual", rows["02400.HK"]["ttm_status"])
        self.assertEqual(2.0, rows["02400.HK"]["ttm_yield_pct"])
        self.assertEqual(1.0, rows["02400.HK"]["ttm_buyback_yield_pct"])
        self.assertEqual(3.0, rows["02400.HK"]["shareholder_yield_pct"])


class CapitalReturnDbTests(unittest.TestCase):
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

    def test_capital_return_events_are_idempotent(self):
        event = {
            "event_id": "demo-div-1", "code": "02400.HK", "event_type": "dividend",
            "status": "executed", "amount": 0.8, "event_date": "2026-06-01",
            "source": "test",
        }
        self.assertEqual(1, database.upsert_capital_return_events([event]))
        self.assertEqual(1, database.upsert_capital_return_events([dict(event, amount=0.9)]))
        rows = database.get_capital_return_events(["02400.HK"], "2026-01-01", "2026-12-31")
        self.assertEqual(1, len(rows))
        self.assertEqual(0.9, rows[0]["amount"])
