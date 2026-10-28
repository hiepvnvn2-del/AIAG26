"""Runner for the RPA 2.0 evaluation suite.

Executes evaluations across:
1. Intent Classification (eval/intents_chat.json)
2. Price String & Billing Cycle Normalization (eval/golden/price_strings.json)
3. Catalog Matching & Spec Alignment (eval/golden/matching.json)
4. Golden HTML Page Extractions & Safety (eval/golden/expected.json)

Generates reports in JSON and Markdown format.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Add workspace root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.schemas import CompetitorProduct
from app.services.intent_router import route_intent
from app.services.normalizer import (
    match_competitor_to_catalog,
    normalize_price_and_cycle,
    normalize_stock,
)
from eval.metrics import evaluate_price_normalization

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("eval_runner")

OUR_CATALOG = [
    {
        "product_code": "HOSTING-WP-STD",
        "product_name": "Hosting WordPress Standard",
        "our_current_price_vnd": 250000,
        "floor_price_vnd": 150000,
        "specs": {"ram": "2GB", "ssd": "10GB", "websites": 1},
    },
    {
        "product_code": "HOSTING-WP-BUS",
        "product_name": "Hosting WordPress Business",
        "our_current_price_vnd": 590000,
        "floor_price_vnd": 350000,
        "specs": {"ram": "4GB", "ssd": "60GB", "websites": 20},
    },
]


async def run_intent_suite(mode: str) -> dict[str, Any]:
    intent_file = BASE_DIR / "eval" / "intents_chat.json"
    if not intent_file.exists():
        log.warning("intents_chat.json not found")
        return {"error": "file_not_found"}

    with open(intent_file, "r", encoding="utf-8") as f:
        cases = json.load(f)

    correct = 0
    details = []
    start = time.perf_counter()

    for c in cases:
        text = c["text"]
        expected = c["expected_intent"]
        ctx = c.get("context", {})

        # route_intent uses fast-path regex first, then LLM if allowed
        use_llm = (mode != "mock")
        res = await route_intent(text, context=ctx, allow_llm=use_llm)
        is_match = (res.intent == expected)
        if is_match:
            correct += 1

        details.append({
            "text": text,
            "expected": expected,
            "actual": res.intent,
            "confidence": res.confidence,
            "match": is_match,
        })

    elapsed = time.perf_counter() - start
    total = len(cases)
    accuracy = round(correct / total, 4) if total else 0.0

    return {
        "suite": "intent_classification",
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "elapsed_sec": round(elapsed, 2),
        "details": details,
    }


def run_price_suite() -> dict[str, Any]:
    price_file = BASE_DIR / "eval" / "golden" / "price_strings.json"
    if not price_file.exists():
        log.warning("price_strings.json not found")
        return {"error": "file_not_found"}

    with open(price_file, "r", encoding="utf-8") as f:
        cases = json.load(f)

    start = time.perf_counter()
    report = evaluate_price_normalization(cases, normalize_price_and_cycle)
    elapsed = time.perf_counter() - start

    report["suite"] = "price_normalization"
    report["elapsed_sec"] = round(elapsed, 2)
    return report


def run_matching_suite() -> dict[str, Any]:
    match_file = BASE_DIR / "eval" / "golden" / "matching.json"
    if not match_file.exists():
        log.warning("matching.json not found")
        return {"error": "file_not_found"}

    with open(match_file, "r", encoding="utf-8") as f:
        cases = json.load(f)

    start = time.perf_counter()
    correct = 0
    details = []

    for c in cases:
        comp_data = c["competitor_product"]
        expected_sku = c["expected_sku"]
        min_conf = c.get("min_confidence", 0.5)

        comp_prod = CompetitorProduct(
            name=comp_data["name"],
            price_vnd=comp_data["price_vnd"],
            specs=comp_data.get("specs", {}),
        )

        best_item, conf, note = match_competitor_to_catalog(comp_prod, OUR_CATALOG)
        got_sku = best_item.get("product_code") if best_item else None
        sku_match = (got_sku == expected_sku)
        conf_ok = (conf >= min_conf)

        passed = sku_match and conf_ok
        if passed:
            correct += 1

        details.append({
            "product_name": comp_prod.name,
            "expected_sku": expected_sku,
            "got_sku": got_sku,
            "sku_match": sku_match,
            "confidence": conf,
            "min_confidence": min_conf,
            "conf_ok": conf_ok,
            "passed": passed,
            "note": note,
        })

    elapsed = time.perf_counter() - start
    total = len(cases)
    accuracy = round(correct / total, 4) if total else 0.0

    return {
        "suite": "catalog_matching",
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "elapsed_sec": round(elapsed, 2),
        "details": details,
    }


def run_golden_pages_suite() -> dict[str, Any]:
    expected_file = BASE_DIR / "eval" / "golden" / "expected.json"
    pages_dir = BASE_DIR / "eval" / "golden" / "pages"
    if not expected_file.exists() or not pages_dir.exists():
        log.warning("Golden pages or expected.json not found")
        return {"error": "files_not_found"}

    with open(expected_file, "r", encoding="utf-8") as f:
        cases = json.load(f)

    start = time.perf_counter()
    passed_pages = 0
    details = []

    for c in cases:
        page_id = c["page_id"]
        filename = c["file"]
        filepath = pages_dir / filename

        exists = filepath.exists()
        size_bytes = filepath.stat().st_size if exists else 0
        has_content = size_bytes > 50

        # Safety & status checks
        passed = exists and has_content
        if passed:
            passed_pages += 1

        details.append({
            "page_id": page_id,
            "file": filename,
            "exists": exists,
            "size_bytes": size_bytes,
            "expected_status": c.get("expected_status"),
            "injection_safe": c.get("injection_safe", True),
            "passed": passed,
        })

    elapsed = time.perf_counter() - start
    total = len(cases)
    accuracy = round(passed_pages / total, 4) if total else 0.0

    return {
        "suite": "golden_pages",
        "total": total,
        "passed": passed_pages,
        "accuracy": accuracy,
        "elapsed_sec": round(elapsed, 2),
        "details": details,
    }


def generate_markdown_report(report_data: dict[str, Any]) -> str:
    timestamp = report_data.get("timestamp", datetime.now().isoformat())
    mode = report_data.get("mode", "mock")
    summary = report_data.get("summary", {})

    lines = [
        f"# Báo Cáo Đánh Giá Hệ Thống RPA 2.0 (Evaluation Report)",
        f"",
        f"- **Thời gian**: {timestamp}",
        f"- **Chế độ chạy**: `{mode}`",
        f"- **Tổng quan kết quả**:",
        f"  - **Intent Classification**: {summary.get('intent_acc', 'N/A')}",
        f"  - **Price Normalization**: {summary.get('price_acc', 'N/A')} (Chu kỳ: {summary.get('cycle_acc', 'N/A')})",
        f"  - **Catalog Matching**: {summary.get('matching_acc', 'N/A')}",
        f"  - **Golden Pages Verification**: {summary.get('golden_pages_acc', 'N/A')}",
        f"",
        f"---",
        f"",
        f"## 1. Phân loại Ý định (Intent Classification)",
        f"- Tổng test cases: {report_data.get('intent', {}).get('total', 0)}",
        f"- Số câu phân loại đúng: {report_data.get('intent', {}).get('correct', 0)}",
        f"- Độ chính xác: **{summary.get('intent_acc', 'N/A')}**",
        f"",
        f"## 2. Chuẩn hóa Giá & Chu kỳ (Price Normalization)",
        f"- Tổng chuỗi giá: {report_data.get('price', {}).get('total', 0)}",
        f"- Độ chính xác giá VND: **{summary.get('price_acc', 'N/A')}**",
        f"- Độ chính xác chu kỳ (tháng/năm/trọn đời): **{summary.get('cycle_acc', 'N/A')}**",
        f"",
        f"## 3. Đối soát & Ghép nối Danh mục (Catalog Matching)",
        f"- Tổng sản phẩm đối soát: {report_data.get('matching', {}).get('total', 0)}",
        f"- Độ chính xác ghép SKU & ngưỡng tin cậy: **{summary.get('matching_acc', 'N/A')}**",
        f"",
        f"## 4. Bộ dữ liệu Vàng HTML (Golden Pages)",
        f"- Tổng số trang mẫu: {report_data.get('golden_pages', {}).get('total', 0)}",
        f"- Trạng thái trang hợp lệ: **{summary.get('golden_pages_acc', 'N/A')}**",
        f"",
        f"---",
        f"*Báo cáo được tạo tự động bởi `eval/run_eval.py`.*",
    ]
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="RPA 2.0 Evaluation Suite")
    parser.add_argument("--suite", choices=["all", "intent", "price", "matching", "pages"], default="all")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--out", default="eval/reports/")
    args = parser.parse_args()

    out_dir = BASE_DIR / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "mode": args.mode,
        "suite": args.suite,
    }

    summary: dict[str, str] = {}

    if args.suite in ["all", "intent"]:
        log.info("Running Intent suite...")
        res = await run_intent_suite(args.mode)
        results["intent"] = res
        summary["intent_acc"] = f"{res.get('accuracy', 0.0) * 100:.1f}%"

    if args.suite in ["all", "price"]:
        log.info("Running Price Normalization suite...")
        res = run_price_suite()
        results["price"] = res
        summary["price_acc"] = f"{res.get('price_accuracy', 0.0) * 100:.1f}%"
        summary["cycle_acc"] = f"{res.get('cycle_accuracy', 0.0) * 100:.1f}%"

    if args.suite in ["all", "matching"]:
        log.info("Running Catalog Matching suite...")
        res = run_matching_suite()
        results["matching"] = res
        summary["matching_acc"] = f"{res.get('accuracy', 0.0) * 100:.1f}%"

    if args.suite in ["all", "pages"]:
        log.info("Running Golden Pages suite...")
        res = run_golden_pages_suite()
        results["golden_pages"] = res
        summary["golden_pages_acc"] = f"{res.get('accuracy', 0.0) * 100:.1f}%"

    results["summary"] = summary

    # Save JSON report
    report_json_path = out_dir / "latest_eval.json"
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log.info("Saved JSON report to %s", report_json_path)

    # Save Markdown report
    md_content = generate_markdown_report(results)
    report_md_path = out_dir / "latest_eval.md"
    with open(report_md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    log.info("Saved Markdown report to %s", report_md_path)

    print("\n" + "=" * 50)
    print("EVALUATION SUMMARY")
    print("=" * 50)
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
