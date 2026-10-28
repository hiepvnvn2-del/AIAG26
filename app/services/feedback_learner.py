"""Feedback Learner & HITL Decision Intelligence for RPA 2.0.

Logs all human decisions (Approved, Rejected, Overridden) to data/decisions.jsonl.
Extracts empirical behavioral patterns from human interactions:
- SKU-specific approval thresholds
- Max acceptable price drop percentages
- Preferred pricing endings (charm pricing: .000, 99k)
- Risk level tolerances
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR
from app.schemas import ApprovalRequest

log = logging.getLogger("rpa.feedback_learner")

DATA_DIR = ROOT_DIR / "data"
DECISIONS_FILE = DATA_DIR / "decisions.jsonl"


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def log_hitl_decision(
    req: ApprovalRequest,
    decision_raw: Any,
    override_price: int | None = None,
) -> dict[str, Any]:
    """Appends a structured decision record to decisions.jsonl."""
    _ensure_data_dir()

    action = "REJECTED"
    final_price = None

    if isinstance(decision_raw, str):
        if decision_raw == "APPROVE":
            action = "APPROVED"
            final_price = req.after_values.get("price_vnd")
        else:
            action = "REJECTED"
    elif isinstance(decision_raw, dict):
        if decision_raw.get("decision") == "OVERRIDE":
            action = "OVERRIDDEN"
            final_price = decision_raw.get("override_price") or override_price
        elif decision_raw.get("decision") == "APPROVE":
            action = "APPROVED"
            final_price = req.after_values.get("price_vnd")
        else:
            action = "REJECTED"

    old_price = req.before_values.get("price_vnd")
    proposed_price = req.after_values.get("price_vnd")

    record = {
        "timestamp": datetime.now().isoformat(),
        "workflow_id": req.workflow_id,
        "product_code": req.after_values.get("product_code", "UNKNOWN"),
        "action": action,
        "old_price": old_price,
        "proposed_price": proposed_price,
        "final_price": final_price,
        "risk_level": req.risk_level,
        "impact_vn": req.impact_vn,
    }

    try:
        with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info("Logged HITL decision: %s for %s (%s)", action, req.workflow_id, record["product_code"])
    except Exception as exc:
        log.error("Failed to append HITL decision log: %s", exc)

    return record


def get_decision_history() -> list[dict[str, Any]]:
    """Loads all historical decisions from JSONL."""
    if not DECISIONS_FILE.exists():
        return []

    records = []
    try:
        with open(DECISIONS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except Exception as exc:
        log.warning("Could not read decisions.jsonl: %s", exc)
    return records


def analyze_admin_feedback() -> dict[str, Any]:
    """Computes statistical insights and behavioral patterns from admin decisions."""
    history = get_decision_history()
    if not history:
        return {
            "total_decisions": 0,
            "approval_rate": "0.0%",
            "rejection_rate": "0.0%",
            "override_rate": "0.0%",
            "by_risk_level": {},
            "by_sku": {},
            "learned_insights": [
                "Chưa có đủ dữ liệu lịch sử để học thói quen admin. Cần tối thiểu 3 phiên HITL."
            ],
        }

    total = len(history)
    approved = sum(1 for d in history if d["action"] == "APPROVED")
    rejected = sum(1 for d in history if d["action"] == "REJECTED")
    overridden = sum(1 for d in history if d["action"] == "OVERRIDDEN")

    # By Risk Level
    by_risk: dict[str, dict[str, int]] = {}
    for d in history:
        r = d.get("risk_level", "UNKNOWN")
        if r not in by_risk:
            by_risk[r] = {"total": 0, "approved": 0, "rejected": 0, "overridden": 0}
        by_risk[r]["total"] += 1
        if d["action"] == "APPROVED":
            by_risk[r]["approved"] += 1
        elif d["action"] == "REJECTED":
            by_risk[r]["rejected"] += 1
        elif d["action"] == "OVERRIDDEN":
            by_risk[r]["overridden"] += 1

    # By SKU
    by_sku: dict[str, dict[str, Any]] = {}
    for d in history:
        sku = d.get("product_code", "OTHER")
        if sku not in by_sku:
            by_sku[sku] = {"total": 0, "approved": 0, "drops_rejected": []}
        by_sku[sku]["total"] += 1
        if d["action"] in ("APPROVED", "OVERRIDDEN"):
            by_sku[sku]["approved"] += 1
        elif d["action"] == "REJECTED":
            old_p = d.get("old_price")
            prop_p = d.get("proposed_price")
            if old_p and prop_p and old_p > prop_p:
                drop_pct = round((old_p - prop_p) / old_p * 100, 1)
                by_sku[sku]["drops_rejected"].append(drop_pct)

    # Distill Learned Insights
    insights = []
    # 1. High/Critical risk tolerance
    crit = by_risk.get("CRITICAL", {})
    if crit.get("total", 0) > 0:
        crit_rate = round(crit.get("approved", 0) / crit["total"] * 100, 1)
        insights.append(f"Tỷ lệ duyệt biến động lớn (CRITICAL >= 30%): {crit_rate}% ({crit.get('approved',0)}/{crit['total']}).")

    # 2. Preferred Drop Ceiling
    for sku, data in by_sku.items():
        if data["drops_rejected"]:
            min_rej_drop = min(data["drops_rejected"])
            insights.append(
                f"Sản phẩm {sku}: Admin từng từ chối mức giảm giá từ {min_rej_drop:.1f}%. Khuyến nghị giữ mức giảm dưới {min_rej_drop - 2:.1f}%."
            )

    if overridden > 0:
        insights.append(f"Admin có xu hướng tự điều chỉnh (Override) trong {overridden}/{total} lần thay vì duyệt tự động.")

    return {
        "total_decisions": total,
        "approved": approved,
        "rejected": rejected,
        "overridden": overridden,
        "approval_rate": f"{(approved + overridden) / total * 100:.1f}%",
        "by_risk_level": by_risk,
        "by_sku": by_sku,
        "learned_insights": insights or ["Admin có xu hướng tuân thủ đề xuất hệ thống với tỷ lệ chấp thuận cao."],
    }


def get_learned_constraints() -> dict[str, Any]:
    """Returns dynamic recommendation guardrails inferred from admin actions."""
    analysis = analyze_admin_feedback()
    # Default constraints
    constraints = {
        "max_recommended_drop_pct": 20.0,
        "require_conservative_mode": False,
        "charm_pricing_suffix": "000",
    }

    if analysis.get("total_decisions", 0) >= 3:
        # If approval rate is low (< 50%), switch to conservative mode
        approved = analysis.get("approved", 0)
        total = analysis["total_decisions"]
        if approved / total < 0.5:
            constraints["require_conservative_mode"] = True
            constraints["max_recommended_drop_pct"] = 12.0

    return constraints
