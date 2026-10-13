import json
import unittest
from unittest.mock import MagicMock, patch

from fetcher import FetchUserPostsError, XueqiuFetcher


def _status(status_id, created_at, mark=0, text=""):
    return {
        "id": status_id,
        "created_at": created_at,
        "mark": mark,
        "text": text or f"post-{status_id}",
        "user": {"screen_name": "A"},
    }


class FetchUserPostsTests(unittest.TestCase):
    def setUp(self):
        self.fetcher = XueqiuFetcher("token-a", "token-r")
        self.fetcher._page = MagicMock()

    def test_pinned_old_post_does_not_stop_window(self):
        since_ms = 1_770_000_000_000
        payload = [
            _status(1, 1_000_000_000_000, mark=1, text="old pin"),
            _status(2, since_ms + 1000, text="new post"),
        ]
        with patch.object(
            self.fetcher, "_fetch_api", return_value=(200, {"statuses": payload})
        ):
            posts = self.fetcher.fetch_user_posts(
                "123", since_ms=since_ms, page_size=20
            )

        self.assertEqual(["2"], [p["id"] for p in posts])
        self.assertIn("new post", posts[0]["text"])

    def test_page1_error_code_raises(self):
        with patch.object(
            self.fetcher,
            "_fetch_api",
            return_value=(
                400,
                {"error_code": "10022", "error_description": "请登录雪球查看更多内容"},
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.fetcher.fetch_user_posts("123", since_ms=1, page_size=20)

        self.assertIn("page=1", str(ctx.exception))
        self.assertIn("10022", str(ctx.exception))

    def test_page2_error_code_keeps_page1_posts(self):
        page1 = [_status(i, 1_780_000_000_000) for i in range(1, 21)]
        pages = []

        def fake_fetch(_path, params):
            pages.append(params["page"])
            if params["page"] == 1:
                return 200, {"statuses": page1}
            return 400, {
                "error_code": "10022",
                "error_description": "请登录雪球查看更多内容",
            }

        with patch.object(self.fetcher, "_fetch_api", side_effect=fake_fetch):
            posts = self.fetcher.fetch_user_posts(
                "123", since_ms=1_000_000_000_000, page_size=20
            )

        self.assertEqual([1, 2], pages)
        self.assertEqual(20, len(posts))
        self.assertEqual("1", posts[0]["id"])
        self.assertEqual("20", posts[-1]["id"])

    def test_page1_4xx_without_error_code_raises(self):
        with patch.object(
            self.fetcher, "_fetch_api", return_value=(403, {"message": "denied"})
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.fetcher.fetch_user_posts("123", page_size=20)

        self.assertIn("page=1", str(ctx.exception))
        self.assertIn("无 error_code", str(ctx.exception))

    def test_failed_later_page_keeps_posts_and_continues_next_author(self):
        first = _status(1, 1_780_000_000_000)
        first["user"] = {}
        with patch.object(self.fetcher, "_fetch_api", side_effect=[
            (200, {"statuses": [first]}),
            RuntimeError("WAF retry exhausted"),
            (200, {"statuses": [_status(2, 1_780_000_000_000)]}),
            (200, {"statuses": []}),
        ]):
            posts, errors = self.fetcher.fetch_all_authors(
                [{"id": "123", "name": "First"}, {"id": "456", "name": "Second"}],
                page_size=1,
            )

        self.assertEqual(["1", "2"], [p["id"] for p in posts])
        self.assertEqual("First", posts[0]["user_name"])
        self.assertEqual(1, len(errors))
        self.assertEqual("123", errors[0]["user_id"])
        self.assertIn("page=2", errors[0]["error"])
        self.assertIn("WAF retry exhausted", errors[0]["error"])

    def test_failed_first_page_reports_error_without_posts(self):
        with patch.object(self.fetcher, "_fetch_api", side_effect=RuntimeError("timeout")):
            with self.assertRaises(FetchUserPostsError) as ctx:
                self.fetcher.fetch_user_posts("123")
        self.assertEqual([], ctx.exception.posts)
        self.assertIn("user=123 page=1", str(ctx.exception))


class WafRecoveryTests(unittest.TestCase):
    CHALLENGE = {"status": 200, "body": '<script>document.cookie="EO_Bot_Ssid=test";</script>'}

    def setUp(self):
        self.fetcher = XueqiuFetcher("token-a", "token-r")
        self.fetcher._page = MagicMock()
        self.path = "/v4/statuses/user_timeline.json"
        self.params = {"user_id": "123", "page": 2, "count": 20}

    def test_mid_session_challenge_recovers_same_request_once(self):
        data = {"statuses": [_status(1, 1_780_000_000_000)]}
        self.fetcher._page.evaluate.side_effect = [
            self.CHALLENGE, {"status": 200, "body": json.dumps(data)},
        ]
        self.assertEqual((200, data), self.fetcher._fetch_api(self.path, self.params))
        self.fetcher._page.goto.assert_called_once_with(
            "https://xueqiu.com/v4/statuses/user_timeline.json?user_id=123&page=2&count=20",
            wait_until="domcontentloaded", timeout=self.fetcher._API_TIMEOUT_MS,
        )
        calls = self.fetcher._page.evaluate.call_args_list
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0], calls[1])

    def test_persistent_challenge_stops_after_one_retry(self):
        self.fetcher._page.evaluate.return_value = self.CHALLENGE
        with self.assertRaisesRegex(RuntimeError, "non-json"):
            self.fetcher._fetch_api(self.path, self.params)
        self.assertEqual(2, self.fetcher._page.evaluate.call_count)
        self.fetcher._page.goto.assert_called_once()

    def test_recovery_navigation_timeout_is_reported(self):
        self.fetcher._page.evaluate.return_value = self.CHALLENGE
        self.fetcher._page.goto.side_effect = RuntimeError("navigation timeout")
        with self.assertRaisesRegex(RuntimeError, "WAF 验证恢复失败.*navigation timeout"):
            self.fetcher._fetch_api(self.path, self.params)
        self.assertEqual(1, self.fetcher._page.evaluate.call_count)

    def test_unrecognized_html_and_non_object_json_fail_without_navigation(self):
        for body in ["<html>Bad gateway</html>", "null", "[]"]:
            with self.subTest(body=body):
                self.fetcher._page.evaluate.return_value = {"status": 200, "body": body}
                with self.assertRaises(RuntimeError):
                    self.fetcher._fetch_api(self.path, self.params)
        self.fetcher._page.goto.assert_not_called()

    def test_login_pagination_boundary_does_not_trigger_recovery(self):
        data = {"error_code": "10022", "error_description": "请登录雪球查看更多内容"}
        self.fetcher._page.evaluate.return_value = {"status": 400, "body": json.dumps(data)}
        self.assertEqual((400, data), self.fetcher._fetch_api(self.path, self.params))
        self.fetcher._page.goto.assert_not_called()

    def test_server_error_is_reported_without_navigation(self):
        self.fetcher._page.evaluate.return_value = {"status": 503, "body": '{"message":"busy"}'}
        with self.assertRaisesRegex(RuntimeError, "server error status=503"):
            self.fetcher._fetch_api(self.path, self.params)
        self.fetcher._page.goto.assert_not_called()


if __name__ == "__main__":
    unittest.main()
