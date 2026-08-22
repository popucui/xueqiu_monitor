import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import database
import notifier


class NotificationDeliveryTests(unittest.TestCase):
    def test_failed_batch_returns_post_ids_for_outbox(self):
        posts = [
            {"id": "post-1", "user_name": "A", "title": "one", "created_at": 1},
            {"id": "post-2", "user_name": "A", "title": "two", "created_at": 2},
        ]
        with patch.object(notifier, "_post_with_retry", return_value="timeout"):
            result = notifier.deliver_post_notifications(
                posts,
                "wechat",
                "https://example.invalid/hook",
            )

        self.assertEqual([], result["sent_ids"])
        self.assertEqual(["post-1", "post-2"], result["failures"][0]["post_ids"])
        self.assertEqual("timeout", result["failures"][0]["error"])


class NotificationOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path_patch = patch.object(
            database,
            "_DB_PATH",
            str(Path(self.temp_dir.name) / "test.db"),
        )
        self.db_path_patch.start()
        database.init_db()
        self.post = {
            "id": "post-1",
            "user_id": "1",
            "user_name": "A",
            "title": "title",
            "text": "text",
            "created_at": 1,
        }
        database.save_posts([self.post])
        database.enqueue_post_notifications([self.post], ["wechat"])

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_failed_delivery_stays_pending_and_success_clears_it(self):
        with patch.object(app.config, "WECHAT_WEBHOOK_URL", "https://example.invalid/hook"), \
             patch.object(app.config, "FEISHU_WEBHOOK_URL", ""), \
             patch.object(app.config, "DINGTALK_WEBHOOK_URL", ""), \
             patch.object(
                 app,
                 "deliver_post_notifications",
                 return_value={
                     "sent_ids": [],
                     "failures": [{"post_ids": ["post-1"], "error": "timeout"}],
                 },
             ):
            errors = app._deliver_pending_post_notifications()

        pending = database.get_pending_post_notifications("wechat")
        self.assertEqual(["wechat: timeout"], errors)
        self.assertEqual(1, len(pending))
        self.assertEqual(1, pending[0]["attempts"])
        self.assertEqual("timeout", pending[0]["notification_last_error"])

        with patch.object(app.config, "WECHAT_WEBHOOK_URL", "https://example.invalid/hook"), \
             patch.object(app.config, "FEISHU_WEBHOOK_URL", ""), \
             patch.object(app.config, "DINGTALK_WEBHOOK_URL", ""), \
             patch.object(
                 app,
                 "deliver_post_notifications",
                 return_value={"sent_ids": ["post-1"], "failures": []},
             ):
            errors = app._deliver_pending_post_notifications()

        self.assertEqual([], errors)
        self.assertEqual([], database.get_pending_post_notifications("wechat"))

    def test_outbox_entry_dropped_after_max_attempts(self):
        expired = database.fail_post_notifications(
            "wechat", ["post-1"], "timeout", max_attempts=1
        )

        self.assertEqual(["post-1"], expired)
        self.assertEqual([], database.get_pending_post_notifications("wechat"))

    def test_outbox_entry_kept_below_max_attempts(self):
        expired = database.fail_post_notifications(
            "wechat", ["post-1"], "timeout", max_attempts=2
        )

        self.assertEqual([], expired)
        pending = database.get_pending_post_notifications("wechat")
        self.assertEqual(1, len(pending))
        self.assertEqual(1, pending[0]["attempts"])


class SignalNotificationTests(unittest.TestCase):
    def test_failed_batch_returns_items_for_outbox(self):
        signals = [
            {"code": "02400.HK", "date": "2026-08-21", "signal_type": "high_vol_up",
             "detail": "x", "name": "心动公司"},
            {"code": "601872.SH", "date": "2026-08-21", "signal_type": "low_vol_down",
             "detail": "y", "name": "招商轮船"},
        ]
        with patch.object(notifier, "_post_with_retry", return_value="timeout"):
            result = notifier.deliver_signal_notifications(
                signals, "wechat", "https://example.invalid/hook",
            )

        self.assertEqual([], result["sent"])
        self.assertEqual(
            ["02400.HK", "601872.SH"],
            [item["code"] for item in result["failures"][0]["items"]],
        )

    def test_signals_are_split_when_markdown_exceeds_limit(self):
        signals = [
            {"code": f"60000{i}.SH", "date": "2026-08-21", "signal_type": "high_vol_up",
             "detail": "x" * 80, "name": f"公司{i}"}
            for i in range(8)
        ]
        with patch.object(notifier, "_WECHAT_MD_LIMIT", 400), \
             patch.object(notifier, "_post_with_retry", return_value=None) as post:
            result = notifier.deliver_signal_notifications(
                signals, "wechat", "https://example.invalid/hook",
            )

        self.assertGreater(post.call_count, 1)
        self.assertEqual(8, len(result["sent"]))
        self.assertEqual([], result["failures"])


class SignalOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path_patch = patch.object(
            database,
            "_DB_PATH",
            str(Path(self.temp_dir.name) / "test.db"),
        )
        self.db_path_patch.start()
        database.init_db()
        self.signal = {
            "code": "02400.HK",
            "date": "2026-08-21",
            "signal_type": "high_vol_up",
            "detail": "涨 5%",
            "name": "心动公司",
        }
        database.add_company_stock("02400.HK", "心动公司", "HK", is_focus=True)
        database.save_signals([self.signal])
        database.enqueue_signal_notifications([self.signal], ["wechat"])

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_failed_delivery_stays_pending_and_success_clears_it(self):
        with patch.object(app.config, "WECHAT_WEBHOOK_URL", "https://example.invalid/hook"), \
             patch.object(app.config, "FEISHU_WEBHOOK_URL", ""), \
             patch.object(app.config, "DINGTALK_WEBHOOK_URL", ""), \
             patch.object(
                 app,
                 "deliver_signal_notifications",
                 return_value={
                     "sent": [],
                     "failures": [{"items": [self.signal], "error": "timeout"}],
                 },
             ):
            errors = app._deliver_pending_signal_notifications()

        pending = database.get_pending_signal_notifications("wechat")
        self.assertEqual(["wechat: timeout"], errors)
        self.assertEqual(1, len(pending))
        self.assertEqual(1, pending[0]["attempts"])

        with patch.object(app.config, "WECHAT_WEBHOOK_URL", "https://example.invalid/hook"), \
             patch.object(app.config, "FEISHU_WEBHOOK_URL", ""), \
             patch.object(app.config, "DINGTALK_WEBHOOK_URL", ""), \
             patch.object(
                 app,
                 "deliver_signal_notifications",
                 return_value={"sent": [self.signal], "failures": []},
             ):
            errors = app._deliver_pending_signal_notifications()

        self.assertEqual([], errors)
        self.assertEqual([], database.get_pending_signal_notifications("wechat"))

    def test_outbox_entry_dropped_after_max_attempts(self):
        expired = database.fail_signal_notifications(
            "wechat", [self.signal], "timeout", max_attempts=1
        )

        self.assertEqual([("02400.HK", "2026-08-21", "high_vol_up")], expired)
        self.assertEqual([], database.get_pending_signal_notifications("wechat"))


if __name__ == "__main__":
    unittest.main()
