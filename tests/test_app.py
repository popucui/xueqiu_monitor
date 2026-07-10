import unittest
from unittest.mock import patch

import app


class FakeFetcher:
    def fetch_all_authors(self, *args, **kwargs):
        return [], [{"user_id": "1", "name": "A", "error": "upstream down"}]


class RefreshStatusTests(unittest.TestCase):
    def setUp(self):
        self.old_post_fetch_time = app._last_fetch_time
        self.old_announcement_fetch_time = app._last_announcement_fetch_time

    def tearDown(self):
        app._last_fetch_time = self.old_post_fetch_time
        app._last_announcement_fetch_time = self.old_announcement_fetch_time

    def test_all_authors_failed_does_not_advance_freshness(self):
        app._last_fetch_time = "previous-success"
        with patch.object(app, "_get_fetcher", return_value=FakeFetcher()), \
             patch.object(app, "_run_on_fetcher_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app.database, "get_db_authors", return_value=[{"user_id": "1", "name": "A"}]), \
             patch.object(app.database, "get_authors_summary", return_value=[]), \
             patch.object(app.database, "save_posts", return_value=[]), \
             patch.object(app, "_deliver_pending_post_notifications", return_value=[]), \
             patch.object(app, "_stop_fetcher"):
            result = app.do_fetch(wait_timeout=0)

        self.assertEqual("error", result["status"])
        self.assertEqual("previous-success", app._last_fetch_time)

    def test_all_announcement_stocks_failed_does_not_advance_freshness(self):
        app._last_announcement_fetch_time = "previous-success"
        with patch.object(app.database, "get_announcement_watchlist", return_value=[{"code": "00001.HK"}]), \
             patch.object(app.announcements, "fetch_for_watchlist", return_value=([], ["00001.HK: down"])), \
             patch.object(app.database, "save_announcements", return_value=[]):
            result = app.do_fetch_announcements()

        self.assertEqual("error", result["status"])
        self.assertEqual("previous-success", app._last_announcement_fetch_time)

    def test_all_prices_failed_returns_error(self):
        with patch.object(app, "fetch_prices", return_value={"_errors": ["all down"]}), \
             patch.object(app.database, "save_prices"), \
             patch.object(app, "notify_prices"):
            result = app.do_fetch_prices()

        self.assertEqual("error", result["status"])
        self.assertEqual(["all down"], result["errors"])


class AnnouncementStockApiTests(unittest.TestCase):
    def test_blank_keyword_string_uses_defaults(self):
        captured = {}

        def add_stock(*args, **kwargs):
            captured.update(kwargs)
            return True

        with patch.object(app.database, "add_announcement_stock", side_effect=add_stock):
            response = app.app.test_client().post(
                "/api/announcement-stocks",
                json={
                    "code": "00001.HK",
                    "name": "Test",
                    "source": "hkex",
                    "keywords": "",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertIsNone(captured["keywords"])


if __name__ == "__main__":
    unittest.main()
