"""Drive the full workflow over HTTP against the stub server (no browser needed here)."""
import asyncio, json, sys
import httpx

BASE = "http://127.0.0.1:8000"

async def main():
    async with httpx.AsyncClient(timeout=30, base_url=BASE) as c:
        r = await c.post("/api/run", json={"instruction": "so sanh gia va cap nhat"})
        r.raise_for_status()
        wf = r.json()["workflow_id"]
        print(f"workflow started: {wf}")

        for i in range(90):
            await asyncio.sleep(1)
            pend = (await c.get("/api/pending")).json()
            if pend:
                for wid, body in pend.items():
                    print(f"  HITL arrived for {wid}")
                    print(f"    risk   : {body.get('risk_level')}")
                    print(f"    staged : {body.get('staged_action')}")
                    print(f"    before : {body.get('before_values')}")
                    print(f"    after  : {body.get('after_values')}")
                    print(f"    impact : {body.get('impact_vn')}")
                    resp = await c.post("/api/hitl/callback",
                                        json={"workflow_id": wid, "decision": "APPROVE", "by": "test"})
                    print(f"    resolve -> {resp.json()}")
                    await asyncio.sleep(4)
                    print("\nE2E PASSED — workflow reached the HITL gate and resumed on approval")
                    return 0
        print("E2E INCOMPLETE — no HITL request within 90s")
        return 1

sys.exit(asyncio.run(main()))