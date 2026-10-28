"""Run endpoints: WebSocket stream, workflow kickoff, HITL callback."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.schemas import AnalyzeURLRequest, HITLCallback
from app.services.event_bus import bus
from app.services.hitl_gate import resolve
from app.services.orchestrator import Orchestrator, run_url_analysis

log = logging.getLogger("rpa.api")
router = APIRouter()

# Keep a handle on in-flight runs so they are not garbage collected mid-flight.
_running: dict[str, asyncio.Task] = {}


class RunRequest(BaseModel):
    instruction: str
    threshold_pct: float = 10.0


@router.websocket("/ws")
async def ws_stream(ws: WebSocket):
    await ws.accept()
    log.info("Dashboard WebSocket connected.")
    try:
        async for evt in bus.subscribe():
            await ws.send_json(evt)
    except WebSocketDisconnect:
        log.info("Dashboard WebSocket disconnected.")
    except Exception as exc:  # noqa: BLE001
        log.warning("WebSocket error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass


@router.post("/api/run")
async def start_run(req: RunRequest, background: BackgroundTasks):
    orch = Orchestrator(req.instruction, threshold_pct=req.threshold_pct)

    async def _task():
        try:
            await orch.run()
        except Exception as exc:  # noqa: BLE001
            log.exception("Workflow failed")
            await bus.publish("workflow_error", {"error": str(exc)}, workflow_id=orch.workflow_id)

    task = asyncio.create_task(_task())
    _running[orch.workflow_id] = task
    task.add_done_callback(lambda _t: _running.pop(orch.workflow_id, None))
    return {"workflow_id": orch.workflow_id, "status": "started"}


@router.post("/api/hitl/callback")
async def hitl_callback(cb: HITLCallback):
    ok = resolve(cb)
    return {"ok": ok, "workflow_id": cb.workflow_id, "decision": cb.decision}


@router.get("/api/pending")
async def pending():
    from app.services.hitl_gate import approval_requests

    return {k: v.model_dump(mode="json") for k, v in approval_requests.items()}


@router.post("/api/analyze")
async def analyze_urls(req: AnalyzeURLRequest):
    """Analyze arbitrary competitor URLs and return a strategic price analysis report."""
    report = await run_url_analysis(req.urls)
    return report.model_dump(mode="json")


@router.get("/api/catalog")
async def get_shop_catalog():
    """Returns current live shop catalog with latest synced prices."""
    from app.services.orchestrator import OUR_CATALOG
    return OUR_CATALOG


class UpdateCatalogPriceRequest(BaseModel):
    product_code: str
    price_vnd: int


@router.post("/api/catalog/update")
async def update_shop_catalog_price(req: UpdateCatalogPriceRequest):
    """Directly updates a product price in the shop catalog."""
    from app.services.orchestrator import update_catalog_price
    update_catalog_price(req.product_code, req.price_vnd)
    return {"ok": True, "product_code": req.product_code, "price_vnd": req.price_vnd}


