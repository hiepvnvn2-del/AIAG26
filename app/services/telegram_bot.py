"""Interactive Telegram Chatbot for Shop Admin.

Allows the admin to:
1. Override staged prices via natural chat (e.g., "Sửa thành 180k", "Đặt giá 190.000đ") while Playwright is frozen.
2. Trigger competitor price checking via natural speech (e.g., "Kiểm tra giá đối thủ", "/run").
3. View internal catalog prices (/catalog) and active workflow status (/status).
4. Chat conversationally for shop administrative queries.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
from typing import Optional

import httpx

from app.config import settings
from app.schemas import HITLCallback, PriceAnalysisReport
from app.services import hitl_gate
from app.services.conversation_memory import memory
from app.services.intent_router import route_intent
from app.services.llm_rotator import llm
from app.services.orchestrator import (
    OUR_CATALOG,
    Orchestrator,
    run_url_analysis,
    stage_manual_price_change,
)
from app.services.pricing_advisor import advise_pricing

log = logging.getLogger("rpa.telegram_bot")

_URL_REGEX = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+|(?:\b[a-zA-Z0-9-]+\.(?:com|vn|net|org|io|biz|info|dev|co|xyz)(?:/[^\s<>\"']*)?)",
    re.IGNORECASE,
)


def normalize_target_url(raw: str) -> str:
    """Normalize raw matched url string, adding https:// if scheme is missing."""
    raw = raw.strip().rstrip(".,;!?:")
    if not raw.startswith(("http://", "https://")):
        return f"https://{raw}"
    return raw

# Regex to detect price override intentions from chat:
# matches "sửa thành 180k", "đổi giá 190.000", "đặt 185k", "180k", "180000", "/price 180000"
_PRICE_PATTERN = re.compile(
    r"(?:sửa|đổi|chỉnh|đặt|lưu|giá|thành|mức|set|price)?\s*(\d+[\d.,]*)\s*(k|đ|vnd|vnđ|nghìn|ngàn)?",
    re.IGNORECASE,
)


def extract_override_price(text: str) -> Optional[int]:
    """Parse numeric price from natural text like 'sửa thành 180k', 'đổi giá 195.000đ', '180000'."""
    text_clean = text.lower().strip()
    # Check if text looks like a price adjustment
    keywords = ["sửa", "đổi", "chỉnh", "đặt", "lưu", "thành", "giá", "price", "k", "đ", "vnd", "nghìn", "ngàn"]
    has_keyword = any(k in text_clean for k in keywords) or text_clean.startswith("/")
    has_digit = any(c.isdigit() for c in text_clean)

    if not (has_keyword and has_digit):
        return None

    for m in _PRICE_PATTERN.finditer(text_clean):
        amount_str = m.group(1)
        suffix = m.group(2)
        if not amount_str:
            continue
        try:
            cleaned = amount_str.replace(".", "").replace(",", "").strip()
            if not cleaned.isdigit():
                continue
            val = int(cleaned)
            if suffix and suffix.lower() in ("k", "nghìn", "ngàn"):
                val *= 1000
            elif val < 1000 and ("k" in text_clean or suffix):
                val *= 1000
            # Sanity check: hosting price in VND should typically be >= 10,000
            if 10000 <= val <= 50000000:
                return val
        except Exception:
            continue
    return None


async def send_telegram_text(text: str, reply_markup: dict | None = None, chat_id: str | None = None) -> bool:
    """Send HTML text message to Telegram chat with auto-fallback on markup errors."""
    token = settings.telegram_bot_token
    target_chat = chat_id or settings.telegram_chat_id
    if not token or not target_chat:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                return True

            log.warning("send_telegram_text HTTP %s: %s", r.status_code, r.text)
            # If HTML parsing failed, retry cleanly without parse_mode
            if r.status_code == 400 and "can't parse entities" in r.text.lower():
                log.info("Retrying sendMessage without HTML parse_mode...")
                plain_payload = {
                    "chat_id": target_chat,
                    "text": re.sub(r"<[^>]+>", "", text),
                }
                if reply_markup:
                    plain_payload["reply_markup"] = reply_markup
                r_retry = await client.post(url, json=plain_payload)
                return r_retry.status_code == 200

            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("send_telegram_text failed: %s", exc)
        return False


async def send_telegram_photo(caption: str, photo_bytes: bytes, reply_markup: dict | None = None, chat_id: str | None = None) -> bool:
    """Send photo with caption to Telegram chat."""
    token = settings.telegram_bot_token
    target_chat = chat_id or settings.telegram_chat_id
    if not token or not target_chat:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        data = {
            "chat_id": target_chat,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        files = {"photo": ("screenshot.jpg", photo_bytes, "image/jpeg")}
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(url, data=data, files=files)
            return r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("send_telegram_photo failed: %s", exc)
        return False


async def send_insights(chat_id: str) -> None:
    """Send learned HITL intelligence & decision statistics."""
    from app.services.feedback_learner import analyze_admin_feedback

    stats = analyze_admin_feedback()
    total = stats.get("total_decisions", 0)

    lines = [
        "🧠 <b>THỐNG KÊ TRÍ TUỆ HITL & HÀNH VI ADMIN (/insights)</b>",
        "",
        f"• <b>Tổng phiên HITL đã xử lý:</b> <code>{total}</code>",
        f"• <b>Tỷ lệ chấp thuận (Approval Rate):</b> <b>{stats.get('approval_rate', '0%')}</b>",
        f"• <b>Duyệt trực tiếp:</b> {stats.get('approved', 0)} | <b>Hủy bỏ:</b> {stats.get('rejected', 0)} | <b>Override thủ công:</b> {stats.get('overridden', 0)}",
        "",
        "📊 <b>PHÂN BỐ THEO MỨC ĐỘ RỦI RO (RISK LEVEL):</b>",
    ]

    by_risk = stats.get("by_risk_level", {})
    if by_risk:
        for r_name, r_data in by_risk.items():
            lines.append(f"• <b>{r_name}:</b> {r_data.get('approved', 0)}/{r_data.get('total', 0)} đã duyệt")
    else:
        lines.append("• <i>Chưa có dữ liệu phân loại rủi ro.</i>")

    lines.append("")
    lines.append("💡 <b>QUY LUẬT HỆ THỐNG ĐÃ HỌC TỪ ADMIN:</b>")
    for ins in stats.get("learned_insights", []):
        lines.append(f"• {html.escape(ins)}")

    lines.append("")
    lines.append("<i>⚡ Hệ thống tự động áp dụng các quy luật trên vào thuật toán định giá để giảm thiểu tỷ lệ từ chối.</i>")

    msg = "\n".join(lines)
    await send_telegram_text(msg, chat_id=chat_id)
    memory.add_assistant_message(chat_id, msg)


async def send_catalog(chat_id: str) -> None:
    """Send current internal shop catalog with quick action buttons."""
    lines = ["📦 <b>BẢNG GIÁ DỊCH VỤ HIỆN TẠI CỦA SHOP</b>\n"]
    for idx, item in enumerate(OUR_CATALOG, 1):
        lines.append(
            f"{idx}. <b>{html.escape(item['product_name'])}</b>\n"
            f"   • Mã SKU: <code>{item['product_code']}</code>\n"
            f"   • Giá niêm yết: <code>{item['our_current_price_vnd']:,} VND/tháng</code>\n"
        )
    lines.append("💡 <i>Bạn muốn so sánh với đối thủ hoặc nhận tư vấn chiến lược giá? Chọn nút bên dưới:</i>")
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "🔍 Quét giá đối thủ", "callback_data": "RUN_DEFAULT_WORKFLOW"},
                {"text": "💡 Tư vấn điều chỉnh giá", "callback_data": "ADVISORY_ACTION"},
            ]
        ]
    }
    await send_telegram_text("\n".join(lines), reply_markup=reply_markup, chat_id=chat_id)


async def send_advisory(chat_id: str, target_sku: str | None = None) -> None:
    """Send strategic pricing advice."""
    reply_text, markup = await advise_pricing(chat_id, target_sku=target_sku)
    await send_telegram_text(reply_text, reply_markup=markup, chat_id=chat_id)
    memory.add_assistant_message(chat_id, reply_text)


async def handle_telegram_message(msg: dict) -> None:
    """Intelligent natural language router for Telegram messages using Fast-Path and LLM Intent Routing."""
    chat_id = str(msg.get("chat", {}).get("id", settings.telegram_chat_id))
    text = (msg.get("text") or "").strip()
    if not text:
        return

    log.info("[Telegram Chat] From %s: %s", chat_id, text)

    # 1. Security Whitelist Check
    allowed = [s.strip() for s in (settings.allowed_chat_ids or settings.telegram_chat_id).split(",") if s.strip()]
    if allowed and chat_id not in allowed:
        log.warning("Unauthorized access attempt from chat_id=%s", chat_id)
        await send_telegram_text("⛔ <b>Bạn không có quyền truy cập hệ thống RPA của shop.</b>", chat_id=chat_id)
        return

    # Record message in conversation memory
    memory.add_user_message(chat_id, text)

    # 1.1 Direct Commands (/insights, /catalog)
    clean_lower = text.strip().lower()
    if clean_lower in ("/insights", "insights", "thống kê", "/stats", "học được gì"):
        await send_insights(chat_id)
        return

    # 2. Contextual Intent Routing (Fast-Path Heuristics -> LLM with 16 Few-Shots)
    has_pending = bool(hitl_gate.pending_approvals)
    ctx = memory.get_context(chat_id, has_pending_hitl=has_pending)
    intent_res = await route_intent(text, ctx)

    # 3. Intent Dispatcher
    # --- SHOW_CATALOG ---
    if intent_res.intent == "SHOW_CATALOG":
        await send_catalog(chat_id)
        return

    # --- GENERAL_CHAT ---
    if intent_res.intent == "GENERAL_CHAT":
        help_text = (
            "🤖 <b>RPA 2.0 AI Assistant — Trợ lý Điều hành Shop</b>\n\n"
            "Chào bạn! Tôi là chatbot tự động hóa điều khiển trình duyệt và quản lý bảng giá shop. "
            "Bạn có thể nhắn tiếng Việt tự nhiên bất kỳ hoặc chọn chức năng bên dưới:\n\n"
            "• 🔍 <b>Quét giá đối thủ:</b> Cào bảng giá các website đối thủ cạnh tranh\n"
            "• 💡 <b>Tư vấn điều chỉnh giá:</b> Đề xuất 3 phương án chiến lược bảo vệ biên lợi nhuận\n"
            "• ⚡ <b>Sửa giá nhanh:</b> Nhắn <i>'Sửa gói Standard thành 180k'</i> hoặc <i>'Áp dụng phương án B'</i>\n"
            "• 📦 <b>Bảng giá dịch vụ:</b> Xem danh mục niêm yết hiện tại của shop"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "📦 Xem bảng giá", "callback_data": "SHOW_CATALOG_ACTION"}, {"text": "🔍 Quét đối thủ", "callback_data": "RUN_DEFAULT_WORKFLOW"}],
                [{"text": "💡 Tư vấn điều chỉnh giá", "callback_data": "ADVISORY_ACTION"}],
            ]
        }
        await send_telegram_text(help_text, reply_markup=markup, chat_id=chat_id)
        memory.add_assistant_message(chat_id, help_text)
        return

    # --- APPROVE ---
    if intent_res.intent == "APPROVE":
        if hitl_gate.pending_approvals:
            wf_id = next(iter(hitl_gate.pending_approvals))
            hitl_gate.resolve(HITLCallback(workflow_id=wf_id, decision="APPROVE", by="telegram_chat"))
            await send_telegram_text(f"✅ <b>ĐÃ PHÊ DUYỆT!</b>\nĐã xác nhận lưu thay đổi giá cho phiên <code>{wf_id}</code>.", chat_id=chat_id)
        else:
            await send_telegram_text("ℹ️ Hiện tại không có phiên duyệt giá nào đang chờ.", chat_id=chat_id)
        return

    # --- REJECT ---
    if intent_res.intent == "REJECT":
        if hitl_gate.pending_approvals:
            wf_id = next(iter(hitl_gate.pending_approvals))
            hitl_gate.resolve(HITLCallback(workflow_id=wf_id, decision="REJECT", by="telegram_chat"))
            await send_telegram_text(f"❌ <b>ĐÃ HỦY BỎ!</b>\nWorkflow <code>{wf_id}</code> đã được hủy an toàn theo lệnh của Admin.", chat_id=chat_id)
        else:
            await send_telegram_text("ℹ️ Hiện tại không có phiên duyệt nào đang chờ.", chat_id=chat_id)
        return

    # --- RUN_COMPETITOR_SCAN ---
    if intent_res.intent == "RUN_COMPETITOR_SCAN":
        instruction = (
            "Kiểm tra giá và tình trạng tồn kho các gói hosting WordPress (Standard và Business) "
            "trên 2 trang đối thủ (competitor_a, competitor_b). "
            "Nếu đối thủ rẻ hơn >= 10% và còn hàng, đề xuất mức giá mới và yêu cầu duyệt HITL trước khi cập nhật."
        )
        await send_telegram_text(
            f"🚀 <b>ĐÃ KHỞI CHẠY QUY TRÌNH RPA 2.0!</b>\n\n"
            f"📝 <b>Yêu cầu:</b> <i>{html.escape(text)}</i>\n\n"
            f"⚡ <i>Hệ thống sử dụng Firecrawl API đám mây kết hợp Browser Workers để bóc tách bảng giá và vượt vật cản. "
            f"Kết quả đối soát sẽ gửi về đây sau ít giây...</i>",
            chat_id=chat_id,
        )
        asyncio.create_task(_run_workflow_background(instruction, chat_id))
        return

    # --- ANALYZE_URL ---
    if intent_res.intent == "ANALYZE_URL":
        raw_urls = intent_res.entities.get("urls") or _URL_REGEX.findall(text)
        urls = [normalize_target_url(u) for u in raw_urls]
        asyncio.create_task(_handle_url_analysis(urls, chat_id))
        return

    # --- QUERY_COMPETITOR_CACHE ---
    if intent_res.intent == "QUERY_COMPETITOR_CACHE":
        url_rep = memory.get_url_report(chat_id)
        if url_rep:
            chunks = format_analysis_report_telegram(url_rep)
            for c in chunks:
                await send_telegram_text(c, chat_id=chat_id)
            return

        scan = memory.get_scan_result(chat_id) or LATEST_SCAN_RESULT
        if scan and "comparison_table" in scan:
            lines = ["📊 <b>BÁO CÁO GIÁ ĐỐI THỦ (TỪ PHIÊN QUÉT GẦN NHẤT):</b>\n"]
            for row in scan.get("comparison_table", []):
                p_name = row.get("product_name") or row.get("product_code")
                our_p = row.get("our_current_price_vnd", 0)
                lines.append(f"📦 <b>{html.escape(p_name)}</b>")
                lines.append(f"   • Giá niêm yết shop: <code>{our_p:,} VND</code>")
                comps = row.get("competitors", [])
                if not comps:
                    lines.append("   • <i>Không tìm thấy đối thủ trực tiếp</i>")
                for c in comps:
                    src = c.get("source", "Đối thủ")
                    cp = c.get("normalized_price_vnd", 0)
                    st = c.get("stock_status", "")
                    diff = c.get("diff_percentage", 0.0)
                    lines.append(f"   • {html.escape(src)}: <code>{cp:,} VND</code> ({st}, chênh lệch {diff:+.1f}%)")
                lines.append("")
            lines.append("💡 <i>Bạn muốn nhận tư vấn chiến lược giá hay quét lại?</i>")
            markup = {
                "inline_keyboard": [
                    [{"text": "💡 Tư vấn điều chỉnh giá", "callback_data": "ADVISORY_ACTION"}],
                    [{"text": "🔍 Quét lại đối thủ", "callback_data": "RUN_DEFAULT_WORKFLOW"}],
                ]
            }
            await send_telegram_text("\n".join(lines), reply_markup=markup, chat_id=chat_id)
            return

        # No cache available
        await send_telegram_text(
            "ℹ️ <b>Chưa có dữ liệu đối thủ được lưu trong phiên gần nhất!</b>\n\n"
            "👉 Bạn có thể nhắn <b>'Quét giá đối thủ'</b> hoặc bấm nút bên dưới để hệ thống mở Playwright kiểm tra ngay nhé.",
            reply_markup={"inline_keyboard": [[{"text": "🔍 Quét giá đối thủ ngay", "callback_data": "RUN_DEFAULT_WORKFLOW"}]]},
            chat_id=chat_id,
        )
        return

    # --- PRICE_ADVISORY (Tư vấn chỉnh giá thông minh) ---
    if intent_res.intent == "PRICE_ADVISORY":
        target_sku = intent_res.entities.get("sku")
        await send_advisory(chat_id, target_sku=target_sku)
        return

    # --- APPLY_PRICE_CHANGE (Áp dụng giá mới & Live Staging HITL) ---
    if intent_res.intent == "APPLY_PRICE_CHANGE":
        opt_char = intent_res.entities.get("proposed_option")
        options = memory.get_suggested_options(chat_id)
        target_price = intent_res.entities.get("price_vnd")
        target_sku = intent_res.entities.get("sku") or memory.get_active_sku(chat_id) or "HOSTING-WP-STD"

        # Resolve from option choice
        if opt_char and opt_char in options:
            opt_data = options[opt_char]
            target_price = opt_data.get("price_vnd")
            target_sku = opt_data.get("sku", target_sku)

        if not target_price:
            target_price = extract_override_price(text)

        if not target_price:
            await send_telegram_text(
                "Dạ bạn muốn điều chỉnh sản phẩm nào và mức giá mới cụ thể là bao nhiêu ạ?\n"
                "Ví dụ: <i>'Sửa gói Standard thành 180k'</i> hoặc <i>'Áp dụng phương án B'</i>.",
                chat_id=chat_id,
            )
            return

        # Safety Check: Min Price Floor
        floor_limit = settings.min_price_floor_bus if "BUS" in target_sku else settings.min_price_floor_std
        if target_price < floor_limit:
            await send_telegram_text(
                f"⚠️ <b>MỨC GIÁ THẤP HƠN GIÁ SÀN TỐI THIỂU!</b>\n\n"
                f"• Sản phẩm: <code>{target_sku}</code>\n"
                f"• Giá bạn yêu cầu: <code>{target_price:,} VND</code>\n"
                f"• <b>Giá sàn quy định:</b> <code>{floor_limit:,} VND</code>\n\n"
                f"❌ Quy tắc kinh doanh: Shop không được bán dưới giá vốn để bảo vệ dòng tiền. "
                f"Vui lòng chọn mức giá từ <b>{floor_limit:,} VND</b> trở lên!",
                chat_id=chat_id,
            )
            return

        # Case A: Live override if a Playwright session is currently paused at HITL
        if hitl_gate.pending_approvals:
            wf_id = next(iter(hitl_gate.pending_approvals))
            req = hitl_gate.approval_requests.get(wf_id)
            prod_name = req.after_values.get("product_name", target_sku) if req else target_sku
            await send_telegram_text(
                f"⚡ <b>ĐÃ NHẬN LỆNH ĐIỀU CHỈNH GIÁ TỪ ADMIN!</b>\n\n"
                f"• Sản phẩm: <b>{html.escape(prod_name)}</b>\n"
                f"• Giá mới admin chỉ định: <code>{target_price:,} VND</code>\n\n"
                f"⏳ <i>Đang điền giá mới vào trình duyệt Playwright và tiến hành Lưu...</i>",
                chat_id=chat_id,
            )
            hitl_gate.resolve_override(wf_id, override_price=target_price, by="telegram_chat")
            return

        # Case B: Launch Live Staging in Playwright Admin Form & Trigger HITL
        await send_telegram_text(
            f"⚡ <b>ĐANG KHỞI CHẠY LIVE STAGING LÊN TRANG ADMIN!</b>\n\n"
            f"• Sản phẩm: <code>{target_sku}</code>\n"
            f"• Giá mới đề xuất: <code>{target_price:,} VND</code>\n\n"
            f"⚙️ <i>Playwright đang mở Chromium, điền giá vào trang Admin và chuẩn bị ảnh chụp kèm nút Duyệt cho bạn...</i>",
            chat_id=chat_id,
        )

        async def _run_manual_stage(sku: str, price: int, cid: str):
            try:
                await stage_manual_price_change(sku, price)
            except Exception as ex:
                log.error("stage_manual_price_change failed: %s", ex, exc_info=True)
                await send_telegram_text(f"❌ <b>Lỗi khi thao tác trên trang Admin:</b> {html.escape(str(ex))}", chat_id=cid)

        asyncio.create_task(_run_manual_stage(target_sku, target_price, chat_id))
        return

    # --- UNCLEAR / Smart Clarification ---
    clarifying = intent_res.clarifying_question or (
        "Dạ em là trợ lý quản lý bảng giá shop. Bạn có muốn xem bảng giá, quét đối thủ hay cần tư vấn điều chỉnh giá không ạ?"
    )
    markup = {
        "inline_keyboard": [
            [{"text": "📦 Xem bảng giá", "callback_data": "SHOW_CATALOG_ACTION"}, {"text": "🔍 Quét đối thủ", "callback_data": "RUN_DEFAULT_WORKFLOW"}],
            [{"text": "💡 Tư vấn điều chỉnh giá", "callback_data": "ADVISORY_ACTION"}],
        ]
    }
    await send_telegram_text(clarifying, reply_markup=markup, chat_id=chat_id)
    memory.add_assistant_message(chat_id, clarifying)


# Global cache for latest scan result
LATEST_SCAN_RESULT: dict[str, Any] = {}


async def _run_workflow_background(instruction: str, chat_id: str) -> None:
    """Run Orchestrator in background and report back to Telegram."""
    global LATEST_SCAN_RESULT
    orch = Orchestrator(instruction, threshold_pct=10.0)
    try:
        result = await orch.run()
        raw_ext = result.get("extractions", [])
        norm = result.get("normalization", {})
        table = norm.get("comparison_table", [])
        flagged = [r for r in table if r.get("action_required")]

        if not raw_ext:
            await send_telegram_text(
                "⚠️ <b>KHÔNG TRÍCH XUẤT ĐƯỢC DỮ LIỆU ĐỐI THỦ!</b>\n\n"
                "Playwright đã truy cập các website nhưng danh sách trích xuất rỗng. "
                "Vui lòng kiểm tra lại đường dẫn trang hoặc thử lại bằng lệnh: <i>'Quét giá đối thủ'</i>.",
                chat_id=chat_id,
            )
            return

        # Cache successful scan
        LATEST_SCAN_RESULT = norm
        memory.set_scan_result(chat_id, norm)

        summary = norm.get("analysis_summary_vn", "")
        lines = [
            "📊 <b>KẾT QUẢ ĐỐI SOÁT GIÁ RPA 2.0</b>\n",
            f"{html.escape(summary)}\n",
        ]
        for row in table:
            lines.append(f"• <b>{html.escape(row.get('product_name', ''))}</b>:")
            lines.append(f"  Giá shop: <code>{row.get('our_current_price_vnd', 0):,} VND</code>")
            for c in row.get("competitors", []):
                diff_val = c.get("diff_percentage", 0.0)
                diff_str = f"{diff_val:+.1f}%" if diff_val else "0%"
                lines.append(f"  - {html.escape(c.get('source', ''))}: <code>{c.get('normalized_price_vnd', 0):,} VND</code> ({c.get('stock_status', '')}) [Chênh lệch: {diff_str}]")

        if not flagged:
            lines.append("\n✅ <i>Không có đối thủ nào rẻ hơn ngưỡng 10% (hoặc hết hàng). Giá của shop hiện tại vẫn đang rất tối ưu!</i>")
            await send_telegram_text("\n".join(lines), chat_id=chat_id)
        else:
            lines.append(f"\n⚠️ <b>Phát hiện {len(flagged)} sản phẩm có đối thủ rẻ hơn vượt ngưỡng 10%!</b>")
            for f_item in flagged:
                lines.append(f"👉 <b>{html.escape(f_item.get('product_name', ''))}</b>: {html.escape(f_item.get('reason_vn', ''))}")
                if f_item.get("recommended_new_price_vnd"):
                    lines.append(f"   💡 Đề xuất giá cạnh tranh: <code>{f_item.get('recommended_new_price_vnd'):,} VND</code>")

            mutation_res = result.get("mutation")
            if mutation_res and mutation_res.get("decision") in ("APPROVE", "OVERRIDE_APPROVE"):
                lines.append("\n🎉 <i>Đã hoàn tất cập nhật giá lên trang Admin theo lệnh phê duyệt!</i>")
            elif hitl_gate.pending_approvals:
                lines.append("\n🛡️ <i>Phiên phê duyệt HITL đã sẵn sàng. Vui lòng xem ảnh chụp màn hình và bấm nút duyệt bên dưới!</i>")
            else:
                lines.append("\n💡 <i>Mức chênh lệch lớn (>30%). Để bảo vệ an toàn biên lợi nhuận, bạn hãy bấm nút <b>'Tư vấn điều chỉnh giá'</b> bên dưới để AI đưa ra các phương án tối ưu nhất!</i>")

            markup = {
                "inline_keyboard": [
                    [{"text": "💡 Tư vấn điều chỉnh giá", "callback_data": "ADVISORY_ACTION"}],
                    [{"text": "📋 Xem bảng giá shop", "callback_data": "SHOW_CATALOG_ACTION"}],
                ]
            }
            await send_telegram_text("\n".join(lines), reply_markup=markup, chat_id=chat_id)
    except Exception as exc:
        log.exception("Background workflow failed")
        await send_telegram_text(
            f"❌ <b>Lỗi trong quá trình thực thi workflow:</b>\n<code>{html.escape(str(exc))}</code>",
            chat_id=chat_id,
        )


async def _handle_conversational_ai(user_text: str, chat_id: str) -> None:
    """Use configured LLM to generate helpful assistant reply for shop admin."""
    sys_prompt = (
        "Bạn là Trợ lý AI Quản trị Shop thông minh và chu đáo của hệ thống RPA 2.0. "
        "Nhiệm vụ của bạn là hỗ trợ chủ shop kiểm tra đối thủ, điều chỉnh bảng giá và quản lý vận hành. "
        "Trả lời ngắn gọn (2-4 câu), lịch thiệp, thực tế và sẵn sàng nhận lệnh điều chỉnh giá."
    )
    prompt = (
        f"Thông tin bảng giá shop hiện tại:\n{json.dumps(OUR_CATALOG, ensure_ascii=False)}\n\n"
        f"Chủ shop nhắn: '{user_text}'\n"
        f"Hãy trả lời ngắn gọn và hướng dẫn nếu họ muốn quét giá hoặc cập nhật giá."
    )
    try:
        # Simple text reply via LLM
        res = await llm._call_anthropic(
            prompt=prompt,
            schema=None,  # We handle raw text below
            system_instruction=sys_prompt,
            temperature=0.3,
            max_tokens=400,
            attempts=1,
        ) if settings.llm_provider == "anthropic" else None
    except Exception:
        res = None

    if not res:
        # Friendly deterministic fallback
        reply = (
            "Dạ em đã nhận tin nhắn của bạn! Bạn có thể nhắn <b>'Kiểm tra giá đối thủ'</b> để em đi cào dữ liệu, "
            "hoặc gõ <b>/catalog</b> để xem bảng giá dịch vụ hiện tại nhé."
        )
    else:
        reply = str(res)

    await send_telegram_text(reply, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Generic URL Analysis Formatter & Handler
# ---------------------------------------------------------------------------
def format_analysis_report_telegram(report: PriceAnalysisReport) -> list[str]:
    """Format PriceAnalysisReport into concise Vietnamese Telegram HTML chunks (<= 4000 chars)."""
    lines = ["📊 <b>BÁO CÁO PHÂN TÍCH ĐỐI THỦ THEO URL</b>\n"]

    pos_icon = "🟢" if report.our_position == "CHEAPER" else ("🟡" if report.our_position == "PARITY" else "🔴")
    pos_desc = {
        "CHEAPER": "Giá shop đang RẺ HƠN đối thủ",
        "PARITY": "Giá shop đang CÂN BẰNG với thị trường",
        "PRICIER": "Giá shop đang CAO HƠN đối thủ",
    }.get(report.our_position, "CÂN BẰNG")

    lines.append(f"{pos_icon} <b>Vị thế giá của shop:</b> {pos_desc}")
    if report.market_min or report.market_median:
        min_str = f"{report.market_min:,} VND" if report.market_min else "N/A"
        med_str = f"{report.market_median:,} VND" if report.market_median else "N/A"
        lines.append(f"• Thấp nhất: <code>{min_str}</code> | Trung vị: <code>{med_str}</code>\n")

    if report.matches:
        lines.append("🔍 <b>ĐỐI CHIẾU SẢN PHẨM:</b>")
        for idx, m in enumerate(report.matches, 1):
            arrow = "🔻" if m.delta_pct < 0 else ("🔺" if m.delta_pct > 0 else "➖")
            conf_pct = int(m.match_confidence * 100)
            p_val = m.competitor_product.price_vnd
            p_str = f"{p_val:,} VND/{m.competitor_product.billing_cycle}" if p_val else m.competitor_product.raw_price_text
            lines.append(
                f"{idx}. <b>Gói shop:</b> <code>{html.escape(m.our_sku)}</code>\n"
                f"   • Đối thủ: <i>{html.escape(m.competitor_product.name)}</i> ({p_str})\n"
                f"   • Chênh lệch: <code>{m.delta_pct:+.1f}%</code> {arrow} (Độ khớp: {conf_pct}%)\n"
                f"   • {html.escape(m.note)}"
            )
        lines.append("")

    if report.suggestions:
        lines.append("💡 <b>ĐỀ XUẤT CHIẾN LƯỢC:</b>")
        for s in report.suggestions:
            lines.append(f"• {html.escape(s)}")
        lines.append("")

    if report.caveats:
        lines.append("⚠️ <b>LƯU Ý & CẢNH BÁO:</b>")
        for c in report.caveats:
            lines.append(f"• {html.escape(c)}")
        lines.append("")

    if report.failed_urls:
        lines.append("❌ <b>URL KHÔNG THỂ CÀO / LỖI:</b>")
        for u, err in report.failed_urls.items():
            lines.append(f"• <code>{html.escape(u)}</code>: <i>{html.escape(err)}</i>")
        lines.append("")

    lines.append("👉 <i>Bạn có thể gửi link khác để tiếp tục phân tích, hoặc gửi 'Kiểm tra giá đối thủ' để chạy quy trình chuẩn của shop!</i>")

    full_text = "\n".join(lines)
    if len(full_text) <= 3900:
        return [full_text]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > 3800:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = len(line) + 1
        else:
            current_chunk.append(line)
            current_len += len(line) + 1
    if current_chunk:
        chunks.append("\n".join(current_chunk))
    return chunks


async def _handle_url_analysis(urls: list[str], chat_id: str) -> None:
    """Execute parallel URL competitor analysis and deliver report to Telegram."""
    total = len(urls)
    max_urls = settings.max_urls_per_request
    status_msg = f"🔍 <b>ĐANG PHÂN TÍCH {min(total, max_urls)} WEBSITE ĐỐI THỦ...</b>\n\n"
    if total > max_urls:
        status_msg += f"⚠️ <i>Phát hiện {total} URLs. Hệ thống chỉ xử lý tối đa {max_urls} URLs đầu tiên, các URL còn lại sẽ được báo cáo trong danh mục cảnh báo.</i>\n\n"
    status_msg += "⚙️ <i>Đang mở Chromium song song, kiểm tra bảo mật, trích xuất bảng giá và đối soát với danh mục shop...</i>"

    await send_telegram_text(status_msg, chat_id=chat_id)

    try:
        report = await run_url_analysis(urls, chat_id=chat_id)
        memory.set_url_report(chat_id, report)
        chunks = format_analysis_report_telegram(report)
        for chunk in chunks:
            await send_telegram_text(chunk, chat_id=chat_id)
    except Exception as exc:
        log.exception("URL analysis failed")
        await send_telegram_text(
            f"❌ <b>Lỗi trong quá trình phân tích URL:</b>\n<code>{html.escape(str(exc))}</code>",
            chat_id=chat_id,
        )

