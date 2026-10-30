"""Telegram bot integration: HITL approvals & 2-way AI chatbot.

Two modes:
  * webhook  — POST /api/telegram/webhook (set via setWebhook in production)
  * polling  — long-poll loop started at startup when TELEGRAM_ENABLED=1 and
               no webhook is configured (good for local dev / stage demo)

Features:
- Handles button clicks (APPROVE / REJECT)
- Handles natural chat messages (price override, trigger RPA run, catalog queries)
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from app.config import settings
from app.schemas import HITLCallback
from app.services import hitl_gate, telegram_bot

log = logging.getLogger("rpa.telegram")

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


def _parse_callback(data: str) -> HITLCallback | None:
    if not data:
        return None
    if data.startswith("APPROVE_ACTION_"):
        return HITLCallback(workflow_id=data[len("APPROVE_ACTION_") :], decision="APPROVE", by="telegram")
    if data.startswith("REJECT_ACTION_"):
        return HITLCallback(workflow_id=data[len("REJECT_ACTION_") :], decision="REJECT", by="telegram")
    return None


async def _handle_callback(cb: dict) -> None:
    data = cb.get("data", "")
    cb_id = cb.get("id")
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", settings.telegram_chat_id))

    if data == "RUN_DEFAULT_WORKFLOW":
        await telegram_bot.send_telegram_text(
            "🚀 <b>Đang kích hoạt quy trình quét giá đối thủ...</b>\n"
            "Hệ thống đang kết hợp Firecrawl API và Agent Workers để cào dữ liệu nhanh chóng.",
            chat_id=chat_id,
        )
        asyncio.create_task(
            telegram_bot._run_workflow_background(
                "Kiểm tra giá các gói hosting trên 2 trang đối thủ, nếu bên nào rẻ hơn >= 10% thì đề xuất cập nhật giá trên trang Admin.",
                chat_id,
            )
        )
    elif data == "SHOW_CATALOG_ACTION":
        await telegram_bot.send_catalog(chat_id)
    elif data == "ADVISORY_ACTION":
        await telegram_bot.send_advisory(chat_id)
    elif data.startswith("APPLY_OPT_"):
        # Format: APPLY_OPT_B_HOSTING-WP-STD
        parts = data.split("_")
        opt_char = parts[2] if len(parts) > 2 else "B"
        sku = parts[3] if len(parts) > 3 else "HOSTING-WP-STD"
        from app.services.conversation_memory import memory
        options = memory.get_suggested_options(chat_id)
        target_price = options.get(opt_char, {}).get("price_vnd")
        if target_price:
            await telegram_bot.send_telegram_text(
                f"⚡ <b>ĐÃ NHẬN LỆNH ÁP DỤNG PHƯƠNG ÁN {opt_char}!</b>\n\n"
                f"• Sản phẩm: <code>{sku}</code>\n"
                f"• Mức giá: <code>{target_price:,} VND</code>\n\n"
                f"⚙️ <i>Playwright đang mở Chromium, điền giá vào form Admin và chuẩn bị ảnh chụp kèm nút Duyệt...</i>",
                chat_id=chat_id,
            )
            from app.services.orchestrator import stage_manual_price_change
            asyncio.create_task(stage_manual_price_change(sku, target_price))
        else:
            await telegram_bot.send_telegram_text(
                "ℹ️ Phương án này đã hết hạn hiệu lực trong phiên. Bạn vui lòng nhắn lại để em tư vấn mức giá mới nhất nhé!",
                chat_id=chat_id,
            )
    else:
        parsed = _parse_callback(data)
        if parsed:
            resolved = hitl_gate.resolve(parsed)
            if not resolved:
                await telegram_bot.send_telegram_text(
                    f"⚠️ <b>PHIÊN DUYỆT ĐÃ HẾT HẠN HOẶC KHÔNG TỒN TẠI!</b>\n\n"
                    f"• Mã phiên: <code>{parsed.workflow_id}</code>\n"
                    f"• Lý do: Trình duyệt chờ duyệt đã tự động đóng an toàn (do quá thời gian chờ hoặc server vừa khởi động lại).\n\n"
                    f"👉 <b>Cách xử lý:</b> Bạn chỉ cần nhắn tin sửa nhanh:\n"
                    f"• <i>'Sửa standard thành 150k rồi lưu'</i>\n"
                    f"• Hoặc bấm nút bên dưới để nhận tư vấn và kích hoạt phiên mới!",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "💡 Tư vấn điều chỉnh giá", "callback_data": "ADVISORY_ACTION"}],
                            [{"text": "🔍 Quét lại đối thủ", "callback_data": "RUN_DEFAULT_WORKFLOW"}],
                        ]
                    },
                    chat_id=chat_id,
                )
                if cb_id:
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "Phiên duyệt đã hết hạn!"},
                            )
                    except Exception:
                        pass
                return

    # Answer the callback so the button stops "loading" on Telegram client
    if cb_id:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery",
                    json={"callback_query_id": cb_id, "text": "Đã ghi nhận yêu cầu!"},
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("answerCallbackQuery failed: %s", exc)


@router.post("/webhook")
async def telegram_webhook(request: Request):
    """Receive updates from Telegram (setWebhook mode)."""
    try:
        update = await request.json()
    except Exception:
        return {"ok": False}

    cb = update.get("callback_query")
    if cb:
        await _handle_callback(cb)

    msg = update.get("message")
    if msg:
        asyncio.create_task(telegram_bot.handle_telegram_message(msg))

    return {"ok": True}


async def start_polling() -> None:
    """Long-poll loop for Telegram bot. Handles buttons and natural language chat messages."""
    if not settings.telegram_enabled or not settings.telegram_bot_token:
        log.info("Telegram polling disabled (token missing or TELEGRAM_ENABLED=0).")
        return

    import httpx

    offset = 0
    log.info("Telegram bot polling started with 2-way interactive chatbot.")
    async with httpx.AsyncClient(timeout=45) as client:
        while True:
            try:
                r = await client.get(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates",
                    params={"offset": offset, "timeout": 20},
                )
                if r.status_code != 200:
                    log.warning("Telegram getUpdates returned %s: %s", r.status_code, r.text[:200])
                    await asyncio.sleep(4.0)
                    continue

                body = r.json()
                if not body.get("ok"):
                    log.warning("Telegram getUpdates not ok: %s", body)
                    await asyncio.sleep(4.0)
                    continue

                updates = body.get("result", [])
                for upd in updates:
                    offset = upd["update_id"] + 1

                    # 1. Handle Inline Buttons (Callbacks)
                    cb = upd.get("callback_query")
                    if cb:
                        await _handle_callback(cb)

                    # 2. Handle Natural Chat Messages from Admin
                    msg = upd.get("message")
                    if msg:
                        asyncio.create_task(telegram_bot.handle_telegram_message(msg))

                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                log.info("Telegram polling cancelled.")
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(4.0)
