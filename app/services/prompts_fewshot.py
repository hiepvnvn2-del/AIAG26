"""Few-shot structured examples for multi-agent roles in RPA 2.0.

All few-shots are defined as typed data structures (list[dict]), never
hardcoded text blobs, ensuring clean formatting, easy ablation, and zero leakage.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# BROWSER_WORKER Few-Shot Examples (ReAct Observe -> Think -> Act)
# ---------------------------------------------------------------------------
WORKER_FEW_SHOTS: list[dict] = [
    {
        "scenario": "Modal / Cookie Banner Blocking View",
        "observation": "Màn hình bị lớp phủ mờ (modal-bg) che khuất bởi thông báo cookie với nút 'Đồng ý' (id=acceptCookie) và nút đóng (id=closeBanner).",
        "agent_turn": {
            "thought": "Phát hiện banner cookie che khuất toàn bộ giao diện bảng giá. Cần đóng banner trước khi cào dữ liệu.",
            "action": "CLICK",
            "target_element_id": "acceptCookie",
            "value": None,
        },
    },
    {
        "scenario": "Admin Price Update Staging (Sensitive Gate)",
        "observation": "Đã điền xong giá mới 180.000đ vào ô nhập giá (id=priceInput). Nút 'Lưu thay đổi' (id=saveBtn) hiển thị sẵn sàng.",
        "agent_turn": {
            "thought": "Đã điền giá mới vào form admin. Đây là thao tác thay đổi dữ liệu nhạy cảm, tuyệt đối không bấm Lưu mà phải yêu cầu con người phê duyệt.",
            "action": "REQUEST_HITL_APPROVAL",
            "target_element_id": "saveBtn",
            "value": None,
        },
    },
    {
        "scenario": "View More / Paginated Content",
        "observation": "Bảng giá chỉ hiển thị 3 gói đầu. Cuối danh sách có nút 'Xem thêm gói' (id=loadMore).",
        "agent_turn": {
            "thought": "Chưa hiển thị hết các gói hosting theo yêu cầu. Bấm nút xem thêm để mở rộng dữ liệu.",
            "action": "CLICK",
            "target_element_id": "loadMore",
            "value": None,
        },
    },
]

# ---------------------------------------------------------------------------
# DATA_NORMALIZER_ANALYST Few-Shot Examples
# ---------------------------------------------------------------------------
NORMALIZER_FEW_SHOTS: list[dict] = [
    {
        "scenario": "Foreign Currency & Fractional Unit",
        "input": {
            "raw_price": "$8.50/mo",
            "stock_status": "in stock",
            "our_price": 250000,
        },
        "output": {
            "normalized_price_vnd": 215900,
            "stock_status": "IN_STOCK",
            "diff_percentage": -13.6,
            "action_required": True,
            "recommended_new_price_vnd": 211582,
            "reason_vn": "Đối thủ dùng tiền tệ USD ($8.50 quy đổi 215.900 VND), rẻ hơn 13.6% vượt ngưỡng 10%.",
        },
    },
    {
        "scenario": "Yearly Discount to Monthly Alignment",
        "input": {
            "raw_price": "2.400.000đ/năm",
            "stock_status": "còn hàng",
            "our_price": 250000,
        },
        "output": {
            "normalized_price_vnd": 200000,
            "stock_status": "IN_STOCK",
            "diff_percentage": -20.0,
            "action_required": True,
            "recommended_new_price_vnd": 196000,
            "reason_vn": "Gói tính theo năm được quy đổi về 200.000 VND/tháng, rẻ hơn 20% so với giá tháng của shop.",
        },
    },
    {
        "scenario": "Out of Stock Competitor Cheaper",
        "input": {
            "raw_price": "149k/tháng",
            "stock_status": "hết hàng",
            "our_price": 250000,
        },
        "output": {
            "normalized_price_vnd": 149000,
            "stock_status": "OUT_OF_STOCK",
            "diff_percentage": -40.4,
            "action_required": False,
            "recommended_new_price_vnd": None,
            "reason_vn": "Đối thủ giá rất rẻ nhưng đã hết hàng (OUT_OF_STOCK), không cần điều chỉnh giá cạnh tranh.",
        },
    },
]

# ---------------------------------------------------------------------------
# PRICE_ANALYST (URL Analysis & Strategic Options) Few-Shots
# ---------------------------------------------------------------------------
PRICE_ANALYST_FEW_SHOTS: list[dict] = [
    {
        "scenario": "Competitor Cheaper with Strikethrough Promotional Pricing",
        "input_summary": "Đối thủ bán 63.246đ/tháng (gốc 129.000đ), shop đang bán 250.000đ/tháng gói WP Standard.",
        "output_report": {
            "our_position": "PRICIER",
            "market_min": 63246,
            "market_median": 88646,
            "suggestions": [
                "Phương án 1 (Giữ giá & Khẳng định chất lượng): Giữ 250.000đ, đẩy mạnh quảng bá tốc độ NVMe và cam kết hỗ trợ kỹ thuật 24/7.",
                "Phương án 2 (Cạnh tranh trực tiếp gói khuyến mãi): Điều chỉnh giảm về 61.900đ/tháng kỳ đầu để kéo khách mới.",
                "Phương án 3 (Tặng kèm giá trị): Tặng miễn phí tên miền .vn hoặc bản quyền Theme/Plugin cao cấp khi đăng ký 1 năm.",
            ],
            "caveats": [
                "Giá đối thủ là mức khuyến mãi chu kỳ đầu, phí gia hạn thực tế cao hơn đáng kể.",
            ],
        },
    },
]


def format_few_shots_for_prompt(few_shots: list[dict], section_title: str, escape_braces: bool = True) -> str:
    """Format structured few-shots into prompt-ready markdown string.

    If escape_braces is True, escapes '{' and '}' to '{{' and '}}' so the string
    can safely be embedded inside Python .format() templates.
    """
    if not few_shots:
        return ""
    import json
    lines = [f"\n--- {section_title} (FEW-SHOT EXAMPLES) ---"]
    for i, ex in enumerate(few_shots, 1):
        lines.append(f"Example {i}:")
        formatted_json = json.dumps(ex, ensure_ascii=False, indent=2)
        if escape_braces:
            formatted_json = formatted_json.replace("{", "{{").replace("}", "}}")
        lines.append(formatted_json)
    lines.append("--------------------------------------------------\n")
    return "\n".join(lines)
