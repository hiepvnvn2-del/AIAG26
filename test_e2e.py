"""End-to-end smoke test against the live server at 127.0.0.1:8000.

Covers: WebSocket stream, /api/run (stubbed LLM), three demo targets, admin HITL,
and the /api/hitl/callback resume path.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000"


async def main() -> int:
    received: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(BASE, wait_until="domcontentloaded")

        # Subscribe to the stream FIRST so we see everything.
        ws = await page.context.new_cdp_session(page) if False else None
        import websockets
        async with websockets.connect("ws://127.0.0.1:8000/ws") as sock:
            await sock.send(json.dumps({"action": "subscribe"}))

            async def reader():
                try:
                    async for raw in sock:
                        received.append(json.loads(raw))
                except Exception:
                    pass

            rtask = asyncio.create_task(reader())

            # Drive the real pipeline via HTTP.
            async with httpx.AsyncClient(timeout=90, base_url=BASE) as client:
                r = await client.post("/api/run", json={"instruction": "test"})
                run_id = r.json()["run_id"]
                print(f"run_id={run_id}")

                # Let workers run, then approve anything that parks.
                for _ in range(40):
                    await asyncio.sleep(1.0)
                    pend = await client.get("/api/pending")
                    wfids = [x["workflow_id"] for x in (pend.json() or [])]
                    for wf in wfids:
                        print(f"  approving {wf}")
                        await client.post(
                            "/api/hitl/callback",
                            json={"workflow_id": wf, "decision": "APPROVE", "by": "test"},
                        )
                    done = await client.get(f"/api/run/{run_id}")
                    st = done.json().get("status")
                    if st in ("completed", "failed"):
                        print(f"  final status: {st}")
                        break

            await asyncio.sleep(1.5)
            rtask.cancel()

        await browser.close()

    kinds: dict[str, int] = {}
    for e in received:
        kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
    print("\n=== WebSocket events received ===")
    for k, v in sorted(kinds.items()):
        print(f"  {k}: {v}")

    hitl = [e for e in received if e.get("kind") == "hitl_request"]
    resolved = [e for e in received if e.get("kind") == "hitl_resolved"]
    print(f"\n  hitl_request={len(hitl)}  hitl_resolved={len(resolved)}")
    if hitl:
        payload = hitl[0]["payload"]
        print(f"  risk={payload.get('risk_level')}  staged={payload.get('staged_action','')[:60]}")
        print(f"  before={payload.get('before')}  after={payload.get('after')}")

    ok = len(hitl) > 0 and len(resolved) > 0
    print("\nE2E:", "PASS — HITL pause/resume observed live" if ok else "INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
