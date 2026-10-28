"""Tests for Firecrawl scraper integration."""
import unittest
from unittest.mock import AsyncMock, patch

from app.schemas import CompetitorProduct
from app.services.firecrawl_scraper import scrape_with_firecrawl


class TestFirecrawlScraper(unittest.IsolatedAsyncioTestCase):
    async def test_missing_api_key_raises_value_error(self):
        with patch("app.services.firecrawl_scraper.settings.firecrawl_api_key", ""):
            with self.assertRaises(ValueError):
                await scrape_with_firecrawl("https://example.com")

    async def test_mocked_firecrawl_scrape_success(self):
        fake_md = "# Pricing\n- Basic: 100.000d/month\n- Pro: 300.000d/month"
        fake_product = CompetitorProduct(
            product_name="Basic Hosting",
            price_raw="100.000đ/tháng",
            billing_cycle="month",
            stock_status_raw="IN_STOCK",
            source_url="https://example.com",
        )

        with patch("app.services.firecrawl_scraper.settings.firecrawl_api_key", "fc-test-key"):
            with patch("httpx.AsyncClient.post") as mock_post:
                mock_resp = unittest.mock.MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "success": True,
                    "data": {"markdown": fake_md},
                }
                mock_post.return_value = mock_resp

                with patch("app.services.firecrawl_scraper.llm.generate_role_json", new_callable=AsyncMock) as mock_llm:
                    mock_llm.return_value = unittest.mock.MagicMock(products=[fake_product])

                    products = await scrape_with_firecrawl("https://example.com")
                    self.assertEqual(len(products), 1)
                    self.assertEqual(products[0].name, "Basic Hosting")
                    self.assertEqual(products[0].raw_price_text, "100.000đ/tháng")


if __name__ == "__main__":
    unittest.main()
