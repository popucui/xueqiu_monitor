import unittest
from unittest.mock import MagicMock, patch

from fetcher import XueqiuFetcher


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


if __name__ == "__main__":
    unittest.main()
