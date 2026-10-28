"""Evaluation metrics calculation for RPA 2.0 evaluation suite."""
from __future__ import annotations

from typing import Any


def calculate_accuracy(actual: list[Any], expected: list[Any]) -> float:
    """Calculate basic top-1 accuracy."""
    if not expected:
        return 0.0
    correct = sum(1 for a, e in zip(actual, expected) if a == e)
    return correct / len(expected)


def calculate_precision_recall_f1(actual_matches: list[bool]) -> dict[str, float]:
    """Calculate precision, recall, and F1 given boolean match results."""
    total = len(actual_matches)
    if total == 0:
        return {"accuracy": 0.0, "total": 0}
    correct = sum(1 for m in actual_matches if m)
    acc = correct / total
    return {
        "accuracy": round(acc, 4),
        "correct": correct,
        "total": total,
        "percentage": round(acc * 100, 2),
    }


def evaluate_price_normalization(test_cases: list[dict], normalize_func) -> dict[str, Any]:
    """Run price strings evaluation."""
    results = []
    correct_count = 0
    cycle_correct_count = 0

    for tc in test_cases:
        raw = tc["raw"]
        exp_vnd = tc["expected_vnd"]
        exp_cycle = tc.get("cycle", "month")

        price_res, cycle_res, _ = normalize_func(raw)

        # Allow slight delta for float fx conversion (within 1%)
        price_match = False
        if price_res is not None and exp_vnd is not None:
            if abs(price_res - exp_vnd) <= max(10, exp_vnd * 0.01):
                price_match = True

        cycle_match = (cycle_res == exp_cycle)

        if price_match:
            correct_count += 1
        if cycle_match:
            cycle_correct_count += 1

        results.append({
            "raw": raw,
            "expected_vnd": exp_vnd,
            "got_vnd": price_res,
            "price_match": price_match,
            "expected_cycle": exp_cycle,
            "got_cycle": cycle_res,
            "cycle_match": cycle_match,
        })

    total = len(test_cases)
    return {
        "total": total,
        "price_accuracy": round(correct_count / total, 4) if total else 0.0,
        "cycle_accuracy": round(cycle_correct_count / total, 4) if total else 0.0,
        "details": results,
    }
