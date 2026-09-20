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
             patch.object(app.database, "save_prices") as save_prices, \
             patch.object(app, "notify_prices") as notify_prices:
            result = app.do_fetch_prices()

        self.assertEqual("error", result["status"])
        self.assertEqual(["all down"], result["errors"])
        save_prices.assert_not_called()
        notify_prices.assert_not_called()

    def test_db_failure_does_not_restart_fetcher(self):
        class OkFetcher:
            def fetch_all_authors(self, *args, **kwargs):
                return [], []

        with patch.object(app, "_get_fetcher", return_value=OkFetcher()), \
             patch.object(app, "_run_on_fetcher_thread", side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             patch.object(app.database, "get_db_authors", return_value=[{"user_id": "1", "name": "A"}]), \
             patch.object(app.database, "get_authors_summary", return_value=[]), \
             patch.object(app.database, "save_posts", side_effect=RuntimeError("sqlite locked")), \
             patch.object(app, "_stop_fetcher") as stop_fetcher:
            result = app.do_fetch(wait_timeout=0)

        self.assertEqual("error", result["status"])
        stop_fetcher.assert_not_called()

    def test_fetcher_failure_restarts_fetcher(self):
        class OkFetcher:
            def fetch_all_authors(self, *args, **kwargs):
                return [], []

        with patch.object(app, "_get_fetcher", return_value=OkFetcher()), \
             patch.object(app, "_run_on_fetcher_thread", side_effect=RuntimeError("browser crashed")), \
             patch.object(app.database, "get_db_authors", return_value=[{"user_id": "1", "name": "A"}]), \
             patch.object(app.database, "get_authors_summary", return_value=[]), \
             patch.object(app, "_stop_fetcher") as stop_fetcher:
            result = app.do_fetch(wait_timeout=0)

        self.assertEqual("error", result["status"])
        stop_fetcher.assert_called_once()


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


class TimezoneTests(unittest.TestCase):
    def test_now_uses_shanghai(self):
        self.assertEqual("Asia/Shanghai", str(app.config.TZ))
        self.assertEqual("Asia/Shanghai", str(app.config.now().tzinfo))


class WriteProtectionTests(unittest.TestCase):
    _XHR = {"X-Requested-With": "XMLHttpRequest"}

    def test_refresh_requires_xhr_header(self):
        for path, target in (
            ("/api/refresh", "do_fetch"),
            ("/api/prices/refresh", "do_fetch_prices"),
            ("/api/announcements/refresh", "do_fetch_announcements"),
            ("/api/signals/refresh", "do_scan_signals"),
        ):
            with self.subTest(path=path), patch.object(app, target) as mocked:
                response = app.app.test_client().post(path)
            self.assertEqual(403, response.status_code)
            mocked.assert_not_called()

    def test_refresh_ok_with_xhr_header(self):
        with patch.object(app, "do_fetch", return_value={"status": "ok"}):
            response = app.app.test_client().post("/api/refresh", headers=self._XHR)
        self.assertEqual(200, response.status_code)
        self.assertEqual("ok", response.get_json()["status"])

    def test_write_blocked_when_read_only(self):
        with patch.object(app.config, "WEB_READ_ONLY", True):
            response = app.app.test_client().post(
                "/api/authors",
                json={"user_id": "1", "name": "A"},
            )
        self.assertEqual(403, response.status_code)
        self.assertIn("只读", response.get_json()["message"])

    def test_write_requires_api_token(self):
        payload = {"user_id": "1", "name": "A"}
        with patch.object(app.config, "WEB_API_TOKEN", "secret"), \
             patch.object(app.database, "add_author", return_value=True):
            denied = app.app.test_client().post("/api/authors", json=payload)
            allowed = app.app.test_client().post(
                "/api/authors",
                json=payload,
                headers={"X-API-Token": "secret"},
            )
        self.assertEqual(403, denied.status_code)
        self.assertEqual(200, allowed.status_code)

    def test_api_read_requires_token(self):
        with patch.object(app.config, "WEB_API_TOKEN", "secret"):
            denied = app.app.test_client().get("/api/authors")
            allowed = app.app.test_client().get(
                "/api/authors", headers={"X-API-Token": "secret"}
            )
        self.assertEqual(401, denied.status_code)
        self.assertEqual(200, allowed.status_code)

    def test_page_shows_auth_prompt_without_token(self):
        with patch.object(app.config, "WEB_API_TOKEN", "secret"):
            response = app.app.test_client().get("/")
        body = response.get_data(as_text=True)
        self.assertEqual(401, response.status_code)
        self.assertIn("WEB_API_TOKEN", body)
        # 未鉴权页面绝不能泄露令牌本身
        self.assertNotIn('content="secret"', body)

    def test_auth_login_sets_session(self):
        client = app.app.test_client()
        with patch.object(app.config, "WEB_API_TOKEN", "secret"), \
             patch.object(app.database, "get_authors_summary", return_value=[]), \
             patch.object(app.database, "get_db_authors", return_value=[]), \
             patch.object(app.database, "count_posts", return_value=0):
            bad = client.post("/auth", data={"token": "wrong"})
            self.assertEqual(401, bad.status_code)
            ok = client.post("/auth", data={"token": "secret"})
            self.assertEqual(302, ok.status_code)
            response = client.get("/api/authors")
        self.assertEqual(200, response.status_code)

    def test_session_does_not_authorize_writes(self):
        """session 登录态只能读；写请求必须带 X-API-Token，防跨站表单借登录态 CSRF。"""
        client = app.app.test_client()
        with patch.object(app.config, "WEB_API_TOKEN", "secret"), \
             patch.object(app.database, "add_author", return_value=True):
            client.post("/auth", data={"token": "secret"})
            response = client.post("/api/authors", json={"user_id": "1", "name": "A"})
        self.assertEqual(403, response.status_code)

    def test_static_assets_not_gated(self):
        with patch.object(app.config, "WEB_API_TOKEN", "secret"):
            response = app.app.test_client().get("/static/api.js")
        self.assertEqual(200, response.status_code)


if __name__ == "__main__":
    unittest.main()
