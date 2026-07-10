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


if __name__ == "__main__":
    unittest.main()
