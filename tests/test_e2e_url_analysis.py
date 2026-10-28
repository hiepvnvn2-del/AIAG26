import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.schemas import CompetitorProduct
from app.services.orchestrator import run_url_analysis


class TestE2EURLAnalysis(unittest.TestCase):
    def test_e2e_partial_failure_continues_analysis(self):
        url_blocked = "http://127.0.0.1:8000/demo/private.html"  # SSRF blocked
        url_valid = "https://example.com/pricing"  # Valid public domain

        mock_products = [
            CompetitorProduct(
                name="Hosting Starter Standard",
                price_vnd=210000,
                original_price_vnd=250000,
                raw_price_text="210.000đ/tháng",
                billing_cycle="month",
                stock="IN_STOCK",
                specs={"ram": "2GB", "ssd": "20GB"},
                source_url=url_valid,
            ),
            CompetitorProduct(
                name="Hosting Enterprise Business",
                price_vnd=520000,
                original_price_vnd=None,
                raw_price_text="520.000đ/tháng",
                billing_cycle="month",
                stock="IN_STOCK",
                specs={"ram": "4GB", "ssd": "50GB"},
                source_url=url_valid,
            ),
        ]

        async def run_test():
            # Mock scrape_generic on BrowserWorker so test doesn't depend on live network
            with patch("app.services.orchestrator.BrowserWorker.scrape_generic", new_callable=AsyncMock) as mock_scrape:
                mock_scrape.return_value = mock_products
                report = await run_url_analysis([url_blocked, url_valid])
                return report

        report = asyncio.run(run_test())

        # 1. Blocked URL must appear in failed_urls
        self.assertIn(url_blocked, report.failed_urls)
        self.assertIn("loopback", report.failed_urls[url_blocked].lower())

        # 2. Valid URL products must be analyzed
        self.assertEqual(len(report.matches), 2)
        skus = [m.our_sku for m in report.matches]
        self.assertIn("HOSTING-WP-STD", skus)
        self.assertIn("HOSTING-WP-BUS", skus)

        # 3. Market stats must be calculated from valid products (210k and 520k)
        self.assertEqual(report.market_min, 210000)
        self.assertEqual(report.market_median, 365000)  # (210k + 520k) / 2

        # 4. Suggestions must be provided
        self.assertGreaterEqual(len(report.suggestions), 2)


if __name__ == "__main__":
    unittest.main()
