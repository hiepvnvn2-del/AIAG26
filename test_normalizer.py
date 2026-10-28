"""Unit test for price/stock normalization and the deterministic fallback path."""
import asyncio
from app.services.normalizer import normalize_price, normalize_stock, _deterministic_normalize

CASES = [
    ("199k", 199000),
    ("199.000đ", 199000),
    ("250.000 VND", 250000),
    ("$8.00", 203200),
    ("£15.00", 480000),
    ("690.000đ", 690000),
]

def test_prices():
    for raw, want in CASES:
        got, note = normalize_price(raw)
        assert got == want, f"{raw!r}: got {got} want {want} ({note})"
        print(f"  OK  {raw!r:14} -> {got}")

def test_stock():
    for raw, want in [
        ("Còn hàng", "IN_STOCK"), ("in stock", "IN_STOCK"),
        ("Hết hàng", "OUT_OF_STOCK"), ("sold out", "OUT_OF_STOCK"),
        ("Sắp hết", "LIMITED"), ("limited", "LIMITED"),
    ]:
        got = normalize_stock(raw)
        assert got == want, f"{raw!r}: got {got} want {want}"
        print(f"  OK  {raw!r:14} -> {got}")

def test_deterministic_fallback():
    catalog = [{"product_code": "HOSTING-WP-STD", "product_name": "Gói Hosting WordPress Standard", "our_current_price_vnd": 250000}]
    extractions = [
        {"source": "Competitor A", "product_code": "HOSTING-WP-STD", "raw_price": "199k", "raw_status": "Còn hàng"},
        {"source": "Competitor B", "product_code": "HOSTING-WP-STD", "raw_price": "$8.00", "raw_status": "in stock"},
    ]
    out = _deterministic_normalize(catalog, extractions, threshold_pct=10.0, fx={"USD": 25400, "GBP": 32000, "VND": 1})
    row = out.comparison_table[0]
    assert row.action_required is True, "should flag action_required"
    assert len(row.competitors) == 2
    cheapest = min(c.normalized_price_vnd for c in row.competitors)
    assert row.recommended_new_price_vnd == int(cheapest * 0.98), "formula: floor(min*0.98)"
    print(f"  OK  cheapest={cheapest} -> recommended={row.recommended_new_price_vnd}")
    print(f"  OK  reason_vn: {row.reason_vn}")

if __name__ == "__main__":
    print("=== normalize_price ==="); test_prices()
    print("=== normalize_stock ==="); test_stock()
    print("=== deterministic fallback ==="); test_deterministic_fallback()
    print("\nALL NORMALIZATION TESTS PASSED")