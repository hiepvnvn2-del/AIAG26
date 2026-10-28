"""Test the HITL gate: worker must park until a human decision arrives."""
import asyncio
from app.schemas import ApprovalRequest, HITLCallback
from app.services import hitl_gate

async def test_approve_unblocks():
    req = ApprovalRequest(
        workflow_id="wf_test_approve",
        risk_level="HIGH",
        before_values={"price_vnd": 250000, "product_code": "HOSTING-WP-STD", "product_name": "Gói Std"},
        after_values={"price_vnd": 195020, "product_code": "HOSTING-WP-STD", "product_name": "Gói Std", "target_system": "Admin"},
        impact_vn="Giảm 22.0%",
        staged_action="Admin -> sửa giá -> dừng trước nút Lưu",
    )
    task = asyncio.create_task(hitl_gate.request_approval(req))
    await asyncio.sleep(0.3)
    assert "wf_test_approve" in hitl_gate.pending_approvals, "gate must be registered while parked"
    assert not task.done(), "must still be parked before the human answers"

    ok = hitl_gate.resolve(HITLCallback(workflow_id="wf_test_approve", decision="APPROVE", by="test"))
    assert ok, "resolve() should find the pending gate"
    decision = await asyncio.wait_for(task, timeout=3)
    assert decision == "APPROVE", f"expected APPROVE, got {decision}"
    assert "wf_test_approve" not in hitl_gate.pending_approvals, "gate must be cleaned up"
    print("  OK  approve path unblocks and cleans up")

async def test_reject_path():
    req = ApprovalRequest(
        workflow_id="wf_test_reject", risk_level="MEDIUM",
        before_values={"price_vnd": 100}, after_values={"price_vnd": 90},
        impact_vn="test", staged_action="test",
    )
    task = asyncio.create_task(hitl_gate.request_approval(req))
    await asyncio.sleep(0.2)
    hitl_gate.resolve(HITLCallback(workflow_id="wf_test_reject", decision="REJECT", by="test"))
    assert await asyncio.wait_for(task, timeout=3) == "REJECT"
    print("  OK  reject path")

async def test_unknown_workflow():
    ok = hitl_gate.resolve(HITLCallback(workflow_id="nope", decision="APPROVE"))
    assert ok is False, "resolving an unknown workflow must return False, not raise"
    print("  OK  unknown workflow handled gracefully")

async def test_message_builds():
    req = ApprovalRequest(
        workflow_id="wf_msg", risk_level="CRITICAL",
        before_values={"price_vnd": 250000}, after_values={"price_vnd": 100000, "target_system": "Admin", "product_code": "X"},
        impact_vn="Giảm 60%", staged_action="stage",
    )
    msg = hitl_gate.build_message(req)
    assert "250000" in msg and "100000" in msg, "message must show before/after"
    assert "<b>" in msg and "<code>" in msg, "must use Telegram HTML"
    assert "-60.0%" in msg, f"delta missing: {msg}"
    print("  OK  message renders before/after + delta")

async def main():
    print("=== HITL gate ===")
    await test_approve_unblocks()
    await test_reject_path()
    await test_unknown_workflow()
    await test_message_builds()
    print("\nHITL GATE TESTS PASSED")

asyncio.run(main())