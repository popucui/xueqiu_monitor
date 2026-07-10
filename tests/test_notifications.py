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


if __name__ == "__main__":
    unittest.main()
