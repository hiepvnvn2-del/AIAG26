"""Business Intelligence & Data Normalization Agent.

Receives raw extractions from multiple Browser Workers, our catalog, and the
business rule. Normalizes prices to integer VND, standardizes stock status,
compares, and flags items where any IN_STOCK competitor is cheaper by the
threshold. Outputs NormalizerOutput JSON using active_role = DATA_NORMALIZER_ANALYST.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings
from app.schemas import (
    ComparisonRow,
    CompetitorEntry,
    CompetitorProduct,
    MatchedItem,
    NormalizerOutput,
    PriceAnalysisReport,
)
from app.services.llm_rotator import llm
from app.services.prompts import PRICE_ANALYST_PROMPT_TEMPLATE, PRICE_NORMALIZATION_RULES

log = logging.getLogger("rpa.normalizer")

# ---------------------------------------------------------------------------
# Deterministic price normalization (code-enforced, not LLM-hallucinated)
# ---------------------------------------------------------------------------
_VND_SUFFIX = re.compile(r"(\d[\d.,]*)\s*(k|đ|vnd|vnđ|dong)?", re.IGNORECASE)
_USD = re.compile(r"\$\s*(\d[\d.,]*)")
_GBP = re.compile(r"£\s*(\d[\d.,]*)")

DEFAULT_FX = {"USD": 25400, "GBP": 32000, "VND": 1}


def _to_int(amount: str, suffix: str | None = None) -> int:
    """'199.000' -> 199000, '199k' -> 199000, '199,000' -> 199000."""
    s = amount.strip().replace(".", "").replace(",", "")
    n = int(float(s))
    if suffix and suffix.lower() == "k":
        return n * 1000
    return n


def normalize_price(raw: str, fx: dict[str, int] | None = None) -> tuple[int | None, str]:
    """Returns (price_vnd, note). note is '' on success."""
    fx = fx or DEFAULT_FX
    raw = (raw or "").strip()

    m = _USD.search(raw)
    if m:
        return int(float(m.group(1).replace(",", "")) * fx["USD"]), ""

    m = _GBP.search(raw)
    if m:
        return int(float(m.group(1).replace(",", "")) * fx["GBP"]), ""

    m = _VND_SUFFIX.search(raw)
    if m:
        return _to_int(m.group(1), m.group(2)), ""

    return None, f"Unrecognized price format: {raw!r}"


def normalize_price_and_cycle(raw: str, fx: dict[str, int] | None = None) -> tuple[int | None, str, str]:
    """Normalize raw price string to monthly VND and detect billing cycle.

    Returns:
        (monthly_price_vnd, billing_cycle, note)
    """
    raw_lower = (raw or "").lower()
    fx = fx or DEFAULT_FX

    is_yearly = any(w in raw_lower for w in ("/năm", "/nam", "/yr", "/year", "năm", "annual", "12 tháng"))
    is_onetime = any(w in raw_lower for w in ("trọn đời", "vĩnh viễn", "lifetime", "one-time", "1 lần"))
    is_monthly = any(w in raw_lower for w in ("/tháng", "/thang", "/mo", "/month", "tháng"))

    cycle = "month"
    if is_yearly:
        cycle = "year"
    elif is_onetime:
        cycle = "one_time"
    elif is_monthly:
        cycle = "month"

    price, note = normalize_price(raw, fx)
    if price is None:
        return None, cycle, note

    if cycle == "year":
        monthly_price = round(price / 12)
        return monthly_price, cycle, f"Quy đổi từ giá năm ({price:,} VND) sang giá tháng (/12)"

    return price, cycle, note


def normalize_stock(raw: str) -> str:
    r = (raw or "").lower()
    # "sắp hết" contains "hết", so the LIMITED branch must win first.
    if any(k in r for k in ("sắp hết", "sap het", "limited", "low stock", "few left", "chỉ còn")):
        return "LIMITED"
    if any(k in r for k in ("hết hàng", "het hang", "out of stock", "sold out", "unavailable")):
        return "OUT_OF_STOCK"
    if any(k in r for k in ("còn hàng", "con hang", "in stock", "available", "có sẵn")):
        return "IN_STOCK"
    return "LIMITED"  # unknown -> conservative


# ---------------------------------------------------------------------------
# LLM-based comparison (with deterministic fallback)
# ---------------------------------------------------------------------------
_COMPARISON_PROMPT = """\
[ACTIVE_ROLE: DATA_NORMALIZER_ANALYST]
You are the Data Normalizer & Business Analyst Agent for RPA 2.0.

Our catalog:
{our_catalog_json}

Raw extractions from competitors:
{raw_extractions_json}

Business rules:
- Threshold: flag if any IN_STOCK competitor is cheaper than us by >= {threshold_pct}%
- Pricing formula: {pricing_formula}
- FX map: {fx_json}

{normalization_rules}

Instructions:
1. Normalize every competitor's raw price into integer VND (`normalized_price_vnd`).
2. Standardize stock into "IN_STOCK", "OUT_OF_STOCK", or "LIMITED".
3. Compute diff_percentage = (competitor_price - our_price) / our_price * 100 (rounded to 1 decimal).
4. If any IN_STOCK competitor is cheaper by >= {threshold_pct}%, set action_required = true.
5. If action_required is true, calculate recommended_new_price_vnd = int(min_competitor_price * 0.98), else null.
6. Provide analysis_summary_vn and clear Vietnamese reason_vn for each product.
"""


async def run_normalizer(
    our_catalog: list[dict],
    raw_extractions: list[dict],
    threshold_pct: float = 10.0,
    pricing_formula: str = "proposed_price = min_competitor_price * 0.98",
    fx: dict[str, int] | None = None,
) -> NormalizerOutput:
    """Run the normalizer with active_role DATA_NORMALIZER_ANALYST. Falls back to deterministic logic."""
    fx = fx or DEFAULT_FX
    prompt = _COMPARISON_PROMPT.format(
        our_catalog_json=json.dumps(our_catalog, ensure_ascii=False, indent=2),
        raw_extractions_json=json.dumps(raw_extractions, ensure_ascii=False, indent=2),
        threshold_pct=threshold_pct,
        pricing_formula=pricing_formula,
        fx_json=json.dumps(fx),
        normalization_rules=PRICE_NORMALIZATION_RULES,
    )

    try:
        raw = await llm.generate_role_json(
            active_role="DATA_NORMALIZER_ANALYST",
            prompt=prompt,
            schema=NormalizerOutput,
        )
        return NormalizerOutput.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM normalizer failed (%s) — using deterministic fallback", exc)
        return _deterministic_normalize(our_catalog, raw_extractions, threshold_pct, fx)


def _deterministic_normalize(
    our_catalog: list[dict],
    raw_extractions: list[dict],
    threshold_pct: float,
    fx: dict[str, int],
) -> NormalizerOutput:
    """Pure-Python fallback when LLM is unavailable."""
    rows: list[ComparisonRow] = []
    for item in our_catalog:
        code = item["product_code"]
        name = item.get("product_name", code)
        our_price = int(item["our_current_price_vnd"])

        competitors: list[CompetitorEntry] = []
        for ext in raw_extractions:
            # Match by product code or name similarity
            ext_code = str(ext.get("product_code") or "").strip()
            ext_name = str(ext.get("product_name") or ext.get("name") or "").strip()

            # Flexible match by code or name
            matched = False
            if ext_code and ext_code.lower() == code.lower():
                matched = True
            elif "standard" in name.lower() and any(w in ext_name.lower() for w in ("standard", "tiêu chuẩn", "cơ bản", "gói 1")):
                matched = True
            elif "business" in name.lower() and any(w in ext_name.lower() for w in ("business", "doanh nghiệp", "pro", "cao cấp", "gói 2", "gói 3")):
                matched = True
            elif ext_name and (ext_name.lower() in name.lower() or name.lower() in ext_name.lower()):
                matched = True

            if not matched:
                continue

            raw_price_str = (
                ext.get("raw_price")
                or ext.get("price_raw")
                or ext.get("price")
                or ext.get("price_vnd")
                or ""
            )
            raw_status_str = (
                ext.get("raw_status")
                or ext.get("stock_status_raw")
                or ext.get("stock_status")
                or ext.get("status")
                or ""
            )

            price, note = normalize_price(str(raw_price_str), fx)
            if price is None:
                log.warning("Skipping unparseable price for %s: %s (raw=%r)", code, note, raw_price_str)
                continue
            stock = normalize_stock(str(raw_status_str))
            diff = round((price - our_price) / our_price * 100, 1) if our_price else 0.0
            competitors.append(
                CompetitorEntry(
                    source=ext.get("source", "Đối thủ"),
                    normalized_price_vnd=price,
                    stock_status=stock,  # type: ignore[arg-type]
                    diff_percentage=diff,
                )
            )

        in_stock_cheaper = [
            c for c in competitors if c.stock_status == "IN_STOCK" and c.diff_percentage <= -threshold_pct
        ]
        action_required = len(in_stock_cheaper) > 0
        recommended = None
        reason = "Không có đối thủ nào rẻ hơn vượt ngưỡng quy định."
        if action_required:
            min_price = min(c.normalized_price_vnd for c in in_stock_cheaper)
            recommended = int(min_price * 0.98)
            cheapest = min(in_stock_cheaper, key=lambda c: c.normalized_price_vnd)
            reason = (
                f"{cheapest.source} đang còn hàng và rẻ hơn {abs(cheapest.diff_percentage):.1f}% "
                f"(vượt ngưỡng {threshold_pct}%)."
            )

        rows.append(
            ComparisonRow(
                product_code=code,
                product_name=name,
                our_current_price_vnd=our_price,
                competitors=competitors,
                action_required=action_required,
                recommended_new_price_vnd=recommended,
                reason_vn=reason,
            )
        )

    summary = (
        f"Đã chuẩn hóa & đối soát {len(rows)} sản phẩm hệ thống với {len(raw_extractions)} bản ghi thu thập. "
        f"{sum(1 for r in rows if r.action_required)} sản phẩm phát hiện đối thủ rẻ hơn cần điều chỉnh giá."
    )
    return NormalizerOutput(analysis_summary_vn=summary, comparison_table=rows)


# ---------------------------------------------------------------------------
# Generic Competitor URL Analysis & Matching
# ---------------------------------------------------------------------------
def match_competitor_to_catalog(
    comp_prod: CompetitorProduct,
    our_catalog: list[dict],
) -> tuple[Optional[dict], float, str]:
    """Matches a competitor product against OUR_CATALOG based on name keywords and specs.

    Returns:
        (best_our_item, match_confidence, note)
    """
    comp_name = comp_prod.name.lower()
    comp_cat = (comp_prod.category or "").lower().strip()
    specs = comp_prod.specs or {}

    # Category matching check (e.g. Website Hosting vs VPS)
    is_comp_vps = any(w in comp_cat or (not comp_cat and w in comp_name) for w in ("vps", "cloud server", "máy chủ ảo", "dedicated"))

    best_match: Optional[dict] = None
    best_conf = 0.0
    best_note = "Khác loại sản phẩm (không so sánh VPS với Web Hosting)" if is_comp_vps else ""

    for our_item in our_catalog:
        our_name = our_item.get("product_name", "").lower()
        our_cat = str(our_item.get("category", "")).lower().strip()
        is_our_vps = any(w in our_cat or w in our_name for w in ("vps", "cloud server", "máy chủ ảo", "dedicated"))

        # Strictly prevent comparing Website Hosting with VPS
        if is_comp_vps != is_our_vps:
            continue

        our_specs = our_item.get("specs", {})

        conf = 0.5
        note_parts: list[str] = []

        # Name / Tier matching
        if "standard" in our_name and any(k in comp_name for k in ("standard", "tiêu chuẩn", "cơ bản", "starter", "gói 1", "package 1", "package 2")):
            conf += 0.3
            note_parts.append("Khớp phân khúc Standard/Starter")
        elif "business" in our_name and any(k in comp_name for k in ("business", "doanh nghiệp", "pro", "cao cấp", "enterprise", "gói 3", "package 3", "package 4", "package 5")):
            conf += 0.3
            note_parts.append("Khớp phân khúc Business/Pro/Enterprise")

        # Spec matching (RAM, SSD)
        if specs and our_specs:
            spec_matched = 0
            if "ram" in specs and "ram" in our_specs:
                if str(specs["ram"]).lower().replace(" ", "") == str(our_specs["ram"]).lower().replace(" ", ""):
                    spec_matched += 1
            if "ssd" in specs and "ssd" in our_specs:
                if str(specs["ssd"]).lower().replace(" ", "") == str(our_specs["ssd"]).lower().replace(" ", ""):
                    spec_matched += 1

            if spec_matched >= 2:
                conf += 0.2
                note_parts.append("Cấu hình RAM & SSD tương đương")
            elif spec_matched == 1:
                conf += 0.1
                note_parts.append("Cấu hình tương đồng 1 phần")

        conf = min(1.0, max(0.3, conf))
        if conf > best_conf:
            best_conf = conf
            best_match = our_item
            best_note = "; ".join(note_parts) or "Đối chiếu theo phân khúc giá"

    return best_match, best_conf, best_note


def _deterministic_price_analysis(
    our_catalog: list[dict],
    competitor_products: list[CompetitorProduct],
    failed_urls: dict[str, str] | None = None,
) -> PriceAnalysisReport:
    """Pure-Python strategic price analysis & report generation."""
    failed_urls = failed_urls or {}
    matches: list[MatchedItem] = []
    caveats: list[str] = []

    valid_prices: list[int] = []

    for comp in competitor_products:
        if comp.price_vnd is None:
            continue
        valid_prices.append(comp.price_vnd)

        best_item, conf, note = match_competitor_to_catalog(comp, our_catalog)
        if not best_item:
            continue

        our_price = int(best_item.get("our_current_price_vnd", 0))
        delta = round((comp.price_vnd - our_price) / our_price * 100, 1) if our_price else 0.0

        if conf < 0.6:
            caveats.append(
                f"Sản phẩm '{comp.name}' ({comp.price_vnd:,} VND) chưa so sánh ngang cấu hình với '{best_item.get('product_name')}' (độ tin cậy: {conf:.2f})."
            )

        if comp.original_price_vnd and comp.original_price_vnd > comp.price_vnd:
            caveats.append(
                f"Sản phẩm '{comp.name}' đang áp dụng giá khuyến mãi kỳ đầu ({comp.price_vnd:,} VND so với giá gốc {comp.original_price_vnd:,} VND)."
            )

        matches.append(
            MatchedItem(
                our_sku=best_item.get("product_code", ""),
                competitor=comp.source_url or "Đối thủ",
                competitor_product=comp,
                match_confidence=conf,
                delta_pct=delta,
                note=note,
            )
        )

    # Compute market stats
    market_min = min(valid_prices) if valid_prices else None
    if valid_prices:
        sorted_p = sorted(valid_prices)
        n = len(sorted_p)
        market_median = sorted_p[n // 2] if n % 2 != 0 else round((sorted_p[n // 2 - 1] + sorted_p[n // 2]) / 2)
    else:
        market_median = None

    # Determine shop position
    avg_delta = sum(m.delta_pct for m in matches) / len(matches) if matches else 0.0
    if avg_delta < -8.0:
        our_position = "PRICIER"
    elif avg_delta > 8.0:
        our_position = "CHEAPER"
    else:
        our_position = "PARITY"

    # Strategic options
    suggestions: list[str] = []
    if our_position == "PRICIER":
        suggestions.append(
            "Phương án 1 (Giữ giá & Nâng cao giá trị): Giữ nguyên giá niêm yết hiện tại, tập trung nhấn mạnh dịch vụ hỗ trợ 24/7 và cam kết SLA 99.9% để bảo toàn biên lợi nhuận. (Rủi ro: Có thể mất khách hàng nhạy cảm về giá)."
        )
        if market_min:
            undercut_price = round(market_min * 0.98)
            suggestions.append(
                f"Phương án 2 (Cạnh tranh trực diện): Điều chỉnh giảm về mức {undercut_price:,} VND/tháng để giành thị phần từ đối thủ rẻ nhất. (Rủi ro: Biên lợi nhuận giảm, cần tối ưu chi phí hạ tầng)."
            )
        suggestions.append(
            "Phương án 3 (Tặng kèm gói gia tăng): Bổ sung chứng chỉ SSL miễn phí trọn đời và Plugin cache tăng tốc thay vì hạ giá bán trực tiếp. (Rủi ro: Chi phí tích hợp ban đầu)."
        )
    elif our_position == "CHEAPER":
        suggestions.append(
            "Phương án 1 (Giữ lợi thế giá rẻ): Duy trì mức giá hiện tại để tiếp tục mở rộng tệp khách hàng mới với thông điệp 'Giá tốt nhất phân khúc'. (Rủi ro: Biên lợi nhuận thấp)."
        )
        suggestions.append(
            "Phương án 2 (Tối ưu hóa doanh thu): Nâng nhẹ giá 5-8% đối với các gói chu kỳ ngắn nhằm tăng doanh thu trung bình trên mỗi khách hàng (ARPU). (Rủi ro: Cần theo dõi tỷ lệ chuyển đổi)."
        )
    else:
        suggestions.append(
            "Phương án 1 (Duy trì vị thế): Giá hiện tại đang ở mức cân bằng so với thị trường. Tiếp tục theo dõi biến động định kỳ hàng tuần."
        )
        suggestions.append(
            "Phương án 2 (Chương trình theo chu kỳ dài): Khuyến khích thanh toán gói 1-2 năm với mức chiết khấu 15-20% để khóa dòng tiền ổn định."
        )

    return PriceAnalysisReport(
        matches=matches,
        market_min=market_min,
        market_median=market_median,
        our_position=our_position,
        suggestions=suggestions,
        caveats=caveats,
        failed_urls=failed_urls,
    )


async def run_price_analyst(
    our_catalog: list[dict],
    competitor_products: list[CompetitorProduct],
    failed_urls: dict[str, str] | None = None,
) -> PriceAnalysisReport:
    """Run Role 5 PRICE_ANALYST to analyze arbitrary URLs with LLM, falling back to deterministic."""
    failed_urls = failed_urls or {}
    if not competitor_products:
        return _deterministic_price_analysis(our_catalog, [], failed_urls)

    prompt = PRICE_ANALYST_PROMPT_TEMPLATE.format(
        our_catalog_json=json.dumps(our_catalog, ensure_ascii=False, indent=2),
        scraped_products_json=json.dumps([p.model_dump(mode="json") for p in competitor_products], ensure_ascii=False, indent=2),
    )

    try:
        raw = await llm.generate_role_json(
            active_role="PRICE_ANALYST",
            prompt=prompt,
            schema=PriceAnalysisReport,
        )
        report = PriceAnalysisReport.model_validate(raw)
        # Ensure failed_urls is merged
        if failed_urls:
            report.failed_urls.update(failed_urls)
        return report
    except Exception as exc:
        log.warning("PRICE_ANALYST LLM call failed (%s) — using deterministic fallback", exc)
        return _deterministic_price_analysis(our_catalog, competitor_products, failed_urls)

