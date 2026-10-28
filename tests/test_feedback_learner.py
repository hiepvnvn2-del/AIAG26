"""Unit tests for Feedback Learner, Domain Profiles, and HITL logging."""
import json
import unittest
from pathlib import Path

from app.schemas import ApprovalRequest
from app.services.domain_profiles import get_profile_for_url
from app.services.feedback_learner import (
    DECISIONS_FILE,
    analyze_admin_feedback,
    get_learned_constraints,
    log_hitl_decision,
)


class TestFeedbackLearner(unittest.TestCase):
    def test_log_and_analyze_decisions(self):
        req = ApprovalRequest(
            workflow_id="wf_test_001",
            risk_level="HIGH",
            staged_action="Update Price",
            before_values={"price_vnd": 250000},
            after_values={"price_vnd": 200000, "product_code": "HOSTING-WP-STD"},
            impact_vn="Giảm giá 20%",
        )

        # Log an approval
        log_hitl_decision(req, "APPROVE")

        # Log a rejection
        req2 = ApprovalRequest(
            workflow_id="wf_test_002",
            risk_level="CRITICAL",
            staged_action="Update Price",
            before_values={"price_vnd": 590000},
            after_values={"price_vnd": 360000, "product_code": "HOSTING-WP-BUS"},
            impact_vn="Giảm giá 39%",
        )
        log_hitl_decision(req2, "REJECT")

        analysis = analyze_admin_feedback()
        self.assertGreaterEqual(analysis["total_decisions"], 2)
        self.assertIn("%", analysis["approval_rate"])
        self.assertTrue(len(analysis["learned_insights"]) > 0)

        constraints = get_learned_constraints()
        self.assertIn("max_recommended_drop_pct", constraints)

    def test_domain_profiles_lookup(self):
        prof = get_profile_for_url("https://www.matbao.net/hosting/wordpress.html")
        self.assertEqual(prof["domain"], "matbao.net")
        self.assertEqual(prof["engine_preference"], "firecrawl")

        prof2 = get_profile_for_url("https://unknown-shop-123.vn")
        self.assertEqual(prof2["engine_preference"], "auto")


if __name__ == "__main__":
    unittest.main()
