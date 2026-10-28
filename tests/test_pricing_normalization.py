import unittest
from app.services.normalizer import normalize_price, normalize_price_and_cycle


class TestPricingNormalization(unittest.TestCase):
    def test_monthly_k_format(self):
        price, cycle, note = normalize_price_and_cycle("199k/tháng")
        self.assertEqual(price, 199000)
        self.assertEqual(cycle, "month")

    def test_vnd_dot_format(self):
        price, cycle, note = normalize_price_and_cycle("250.000đ")
        self.assertEqual(price, 250000)
        self.assertEqual(cycle, "month")

    def test_usd_format(self):
        price, cycle, note = normalize_price_and_cycle("$9.90")
        # 9.90 * 25400 = 251460
        self.assertEqual(price, 251460)

    def test_gbp_format(self):
        price, cycle, note = normalize_price_and_cycle("£15.00")
        # 15.00 * 32000 = 480000
        self.assertEqual(price, 480000)

    def test_yearly_vnd_converted_to_monthly(self):
        price, cycle, note = normalize_price_and_cycle("2.400.000đ/năm")
        # 2,400,000 / 12 = 200,000
        self.assertEqual(price, 200000)
        self.assertEqual(cycle, "year")
        self.assertIn("/12", note)


if __name__ == "__main__":
    unittest.main()
