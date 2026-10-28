"""Strategic Pricing Advisor (Role 5 / Price Analyst).

Provides structured pricing recommendations comparing OUR_CATALOG against
cached competitor data (URL analysis or standard scan).
Enforces MIN_PRICE_FLOOR, prevents race-to-the-bottom, and prepares actionable HITL options.
"""
from __future__ import annotations

import html
import logging
import time
from typing import Any

from app.config import settings
from app.schemas import PriceAnalysisReport
from app.services.conversation_memory import memory
from app.services.orchestrator import OUR_CATALOG

log = logging.getLogger("rpa.pricing_advisor")


def _get_catalog_item(sku_or_name: str | None) -> dict[str, Any]:
    if not sku_or_name:
        return OUR_CATALOG[0]
    s = sku_or_name.lower().strip()
    for item in OUR_CATALOG:
        if s in item["product_code"].lower() or s in item["product_name"].lower():
            return item
    if "bus" in s:
        return OUR_CATALOG[1]
    return OUR_CATALOG[0]


async def advise_pricing(
    chat_id: str,
    target_sku: str | None = None,
) -> tuple[str, dict | None]:
    """Generate strategic pricing advice comparing shop catalog to competitor cache.

    Returns:
        (formatted_html_text, telegram_reply_markup)
    """
    session = memory.get_session(chat_id)
    url_report: PriceAnalysisReport | None = session.get_valid_url_report()
    scan_result: dict[str, Any] | None = session.get_valid_scan()

    # Fallback to global scan result if session empty
    from app.services.telegram_bot import LATEST_SCAN_RESULT
    if not scan_result and LATEST_SCAN_RESULT and "comparison_table" in LATEST_SCAN_RESULT:
        scan_result = LATEST_SCAN_RESULT

    # 1. If NO competitor data is cached or available:
    if not url_report and not scan_result:
        text = (
            "ℹ️ <b>DẠ EM CHƯA CÓ DỮ LIỆU ĐỐI THỦ MỚI ĐỂ TƯ VẤN CHÍNH XÁC!</b>\n\n"
            "Để bảo vệ biên lợi nhuận của shop và không đoán mò, em cần quét dữ liệu bảng giá thực tế từ các đối thủ trước.\n\n"
            "👉 <b>Anh/chị có muốn em quét đối thủ ngay bây giờ không ạ?</b>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🔍 Quét giá đối thủ ngay", "callback_data": "RUN_DEFAULT_WORKFLOW"}],
                [{"text": "📦 Xem bảng giá shop", "callback_data": "SHOW_CATALOG_ACTION"}],
            ]
        }
        return text, markup

    # 2. Target item resolution
    item = _get_catalog_item(target_sku or session.active_sku)
    sku = item["product_code"]
    p_name = item["product_name"]
    our_price = item["our_current_price_vnd"]
    floor_price = item.get("floor_price_vnd", settings.min_price_floor_std)

    memory.set_active_sku(chat_id, sku)

    # 3. Analyze competitor baseline from either URL report or scan result
    comp_min = None
    comp_median = None
    comp_notes = []

    if url_report:
        comp_min = url_report.market_min
        comp_median = url_report.market_median
        for m in url_report.matches:
            if m.our_sku == sku or not target_sku:
                disp_p = f"{m.competitor_product.price_vnd:,}" if m.competitor_product.price_vnd else (m.competitor_product.raw_price_text or "0")
                comp_notes.append(f"• {m.competitor}: <code>{disp_p} VND</code> ({m.delta_pct:+.1f}%)")
    elif scan_result:
        for row in scan_result.get("comparison_table", []):
            if row.get("product_code") == sku:
                comps = row.get("competitors", [])
                prices = [c.get("normalized_price_vnd") for c in comps if c.get("normalized_price_vnd")]
                if prices:
                    comp_min = min(prices)
                    comp_median = sorted(prices)[len(prices) // 2]
                for c in comps:
                    comp_notes.append(f"• {c.get('source', 'Đối thủ')}: <code>{c.get('normalized_price_vnd', 0):,} VND</code> ({c.get('diff_percentage', 0.0):+.1f}%)")

    # Safe defaults if no competitor matched
    comp_min = comp_min or int(our_price * 0.75)
    comp_median = comp_median or int(our_price * 0.85)

    # 4. Formulate 3 Strategic Options respecting MIN_PRICE_FLOOR
    # Option A: Value-Add & Keep Price
    opt_a_price = our_price
    opt_a_title = "Giữ giá & Nâng cao giá trị (Bảo toàn lợi nhuận)"
    opt_a_desc = (
        f"Giữ nguyên giá niêm yết <b>{opt_a_price:,} VND</b>. Nhấn mạnh hỗ trợ kỹ thuật 24/7 trực tiếp, "
        "cam kết máy chủ đặt tại Datacenter Việt Nam (độ trễ thấp dưới 10ms) và tặng miễn phí SSL Let's Encrypt."
    )
    opt_a_risk = "Có thể chậm mở rộng khách hàng mới nhạy cảm về giá thuần túy."

    # Option B: Optimal Market Adjustment (Strictly >= MIN_PRICE_FLOOR)
    # Never race below floor price!
    raw_target = int(comp_median * 0.95) if comp_median >= floor_price else floor_price
    opt_b_price = max(raw_target, floor_price)
    # Round to clean thousand
    opt_b_price = (opt_b_price // 1000) * 1000

    delta_b = round((opt_b_price - our_price) / our_price * 100, 1)
    opt_b_title = f"Cạnh tranh tối ưu (Điều chỉnh về {opt_b_price:,} VND)"
    opt_b_desc = (
        f"Điều chỉnh giá về <b>{opt_b_price:,} VND/tháng</b> ({delta_b:+.1f}%). "
        f"Mức giá này đảm bảo trên <b>giá sàn tối thiểu ({floor_price:,} VND)</b> để bảo vệ lợi nhuận gộp, "
        "đồng thời thu hẹp khoảng cách đáng kể so với mức giá thị trường."
    )
    opt_b_risk = f"Biên lợi nhuận gộp giảm nhẹ khoảng {abs(delta_b):.0f}%, cần bù đắp bằng số lượng thuê bao."

    # Option C: Long-term Cycle Lock-in
    opt_c_annual_monthly = int(our_price * 0.8)
    opt_c_annual_monthly = max(opt_c_annual_monthly, floor_price)
    opt_c_title = "Khuyến mại thanh toán 1-2 năm (Khóa dòng tiền)"
    opt_c_desc = (
        f"Giữ giá gói lẻ 1 tháng, nhưng áp dụng mức <b>{opt_c_annual_monthly:,} VND/tháng</b> khi thanh toán từ 12 tháng trở lên "
        "(tặng thêm 2 tháng sử dụng). Khóa chân khách hàng lâu dài và thu hồi vốn ngay."
    )
    opt_c_risk = "Dòng tiền tập trung đầu chu kỳ, cần duy trì chất lượng hạ tầng ổn định."

    # Save to memory for rapid one-click application
    suggested_options = {
        "A": {"sku": sku, "price_vnd": opt_a_price, "title": opt_a_title},
        "B": {"sku": sku, "price_vnd": opt_b_price, "title": opt_b_title},
        "C": {"sku": sku, "price_vnd": opt_c_annual_monthly, "title": opt_c_title},
    }
    memory.set_suggested_options(chat_id, suggested_options)

    # 5. Format Telegram Message
    lines = [
        f"📊 <b>TƯ VẤN ĐIỀU CHỈNH GIÁ: {html.escape(p_name)}</b>\n",
        f"• <b>Mã sản phẩm:</b> <code>{sku}</code>",
        f"• <b>Giá hiện tại của shop:</b> <code>{our_price:,} VND/tháng</code>",
        f"• <b>Giá sàn tối thiểu (Floor):</b> <code>{floor_price:,} VND</code> <i>(quy tắc bảo vệ vốn)</i>",
        f"• <b>Thị trường đối thủ:</b> Min <code>{comp_min:,} VND</code> | Trung vị <code>{comp_median:,} VND</code>\n",
    ]

    if comp_notes:
        lines.append("🔍 <b>Dữ liệu đối thủ đã ghi nhận:</b>")
        lines.extend(comp_notes[:3])
        lines.append("")

    lines.append("💡 <b>CÁC PHƯƠNG ÁN CHIẾN LƯỢC ĐỀ XUẤT:</b>\n")
    lines.append(f"<b>1. Phương án A — {opt_a_title}:</b>\n{opt_a_desc}\n<i>⚠️ Rủi ro: {opt_a_risk}</i>\n")
    lines.append(f"<b>2. Phương án B — {opt_b_title} ⭐ (ƯU TIÊN):</b>\n{opt_b_desc}\n<i>⚠️ Rủi ro: {opt_b_risk}</i>\n")
    lines.append(f"<b>3. Phương án C — {opt_c_title}:</b>\n{opt_c_desc}\n<i>⚠️ Rủi ro: {opt_c_risk}</i>\n")

    lines.append(
        "👉 <i>Bạn có thể bấm nút bên dưới để chọn áp dụng, hoặc nhắn: "
        f"<b>'Áp dụng phương án B'</b> hay <b>'Sửa thành {opt_b_price:,}đ'</b>!</i>"
    )

    markup = {
        "inline_keyboard": [
            [
                {"text": f"✅ Áp dụng PA B ({opt_b_price:,}đ)", "callback_data": f"APPLY_OPT_B_{sku}"},
                {"text": "🛡️ Giữ giá (PA A)", "callback_data": f"APPLY_OPT_A_{sku}"},
            ],
            [
                {"text": "🔍 Quét lại đối thủ", "callback_data": "RUN_DEFAULT_WORKFLOW"},
                {"text": "📦 Xem bảng giá", "callback_data": "SHOW_CATALOG_ACTION"},
            ],
        ]
    }

    return "\n".join(lines), markup
