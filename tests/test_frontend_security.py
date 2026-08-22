import unittest
from pathlib import Path


class FrontendSecurityTests(unittest.TestCase):
    def test_stock_modal_uses_dom_event_listener(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "announcements.html"
        ).read_text(encoding="utf-8")
        function_body = template.split("function renderModalStocks", 1)[1].split(
            "function safeExternalUrl", 1
        )[0]

        self.assertNotIn("onclick=", function_body)
        self.assertIn("addEventListener('click'", function_body)
        self.assertIn("textContent", function_body)

    def test_signal_cards_group_same_company(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "companies.html"
        ).read_text(encoding="utf-8")
        function_body = template.split("function renderSignals", 1)[1].split(
            "async function scanSignals", 1
        )[0]
        self.assertIn("groupSignals", function_body)
        self.assertIn("signal-badges", function_body)
        self.assertNotIn("innerHTML", function_body)

    def test_company_table_delete_uses_dom_listener(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "companies.html"
        ).read_text(encoding="utf-8")
        function_body = template.split("function renderCompanies", 1)[1].split(
            "function renderModalCompanies", 1
        )[0]

        self.assertNotIn("onclick=", function_body)
        self.assertIn("addEventListener('click'", function_body)
        self.assertIn("textContent", function_body)

    def test_index_posts_use_safe_external_url(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        render_posts = template.split("function renderPosts", 1)[1].split(
            "function getEnrichedAuthors", 1
        )[0]

        self.assertIn("function safeExternalUrl", template)
        self.assertIn("postTargetUrl", render_posts)
        self.assertNotIn("startsWith('http')", render_posts)


if __name__ == "__main__":
    unittest.main()
