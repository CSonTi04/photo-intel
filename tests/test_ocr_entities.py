"""Tests for OCR entity extraction patterns."""

import re

from src.tasks.handlers.ocr_handler import OCREntitiesHandler

handler = OCREntitiesHandler()


class TestEntityPatterns:
    """Test regex patterns for entity detection."""

    def _find_all(self, entity_type: str, text: str) -> list:
        patterns = handler.PATTERNS.get(entity_type, [])
        found = set()
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            found.update(matches)
        return sorted(found)

    # Dates
    def test_iso_date(self):
        assert self._find_all("dates", "Meeting on 2024-03-15 at noon")

    def test_eu_date(self):
        assert self._find_all("dates", "Deadline: 15/03/2024")

    def test_english_date(self):
        results = self._find_all("dates", "Due by March 15, 2024")
        assert len(results) > 0

    # URLs
    def test_https_url(self):
        results = self._find_all("urls", "Visit https://example.com/page for details")
        assert any("example.com" in r for r in results)

    def test_www_url(self):
        results = self._find_all("urls", "Go to www.example.com")
        assert len(results) > 0

    # Prices
    def test_dollar_price(self):
        results = self._find_all("prices", "Total: $49.99")
        assert len(results) > 0

    def test_euro_price(self):
        results = self._find_all("prices", "Price: €29.99")
        assert len(results) > 0

    def test_huf_price(self):
        results = self._find_all("prices", "Ár: 5990 Ft")
        assert len(results) > 0

    # Emails
    def test_email(self):
        results = self._find_all("emails", "Contact: user@example.com for info")
        assert "user@example.com" in results

    # Mixed content
    def test_mixed_entities(self):
        text = """
        Order #12345
        Date: 2024-03-15
        Amount: $199.99
        Confirmation: https://shop.example.com/order/12345
        Contact: support@shop.com
        Delivery: 1052 Budapest
        """
        dates = self._find_all("dates", text)
        prices = self._find_all("prices", text)
        urls = self._find_all("urls", text)
        emails = self._find_all("emails", text)

        assert len(dates) > 0
        assert len(prices) > 0
        assert len(urls) > 0
        assert len(emails) > 0
