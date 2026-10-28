"""Patch the Gemini rotator with deterministic stub responses.

Import this BEFORE app.main. Enables a fully offline, 100%-reliable demo/test:
no API keys, no network, no LLM nondeterminism.
"""
from __future__ import annotations

from app.services import llm_rotator as LR
from app.schemas import AgentTurn, WorkflowDAG, NormalizerOutput, ComparisonRow, CompetitorEntry

_call_count = {"n": 0}


async def _stub_generate_json(prompt: str, schema, system_instruction=None, system=None, **kw):
    """Return a canned, schema-valid object depending on the requested schema."""
    _call_count["n"] += 1

    if schema is WorkflowDAG:
        return WorkflowDAG(
            workflow_id="stub",
            intent_summary="So sánh giá và cập nhật giá khi đối thủ rẻ hơn 10%",
            parallel_worker_tasks=[
                {
                    "worker_id": "w1",
                    "target_name": "Competitor A",
                    "start_url": "http://127.0.0.1:8000/demo/competitor_a.html",
                    "goal": "Đóng popup modal rồi trích xuất giá gói Hosting WordPress.",
                    "target_fields": ["product_name", "price_vnd", "billing_cycle", "stock_status"],
                },
                {
                    "worker_id": "w2",
                    "target_name": "Competitor B",
                    "start_url": "http://127.0.0.1:8000/demo/competitor_b.html",
                    "goal": "Scroll hết trang rồi trích xuất giá và trạng thái tồn kho.",
                    "target_fields": ["product_name", "price_vnd", "billing_cycle", "stock_status"],
                },
            ],
            business_rule_evaluation={
                "condition_description": "Nếu đối thủ IN_STOCK rẻ hơn >= 10%",
                "pricing_formula": "proposed_price = min_competitor_price * 0.98",
            },
            mutation_action_task={
                "enabled": True,
                "target_system": "Internal Shop Admin Portal",
                "action_description": "Admin -> điền giá -> dừng trước nút Lưu thay đổi",
                "requires_hitl_approval": True,
                "hitl_channel": "telegram",
            },
        )

    if schema is AgentTurn:
        on_a = "competitor_a.html" in prompt
        on_b = "competitor_b.html" in prompt
        on_admin = "admin.html" in prompt
        turn_no = prompt.count('"action":')

        if on_a and "promoModal" in prompt or (on_a and "Nhận mã ngay" in prompt):
            return AgentTurn(
                thought="Có popup quảng cáo che màn hình. Bấm 'Để sau' hoặc nút đóng để tiếp tục quan sát bảng giá.",
                action_type="CLICK",
                target_element_id=1,
                self_healing_triggered=True,
            )
        if on_a:
            return AgentTurn(
                thought="Popup đã đóng. Trích xuất thành công giá gói Hosting WordPress Standard 250.000đ.",
                action_type="EXTRACT_DATA",
                extracted_payload=[
                    {
                        "product_code": "HOSTING-WP-STD",
                        "product_name": "Hosting WordPress Standard",
                        "raw_price": "250.000đ",
                        "raw_status": "Còn hàng",
                        "source": "PA Vietnam",
                    },
                ],
            )
        if on_b and turn_no < 1:
            return AgentTurn(
                thought="Danh sách dài dạng lazy scroll. Cuộn xuống để load đủ các gói hosting.",
                action_type="SCROLL_DOWN",
            )
        if on_b:
            return AgentTurn(
                thought="Đã cuộn tải đủ danh sách. Trích xuất giá và tồn kho của Mắt Bão: 199.000đ/tháng.",
                action_type="EXTRACT_DATA",
                extracted_payload=[
                    {
                        "product_code": "HOSTING-WP-STD",
                        "product_name": "Hosting WordPress Standard",
                        "raw_price": "199.000đ/tháng",
                        "raw_status": "in stock",
                        "source": "Mat Bao",
                    },
                ],
            )
        if on_admin and "price-0" in prompt:
            return AgentTurn(
                thought="Đã nhập giá mới vào ô input price-0. DỪNG LẠI ngay trước nút Lưu thay đổi để chờ HITL duyệt.",
                action_type="REQUEST_HITL_APPROVAL",
            )
        if on_admin:
            return AgentTurn(
                thought="DỪNG LẠI trước nút Lưu thay đổi để yêu cầu phê duyệt.",
                action_type="REQUEST_HITL_APPROVAL",
            )
        return AgentTurn(thought="Hoàn tất trích xuất dữ liệu an toàn.", action_type="EXTRACT_DATA")

    if schema is NormalizerOutput:
        return NormalizerOutput(
            analysis_summary_vn="Phát hiện Mắt Bão đang bán rẻ hơn 20.4%, đề xuất cập nhật giá.",
            comparison_table=[
                ComparisonRow(
                    product_code="HOSTING-WP-STD",
                    product_name="Gói Hosting WordPress Standard",
                    our_current_price_vnd=250000,
                    competitors=[
                        CompetitorEntry(
                            source="Mat Bao",
                            normalized_price_vnd=199000,
                            stock_status="IN_STOCK",
                            diff_percentage=-20.4,
                        )
                    ],
                    action_required=True,
                    recommended_new_price_vnd=195000,
                    reason_vn="Mat Bao đang còn hàng và rẻ hơn 20.4% (vượt ngưỡng 10%).",
                )
            ],
        )

    raise RuntimeError(f"stub: unsupported schema {schema}")


async def _stub_generate_role_json(active_role: str, prompt: str, schema, **kw):
    return await _stub_generate_json(prompt=prompt, schema=schema, **kw)


LR.rotator.generate_json = _stub_generate_json  # type: ignore[method-assign]
LR.rotator.generate_role_json = _stub_generate_role_json  # type: ignore[method-assign]
LR.llm.generate_json = _stub_generate_json  # type: ignore[method-assign]
LR.llm.generate_role_json = _stub_generate_role_json  # type: ignore[method-assign]