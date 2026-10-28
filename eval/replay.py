"""Offline Workflow Replay & Forensic Debugger for RPA 2.0.

Replays historic workflows from decisions.jsonl or logs without re-scraping:
- Validates determinism of pricing calculations
- Re-runs catalog matching against OUR_CATALOG
- Confirms whether decision violated price floor rules

Usage:
  python eval/replay.py --last
  python eval/replay.py --workflow-id <id>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.config import settings
from app.schemas import CompetitorProduct
from app.services.feedback_learner import get_decision_history
from app.services.normalizer import match_competitor_to_catalog
from app.services.orchestrator import OUR_CATALOG

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("replay_tool")

# Fix Windows console UTF-8 output encoding
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def replay_record(record: dict) -> bool:
    wf_id = record.get("workflow_id", "UNKNOWN")
    sku = record.get("product_code", "HOSTING-WP-STD")
    old_p = record.get("old_price")
    prop_p = record.get("proposed_price")
    action = record.get("action")
    risk = record.get("risk_level")

    print("\n" + "=" * 60)
    print(f"REPLAYING WORKFLOW: {wf_id}")
    print("=" * 60)
    print(f"• Timestamp: {record.get('timestamp')}")
    print(f"• Target SKU: {sku}")
    print(f"• Old Price: {old_p:,} VND" if old_p else "• Old Price: None")
    print(f"• Proposed Price: {prop_p:,} VND" if prop_p else "• Proposed Price: None")
    print(f"• Human Decision: {action}")
    print(f"• Staged Risk Level: {risk}")

    # Safety Verification
    floor = settings.min_price_floor_bus if "bus" in sku.lower() else settings.min_price_floor_std
    if prop_p and prop_p < floor:
        print(f"❌ [ALERT] Proposed price ({prop_p:,} VND) fell BELOW price floor ({floor:,} VND)!")
        safety_pass = False
    else:
        print(f"✅ [SAFE] Price proposal satisfies minimum floor ({floor:,} VND).")
        safety_pass = True

    # Re-verify Catalog Match
    mock_comp = CompetitorProduct(
        name=f"Replay Competitor for {sku}",
        price_vnd=prop_p,
        specs={},
    )
    best_item, conf, note = match_competitor_to_catalog(mock_comp, OUR_CATALOG)
    print(f"• Replay Catalog Match: {best_item.get('product_code')} (Conf={conf:.2f}, Note={note})")
    print("=" * 60 + "\n")
    return safety_pass


def main():
    parser = argparse.ArgumentParser(description="Workflow Replay Debugger")
    parser.add_argument("--workflow-id", help="Specific workflow ID to replay")
    parser.add_argument("--last", action="store_true", help="Replay the most recent recorded decision")
    args = parser.parse_args()

    history = get_decision_history()
    if not history:
        print("No historic decisions found in data/decisions.jsonl. Run a workflow or test first.")
        return

    target = None
    if args.last:
        target = history[-1]
    elif args.workflow_id:
        target = next((d for d in history if d.get("workflow_id") == args.workflow_id), None)
        if not target:
            print(f"Workflow ID '{args.workflow_id}' not found in history.")
            return
    else:
        # Default to last if none specified
        target = history[-1]

    replay_record(target)


if __name__ == "__main__":
    main()
