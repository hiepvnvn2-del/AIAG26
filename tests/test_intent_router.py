"""Unit tests for IntentRouter (Stage 1)."""
import unittest
from unittest.mock import AsyncMock, patch

from app.schemas import IntentResult
from app.services.intent_router import _fast_path_route, route_intent


class TestIntentRouter(unittest.IsolatedAsyncioTestCase):
    def test_fast_path_exact_commands(self):
        r1 = _fast_path_route("/catalog")
        self.assertIsNotNone(r1)
        self.assertEqual(r1.intent, "SHOW_CATALOG")

        r2 = _fast_path_route("bảng giá")
        self.assertIsNotNone(r2)
        self.assertEqual(r2.intent, "SHOW_CATALOG")

        r3 = _fast_path_route("/run")
        self.assertIsNotNone(r3)
        self.assertEqual(r3.intent, "RUN_COMPETITOR_SCAN")

    def test_fast_path_pending_hitl_approval(self):
        ctx = {"has_pending_hitl": True}
        r = _fast_path_route("duyệt", context=ctx)
        self.assertIsNotNone(r)
        self.assertEqual(r.intent, "APPROVE")

        r_rej = _fast_path_route("hủy bỏ", context=ctx)
        self.assertIsNotNone(r_rej)
        self.assertEqual(r_rej.intent, "REJECT")

    def test_fast_path_price_override(self):
        r = _fast_path_route("Sửa gói standard thành 180k rồi lưu")
        self.assertIsNotNone(r)
        self.assertEqual(r.intent, "APPLY_PRICE_CHANGE")
        self.assertEqual(r.entities.get("price_vnd"), 180000)
        self.assertEqual(r.entities.get("sku"), "HOSTING-WP-STD")

    def test_fast_path_options_selection(self):
        ctx = {"last_suggested_options": {"A": 220000, "B": 240000}}
        r = _fast_path_route("áp dụng phương án B", context=ctx)
        self.assertIsNotNone(r)
        self.assertEqual(r.intent, "APPLY_PRICE_CHANGE")
        self.assertEqual(r.entities.get("proposed_option"), "B")

        r2 = _fast_path_route("ok làm đi", context=ctx)
        self.assertIsNotNone(r2)
        self.assertEqual(r2.intent, "APPLY_PRICE_CHANGE")
        self.assertEqual(r2.entities.get("proposed_option"), "A")

    async def test_llm_route_problematic_logs(self):
        # Test case 1 from real user log
        msg1 = "vậy bây giờ tôi cần chỉnh giá lại cho sản phẩm của tôi như nào"
        mock_result1 = IntentResult(intent="PRICE_ADVISORY", confidence=0.98, entities={})

        with patch("app.services.intent_router.llm.generate_role_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_result1
            res1 = await route_intent(msg1)
            self.assertEqual(res1.intent, "PRICE_ADVISORY")

        # Test case 2 from real user log
        msg2 = "tôi muốn chỉnh sửa giá để phù hợp hãy đề xuất cho tôi"
        mock_result2 = IntentResult(intent="PRICE_ADVISORY", confidence=0.99, entities={})

        with patch("app.services.intent_router.llm.generate_role_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_result2
            res2 = await route_intent(msg2)
            self.assertEqual(res2.intent, "PRICE_ADVISORY")

    async def test_llm_fallback_on_unclear(self):
        with patch("app.services.intent_router.llm.generate_role_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = IntentResult(intent="UNCLEAR", confidence=0.3, needs_clarification=True)
            res = await route_intent("trời hôm nay nhiều mây")
            self.assertEqual(res.intent, "UNCLEAR")
            self.assertTrue(res.needs_clarification)
            self.assertIsNotNone(res.clarifying_question)


if __name__ == "__main__":
    unittest.main()
