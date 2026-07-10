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


if __name__ == "__main__":
    unittest.main()
