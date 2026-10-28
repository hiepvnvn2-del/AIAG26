import asyncio
import os
import unittest
from pathlib import Path

from app.config import settings
from app.schemas import CompetitorProduct
from app.services.browser_worker import BrowserWorker
from app.services.normalizer import match_competitor_to_catalog

BASE_DIR = Path(__file__).resolve().parent.parent
GOLDEN_PAGE = (BASE_DIR / "eval" / "golden" / "pages" / "17_tabs_and_toggles.html").as_uri()

OUR_CATALOG = [
    {
        "product_code": "HOSTING-WP-STD",
        "product_name": "Hosting WordPress Standard",
        "category": "Websites",
        "our_current_price_vnd": 250000,
        "floor_price_vnd": 150000,
        "specs": {"ram": "2GB", "ssd": "10GB", "websites": 1},
    },
    {
        "product_code": "HOSTING-WP-BUS",
        "product_name": "Hosting WordPress Business",
        "category": "Websites",
        "our_current_price_vnd": 590000,
        "floor_price_vnd": 350000,
        "specs": {"ram": "4GB", "ssd": "60GB", "websites": 20},
    },
]


class TestTabsAndTogglesScraping(unittest.TestCase):
    def test_scrape_tabs_and_toggles_end_to_end(self):
        """Verify:
        1. Both category tabs ('Websites' and 'VPS hosting') are crawled.
        2. Billing cycles ('month' and 'year') are extracted.
        3. No duplicate records.
        4. Category names and billing cycles are accurately assigned.
        5. Banned buttons ('Buy', 'Add to cart', etc.) are never treated as tabs.
        """
        async def run_scrape():
            worker = BrowserWorker(worker_id="test_tabs_worker")
            try:
                products = await worker.scrape_generic(GOLDEN_PAGE)
                return products
            finally:
                await worker.stop()

        products = asyncio.run(run_scrape())

        self.assertGreaterEqual(len(products), 4, f"Expected at least 4 products, got {len(products)}")

        # Check categories
        categories = {p.category for p in products}
        self.assertIn("Websites", categories, "Category 'Websites' must be extracted")
        self.assertIn("VPS hosting", categories, "Category 'VPS hosting' must be extracted")

        # Check billing cycles
        cycles = {p.billing_cycle for p in products}
        self.assertIn("month", cycles, "Billing cycle 'month' must be extracted")
        self.assertIn("year", cycles, "Billing cycle 'year' must be extracted")

        # Check deduplication
        keys = [(p.name.lower(), p.price_vnd, p.billing_cycle, (p.category or "").lower()) for p in products]
        self.assertEqual(len(keys), len(set(keys)), "No duplicate records should exist")

        # Check specific products
        web_starter_month = next((p for p in products if "starter" in p.name.lower() and p.billing_cycle == "month"), None)
        self.assertIsNotNone(web_starter_month, "Must find WordPress Starter Monthly")
        self.assertEqual(web_starter_month.price_vnd, 99000)
        self.assertEqual(web_starter_month.category, "Websites")

        vps_basic_month = next((p for p in products if "vps basic" in p.name.lower() and p.billing_cycle == "month"), None)
        self.assertIsNotNone(vps_basic_month, "Must find Cloud VPS Basic Monthly")
        self.assertEqual(vps_basic_month.price_vnd, 150000)
        self.assertEqual(vps_basic_month.category, "VPS hosting")

        # Verify no banned buttons were extracted as products
        for p in products:
            for banned in ("login", "sign in", "giỏ hàng", "mua ngay", "thanh toán", "đặt hàng"):
                self.assertNotIn(banned, p.name.lower())

    def test_catalog_matching_separation(self):
        """Verify:
        - Website hosting competitor products match our website hosting catalog.
        - VPS hosting products NEVER match our website hosting catalog.
        """
        web_prod = CompetitorProduct(
            name="Hosting WordPress Starter",
            price_vnd=99000,
            billing_cycle="month",
            category="Websites",
            specs={"websites": 1, "ssd": "10GB"},
        )
        vps_prod = CompetitorProduct(
            name="Cloud VPS Basic",
            price_vnd=150000,
            billing_cycle="month",
            category="VPS hosting",
            specs={"ram": "2GB", "ssd": "30GB"},
        )

        best_web, conf_web, note_web = match_competitor_to_catalog(web_prod, OUR_CATALOG)
        self.assertIsNotNone(best_web, "Web hosting competitor product should match our catalog")
        self.assertEqual(best_web["product_code"], "HOSTING-WP-STD")

        best_vps, conf_vps, note_vps = match_competitor_to_catalog(vps_prod, OUR_CATALOG)
        self.assertIsNone(best_vps, "VPS hosting competitor product should NOT match our web hosting catalog")
        self.assertIn("Khác loại sản phẩm", note_vps)


if __name__ == "__main__":
    unittest.main()
