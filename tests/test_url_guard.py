import unittest
from app.services.url_guard import validate_public_url


class TestURLGuard(unittest.TestCase):
    def test_blocks_localhost(self):
        valid, reason, _ = validate_public_url("http://localhost:8000")
        self.assertFalse(valid)
        self.assertIn("cục bộ", reason)

    def test_blocks_loopback_ip(self):
        valid, reason, _ = validate_public_url("http://127.0.0.1:8000/demo/competitor_a.html")
        self.assertFalse(valid)
        self.assertIn("loopback", reason.lower())

    def test_blocks_private_network(self):
        valid, reason, _ = validate_public_url("http://192.168.1.1/admin")
        self.assertFalse(valid)
        self.assertIn("nội bộ", reason.lower())

    def test_blocks_non_http_scheme(self):
        valid, reason, _ = validate_public_url("file:///etc/passwd")
        self.assertFalse(valid)
        self.assertIn("http hoặc https", reason)

    def test_allows_public_domain(self):
        # google.com or cloudflare.com is public
        valid, reason, ip = validate_public_url("https://google.com")
        self.assertTrue(valid, msg=f"Failed with reason: {reason}")
        self.assertIsNotNone(ip)


if __name__ == "__main__":
    unittest.main()
