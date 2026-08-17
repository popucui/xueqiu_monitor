import json
import unittest
from datetime import date
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import announcements


class AnnouncementPaginationTests(unittest.TestCase):
    def test_cninfo_fetches_every_page(self):
        requested_pages = []

        def fake_http_json(url, **kwargs):
            page = int(kwargs["data"]["pageNum"])
            requested_pages.append(page)
            return {
                "totalAnnouncement": 2,
                "hasMore": page == 1,
                "announcements": [
                    {
                        "announcementId": str(page),
                        "announcementTitle": f"title-{page}",
                        "announcementTime": 0,
                        "adjunctUrl": f"{page}.pdf",
                    }
                ],
            }

        stock = {
            "code": "600000.SH",
            "name": "Test",
            "org_id": "org",
            "keywords": [],
        }
        with patch.object(announcements, "http_json", side_effect=fake_http_json):
            rows = announcements.fetch_cninfo(
                stock,
                date(2026, 7, 1),
                date(2026, 7, 10),
                page_size=1,
            )

        self.assertEqual([1, 2], requested_pages)
        self.assertEqual(["1", "2"], [row.ann_id for row in rows])

    def test_hkex_expands_row_range_until_complete(self):
        requested_ranges = []
        first = {
            "NEWS_ID": "1",
            "TITLE": "one",
            "FILE_LINK": "/one.pdf",
            "DATE_TIME": "01/07/2026 08:00",
        }
        second = {
            "NEWS_ID": "2",
            "TITLE": "two",
            "FILE_LINK": "/two.pdf",
            "DATE_TIME": "02/07/2026 08:00",
        }

        def fake_http_json(url, **kwargs):
            row_range = int(parse_qs(urlparse(url).query)["rowRange"][0])
            requested_ranges.append(row_range)
            items = [first] if row_range == 1 else [first, second]
            return {
                "result": json.dumps(items),
                "hasNextRow": row_range == 1,
                "loadedRecord": len(items),
                "recordCnt": 2,
            }

        stock = {
            "code": "00001.HK",
            "name": "Test",
            "stock_id": "stock",
            "keywords": [],
        }
        with patch.object(announcements, "http_json", side_effect=fake_http_json):
            rows = announcements.fetch_hkex(
                stock,
                date(2026, 7, 1),
                date(2026, 7, 10),
                page_size=1,
            )

        self.assertEqual([1, 2], requested_ranges)
        self.assertEqual(["1", "2"], [row.ann_id for row in rows])


class SentimentClassificationTests(unittest.TestCase):
    def test_positive_keywords(self):
        self.assertEqual("positive", announcements.classify_sentiment("关于回购公司股份的进展公告"))
        self.assertEqual("positive", announcements.classify_sentiment("Repurchase of Shares"))

    def test_negative_keywords(self):
        self.assertEqual("negative", announcements.classify_sentiment("关于股东减持计划的公告"))
        self.assertEqual("negative", announcements.classify_sentiment("Profit Warning Announcement"))

    def test_negative_wins_on_conflict(self):
        self.assertEqual("negative", announcements.classify_sentiment("回购股份暨立案调查进展"))

    def test_neutral(self):
        self.assertEqual("", announcements.classify_sentiment("2026年半年度报告"))

    def test_save_and_read_sentiment(self):
        import tempfile
        from pathlib import Path
        import database
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(database, "_DB_PATH", str(Path(temp_dir) / "test.db")):
                database.init_db()
                ann = announcements.Announcement(
                    source="cninfo", stock_code="600001.SH", stock_name="测试",
                    ann_id="a1", title="关于回购公司股份的公告",
                    published_at="2026-08-08 10:00", url="",
                    matched_keywords="", sentiment="positive",
                )
                self.assertEqual(1, len(database.save_announcements([ann])))
                rows = database.get_recent_announcements()
                self.assertEqual("positive", rows[0]["sentiment"])

    def test_exemption_for_release_pledge(self):
        self.assertEqual("positive", announcements.classify_sentiment("关于股东部分股份解除质押的公告"))

    def test_exemption_for_hkex_profit_alert_tag(self):
        title = ("PROFIT ALERT - Announcements and Notices - "
                 "[Profit Warning / Inside Information]")
        self.assertEqual("positive", announcements.classify_sentiment(title))

    def test_real_profit_warning_still_negative(self):
        self.assertEqual("negative", announcements.classify_sentiment(
            "盈利警告 - Announcements and Notices - [Profit Warning / Inside Information]"))


if __name__ == "__main__":
    unittest.main()
