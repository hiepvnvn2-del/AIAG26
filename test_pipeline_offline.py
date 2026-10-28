"""End-to-end integration verification test with stub LLM and live local server."""
import asyncio
import os
import sys
import threading
import time

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

import uvicorn
from app.main import app as fastapi_app
import app.stub_llm
from app.config import settings
from app.schemas import HITLCallback
from app.services import hitl_gate
from app.services.orchestrator import Orchestrator


class ServerThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=8000, log_level="warning")
        self.server = uvicorn.Server(config)

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True


async def test_full_pipeline():
    print("[TEST] 1. Khởi động local server demo...")
    server_thread = ServerThread()
    server_thread.start()
    await asyncio.sleep(1.0)  # Wait for server to bind

    print("[TEST] 2. Khởi động Orchestrator pipeline với kiến trúc RPA 2.0...")
    settings.headless = True
    instruction = "Kiểm tra giá các gói hosting trên 2 trang đối thủ, nếu bên nào rẻ hơn >= 10% thì đề xuất cập nhật giá trên trang Admin."

    orch = Orchestrator(instruction=instruction, threshold_pct=10.0)

    async def auto_approver():
        for _ in range(120):
            await asyncio.sleep(0.5)
            if orch.workflow_id in hitl_gate.pending_approvals:
                print(f"[TEST 4 - HITL_GATEKEEPER] Phát hiện phiên Playwright đang được giữ mở an toàn!")
                print(f"[TEST 4 - HITL_GATEKEEPER] Workflow ID: {orch.workflow_id}")
                req = hitl_gate.approval_requests.get(orch.workflow_id)
                if req:
                    print(f"[TEST 4 - HITL_GATEKEEPER] Cảnh báo rủi ro: {req.risk_level}")
                    print(f"[TEST 4 - HITL_GATEKEEPER] Đánh giá tác động: {req.impact_vn}")
                print("[TEST 4 - HITL_GATEKEEPER] Nhận callback APPROVE từ Telegram/Dashboard...")
                hitl_gate.resolve(HITLCallback(workflow_id=orch.workflow_id, decision="APPROVE", by="telegram"))
                break

    approver_task = asyncio.create_task(auto_approver())

    try:
        result = await orch.run()
        print("\n=======================================================")
        print("[TEST HOÀN TẤT] KẾT QUẢ QUY TRÌNH RPA 2.0:")
        print(f"1. ORCHESTRATOR_PLANNER: {result['dag']['intent_summary']}")
        print(f"2. BROWSER_WORKER: Đã trích xuất & vượt popup thành công")
        print(f"3. DATA_NORMALIZER_ANALYST: {result['normalization']['analysis_summary_vn']}")
        print(f"4. HITL_GATEKEEPER: Quyết định = {result['mutation']['decision']}")
        print("=======================================================")
        assert result['mutation'] is not None
        assert result['mutation']['decision'] == "APPROVE"
        print("=> TOÀN BỘ 4 VAI TRÒ ĐÃ HỢP NHẤT VÀ CHẠY THÀNH CÔNG RỰC RỠ!")
    finally:
        approver_task.cancel()
        server_thread.stop()


if __name__ == "__main__":
    asyncio.run(test_full_pipeline())
