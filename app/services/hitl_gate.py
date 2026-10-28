"""HITL Gatekeeper: freezes a Playwright worker session on an asyncio.Event until human approval.

The worker parks inside its Playwright context (browser stays open, form stays filled),
so approving means immediately resuming to submit.
Two channels can unblock it:
  * Telegram inline buttons (callback_query) or chat message override
  * The dashboard "Approve" button, which posts to /api/hitl/callback
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
from typing import Any

from app.config import settings
from app.schemas import ApprovalRequest, HITLCallback
from app.services.event_bus import bus

log = logging.getLogger("rpa.hitl")

# workflow_id -> asyncio.Event
pending_approvals: dict[str, asyncio.Event] = {}
# workflow_id -> the request being staged
approval_requests: dict[str, ApprovalRequest] = {}
# workflow_id -> human decision once resolved (str or dict for override)
decisions: dict[str, Any] = {}

_APPROVE_TIMEOUT_S = 900.0


def _risk_for(delta_pct: float | None) -> str:
    if delta_pct is None:
        return "HIGH"
    if abs(delta_pct) >= 30:
        return "CRITICAL"
    if abs(delta_pct) >= 10:
        return "HIGH"
    return "MEDIUM"


def build_message(req: ApprovalRequest) -> str:
    """Vietnamese Telegram HTML message. HTML-escaped for Telegram markup parser."""
    def e(value) -> str:
        return html.escape("" if value is None else str(value))

    delta = None
    old = req.before_values.get("price_vnd")
    new = req.after_values.get("price_vnd")
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) and old:
        delta = round((new - old) / old * 100, 1)

    lines = [
        f"<b>⚠️ {e(req.risk_level)} — YÊU CẦU PHÊ DUYỆT (HITL GATE)</b>",
        "",
        f"<b>Workflow ID:</b> <code>{e(req.workflow_id)}</code>",
        f"<b>Hệ thống đích:</b> <code>{e(req.after_values.get('target_system', 'Internal Shop Admin Portal'))}</code>",
        f"<b>Sản phẩm:</b> <code>{e(req.after_values.get('product_code', '-'))}</code> "
        f"{e(req.after_values.get('product_name', ''))}",
        "",
        "<b>Hành động đang chờ kích hoạt:</b>",
        f"<code>{e(req.staged_action)}</code>",
        "",
        "<b>Biến động giá:</b>",
        f"• Giá hiện tại: <code>{old:,} VND</code>" if isinstance(old, (int, float)) else f"• Giá hiện tại: <code>{e(old)}</code>",
        f"• Giá đề xuất: <code>{new:,} VND</code>" if isinstance(new, (int, float)) else f"• Giá đề xuất: <code>{e(new)}</code>",
    ]
    if delta is not None:
        arrow = "🔻" if delta < 0 else ("🔺" if delta > 0 else "➖")
        lines.append(f"• Chênh lệch: <code>{delta:+.1f}%</code> {arrow}")
    lines += [
        "",
        f"<b>Đánh giá tác động:</b> {e(req.impact_vn)}",
        "",
        "<i>⚡ Phiên Playwright đang mở sẵn. Chưa lưu dữ liệu cho đến khi bạn xác nhận.</i>",
        "",
        "💬 <i>Mẹo sửa nhanh: Nhắn tin <code>Sửa thành 180k</code> hoặc <code>Đặt giá 190.000</code> để đổi giá và lưu ngay!</i>",
    ]
    return "\n".join(lines)


async def send_telegram(req: ApprovalRequest) -> bool:
    """Post the approval card with inline buttons. Sends photo screenshot if available."""
    if not settings.telegram_enabled or not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False
    try:
        import httpx

        msg_text = build_message(req)
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Duyệt & Click Lưu ngay", "callback_data": f"APPROVE_ACTION_{req.workflow_id}"},
                    {"text": "❌ Hủy bỏ phiên", "callback_data": f"REJECT_ACTION_{req.workflow_id}"},
                ]
            ]
        }

        async with httpx.AsyncClient(timeout=20) as client:
            # If screenshot is staged, send as photo with caption
            if req.screenshot_b64:
                try:
                    photo_bytes = base64.b64decode(req.screenshot_b64)
                    photo_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto"
                    res = await client.post(
                        photo_url,
                        data={
                            "chat_id": settings.telegram_chat_id,
                            "caption": msg_text[:1024],
                            "parse_mode": "HTML",
                            "reply_markup": json.dumps(reply_markup),
                        },
                        files={"photo": ("staged_form.jpg", photo_bytes, "image/jpeg")},
                    )
                    if res.status_code == 200:
                        return True
                except Exception as ex:
                    log.warning("Telegram sendPhoto failed, falling back to sendMessage: %s", ex)

            # Fallback to sendMessage
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            body = {
                "chat_id": settings.telegram_chat_id,
                "text": msg_text,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            }
            r = await client.post(url, json=body)
            return r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram send failed: %s", exc)
        return False


async def request_approval(req: ApprovalRequest) -> Any:
    """Park the caller until a human decides. Returns 'APPROVE', 'REJECT' or dict with override info."""
    event = asyncio.Event()
    pending_approvals[req.workflow_id] = event
    approval_requests[req.workflow_id] = req

    try:
        message_html = build_message(req)
    except Exception as exc:  # noqa: BLE001
        log.warning("build_message failed for %s: %s", req.workflow_id, exc)
        message_html = (
            f"<b>⚠️ {html.escape(req.risk_level)} — YÊU CẦU PHÊ DUYỆT</b>\n"
            f"<code>{html.escape(req.staged_action)}</code>"
        )

    await bus.publish(
        "hitl_request",
        {
            "workflow_id": req.workflow_id,
            "risk_level": req.risk_level,
            "staged_action": req.staged_action,
            "before": req.before_values,
            "after": req.after_values,
            "impact_vn": req.impact_vn,
            "message_html": message_html,
            "screenshot_b64": req.screenshot_b64,
        },
        workflow_id=req.workflow_id,
    )

    delivered = await send_telegram(req)
    if delivered:
        log.info("Approval card sent to Telegram for %s", req.workflow_id)
    else:
        log.info("Telegram not active/unconfigured — approval waiting on dashboard UI.")

    try:
        await asyncio.wait_for(event.wait(), timeout=_APPROVE_TIMEOUT_S)
        decision = decisions.get(req.workflow_id, "REJECT")
    except asyncio.TimeoutError:
        decision = "REJECT"
        log.warning("Approval timed out for %s — defaulting to REJECT", req.workflow_id)
    finally:
        pending_approvals.pop(req.workflow_id, None)

    decision_name = decision if isinstance(decision, str) else decision.get("decision", "APPROVE")

    # Feedback Learner: Log decision for empirical intelligence & insight distillation
    try:
        from app.services.feedback_learner import log_hitl_decision
        log_hitl_decision(req, decision)
    except Exception as exc:
        log.warning("Could not record decision in feedback learner: %s", exc)

    await bus.publish(
        "hitl_resolved",
        {"workflow_id": req.workflow_id, "decision": decision_name, "raw": decision},
        workflow_id=req.workflow_id,
    )
    return decision


def resolve(cb: HITLCallback) -> bool:
    """Called from the Telegram poller or the dashboard callback route."""
    ev = pending_approvals.get(cb.workflow_id)
    if ev is None:
        log.warning("resolve() for unknown/expired workflow %s", cb.workflow_id)
        return False
    decisions[cb.workflow_id] = cb.decision
    ev.set()
    return True


def resolve_override(workflow_id: str, override_price: int, by: str = "telegram") -> bool:
    """Allow human to adjust proposed price via Telegram chat, then approve."""
    ev = pending_approvals.get(workflow_id)
    if ev is None:
        log.warning("resolve_override() for unknown/expired workflow %s", workflow_id)
        return False
    decisions[workflow_id] = {
        "decision": "OVERRIDE",
        "override_price": override_price,
        "by": by,
    }
    ev.set()
    return True


def _risk_from_values(before: dict, after: dict) -> str:
    old, new = before.get("price_vnd"), after.get("price_vnd")
    if not isinstance(old, (int, float)) or not old or not isinstance(new, (int, float)):
        return "HIGH"
    return _risk_for((new - old) / old * 100)