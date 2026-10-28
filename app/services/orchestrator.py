"""Master Orchestrator.

Pipeline:
  1. Planner LLM (active_role=ORCHESTRATOR_PLANNER) turns a natural-language command
     into an executable WorkflowDAG (parallel worker tasks + business rules + mutation task).
  2. Each parallel worker task runs in its own BrowserWorker (active_role=BROWSER_WORKER)
     via asyncio.gather in isolated Playwright contexts.
  3. Raw extractions + our internal catalog are processed by the Normalizer Agent
     (active_role=DATA_NORMALIZER_ANALYST).
  4. If any row requires action, an admin updater stages the price in Chromium,
     freezes the session, and triggers HITL Gatekeeper (active_role=HITL_GATEKEEPER)
     waiting on Telegram/Dashboard approval before clicking Submit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from typing import Optional

from app.config import settings
from app.schemas import (
    ApprovalRequest,
    CompetitorProduct,
    PriceAnalysisReport,
    WorkerTask,
    WorkflowDAG,
)
from app.services.browser_worker import BrowserWorker
from app.services.event_bus import bus
from app.services.firecrawl_scraper import scrape_with_firecrawl
from app.services.hitl_gate import _risk_from_values, request_approval
from app.services.llm_rotator import llm
from app.services.normalizer import run_normalizer, run_price_analyst
from app.services.url_guard import validate_public_url

log = logging.getLogger("rpa.orchestrator")

# Our internal catalog — baseline for price comparison
OUR_CATALOG_BASELINE = [
    {
        "product_code": "HOSTING-WP-STD",
        "product_name": "Gói Hosting WordPress Standard",
        "our_current_price_vnd": 250000,
        "floor_price_vnd": settings.min_price_floor_std,
        "category": "Websites",
        "specs": {"ram": "2GB", "ssd": "20GB", "websites": 1, "bandwidth": "unlimited"},
    },
    {
        "product_code": "HOSTING-WP-BUS",
        "product_name": "Gói Hosting WordPress Business",
        "our_current_price_vnd": 590000,
        "floor_price_vnd": settings.min_price_floor_bus,
        "category": "Websites",
        "specs": {"ram": "4GB", "ssd": "50GB", "websites": 5, "bandwidth": "unlimited"},
    },
]

from app.config import ROOT_DIR

CATALOG_FILE = ROOT_DIR / "data" / "catalog.json"


def _load_catalog() -> list[dict]:
    if CATALOG_FILE.exists():
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, list) and len(saved) > 0:
                    return saved
        except Exception as ex:
            log.warning("Could not read catalog.json: %s", ex)
    return [dict(it) for it in OUR_CATALOG_BASELINE]


OUR_CATALOG = _load_catalog()


def update_catalog_price(product_code: str, new_price: int) -> None:
    """Updates product price in memory and persists to data/catalog.json."""
    updated = False
    for item in OUR_CATALOG:
        if item.get("product_code") == product_code:
            item["our_current_price_vnd"] = int(new_price)
            updated = True

    if updated:
        try:
            CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CATALOG_FILE, "w", encoding="utf-8") as f:
                json.dump(OUR_CATALOG, f, ensure_ascii=False, indent=2)
            log.info("Catalog price updated & persisted: %s -> %s VND", product_code, new_price)
        except Exception as ex:
            log.error("Failed to persist catalog.json: %s", ex)



class Orchestrator:
    def __init__(self, instruction: str, threshold_pct: float = 10.0):
        self.instruction = instruction
        self.threshold_pct = threshold_pct
        self.workflow_id = f"wf_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.dag: WorkflowDAG | None = None

    # ------------------------------------------------------------- planning
    async def plan(self) -> WorkflowDAG:
        prompt = (
            f"User business instruction:\n{self.instruction}\n\n"
            f"Our internal catalog:\n{json.dumps(OUR_CATALOG, ensure_ascii=False, indent=2)}\n\n"
            f"Available target websites/endpoints:\n"
            f"{json.dumps(settings.demo_urls, ensure_ascii=False, indent=2)}\n\n"
            f"Decompose this instruction into a WorkflowDAG JSON. Ensure parallel_worker_tasks "
            f"splits competitor extraction into separate tasks, and mutation_action_task is enabled "
            f"with requires_hitl_approval=true."
        )
        raw = await llm.generate_role_json(
            active_role="ORCHESTRATOR_PLANNER",
            prompt=prompt,
            schema=WorkflowDAG,
        )
        dag = WorkflowDAG.model_validate(raw)
        dag.workflow_id = self.workflow_id
        self.dag = dag
        await bus.publish(
            "plan_ready",
            dag.model_dump(mode="json"),
            workflow_id=self.workflow_id,
        )
        return dag

    # ------------------------------------------------------- parallel phase
    async def _run_worker(self, task: WorkerTask) -> list[dict]:
        worker = BrowserWorker(
            worker_id=task.worker_id,
            target_name=task.target_name,
            start_url=task.start_url,
            goal=task.goal,
            target_fields=task.target_fields,
            workflow_id=self.workflow_id,
            park_on_hitl=False,
        )
        try:
            data = await worker.run()
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc).strip() or repr(exc)
            log.error("[%s] Worker crashed: %s", task.worker_id, err_msg, exc_info=True)
            await bus.publish(
                "worker_error",
                {"worker_id": task.worker_id, "error": err_msg},
                workflow_id=self.workflow_id,
            )
            return []

        for row in data:
            row.setdefault("source", task.target_name)
        return data

    async def run_parallel(self, tasks: list[WorkerTask]) -> list[dict]:
        results = await asyncio.gather(*(self._run_worker(t) for t in tasks))
        merged = [row for batch in results for row in batch]
        await bus.publish(
            "extraction_done",
            {"total_rows": len(merged)},
            workflow_id=self.workflow_id,
        )
        return merged

    # ------------------------------------------------------- mutation phase
    async def stage_admin_update(self, admin_url: str, updates: list[dict]) -> dict:
        """Stage price changes in Admin UI, freeze session with Playwright open, wait for HITL."""
        first = updates[0] if updates else {}
        target_code = first.get("product_code", "HOSTING-WP-STD")
        new_price = first.get("recommended_new_price_vnd", 200000)

        worker = BrowserWorker(
            worker_id="admin_updater",
            target_name="Internal Shop Admin",
            start_url=admin_url,
            goal=(
                f"Mở trang quản trị, tìm ô nhập giá (input number) của sản phẩm {target_code}, "
                f"điền giá mới là {new_price}, nhưng KHÔNG bấm nút Lưu thay đổi. "
                f"Dừng lại ngay trước nút Lưu và trả về action_type REQUEST_HITL_APPROVAL."
            ),
            target_fields=["product_code", "price_vnd"],
            workflow_id=self.workflow_id,
            park_on_hitl=True,
        )

        shot = ""
        decision = "REJECT"
        try:
            # Let the ReAct loop stage the form and park
            await worker.run()

            # Ensure target input has the new price filled in so screenshot & form state are 100% accurate
            if worker.page:
                idx_map = {
                    "HOSTING-WP-STD": "price-0",
                    "HOSTING-WP-PRO": "price-1",
                    "HOSTING-WP-BUS": "price-2",
                }
                inp_id = idx_map.get(target_code, "price-0")
                try:
                    await worker.page.fill(f"#{inp_id}", str(new_price))
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
                shot = await worker._screenshot()

            before = {
                "price_vnd": first.get("our_current_price_vnd"),
                "product_code": target_code,
                "product_name": first.get("product_name"),
            }
            after = {
                "price_vnd": new_price,
                "product_code": target_code,
                "product_name": first.get("product_name"),
                "target_system": "Internal Shop Admin Portal",
            }
            delta = None
            if before["price_vnd"] and after["price_vnd"]:
                delta = (after["price_vnd"] - before["price_vnd"]) / before["price_vnd"] * 100

            req = ApprovalRequest(
                workflow_id=self.workflow_id,
                risk_level=_risk_from_values(before, after),
                before_values=before,
                after_values=after,
                impact_vn=(
                    f"Giá {first.get('product_name')} điều chỉnh {delta:+.1f}% "
                    f"({before['price_vnd']:,} VND → {after['price_vnd']:,} VND) "
                    f"để cạnh tranh với đối thủ."
                    if delta is not None
                    else "Thay đổi dữ liệu nhạy cảm trên hệ thống quản trị."
                ),
                staged_action=f"Cập nhật giá {target_code} thành {new_price:,} VND trên trang Admin -> Chờ duyệt Submit",
                screenshot_b64=shot,
            )

            # Park on asyncio.Event until human approves via Telegram or Dashboard
            decision_res = await request_approval(req)

            if decision_res == "APPROVE":
                log.info("[%s] HITL Approved! Submitting live form via Playwright...", self.workflow_id)
                commit_info = await worker.commit_submit(
                    target_button_text="Lưu thay đổi",
                    product_code=target_code,
                    new_price=new_price,
                )
                
                # Update persistent catalog so /catalog immediately reflects the new price
                update_catalog_price(target_code, new_price)

                await bus.publish(
                    "mutation_executed",
                    {"updates": updates, "screenshot_b64": commit_info.get("screenshot_b64")},
                    workflow_id=self.workflow_id,
                )
                try:
                    from app.services.telegram_bot import send_telegram_photo, send_telegram_text
                    import base64
                    confirm_text = (
                        f"✅ <b>ĐÃ PHÊ DUYỆT & LƯU THÀNH CÔNG!</b>\n\n"
                        f"• Sản phẩm: <b>{first.get('product_name', target_code)}</b>\n"
                        f"• Mã SKU: <code>{target_code}</code>\n"
                        f"• Giá niêm yết mới: <code>{new_price:,} VND/tháng</code>\n\n"
                        f"🎉 <i>Playwright đã bấm 'Lưu thay đổi' trên trang Admin. Bảng giá shop (/catalog) đã cập nhật giá mới ngay lập tức!</i>"
                    )
                    b64_img = commit_info.get("screenshot_b64")
                    if b64_img:
                        await send_telegram_photo(caption=confirm_text, photo_bytes=base64.b64decode(b64_img))
                    else:
                        await send_telegram_text(confirm_text)
                except Exception as ex:
                    log.warning("Telegram approve confirmation failed: %s", ex)
                decision = "APPROVE"
            elif isinstance(decision_res, dict) and decision_res.get("decision") == "OVERRIDE":
                override_price = decision_res.get("override_price", new_price)
                log.info("[%s] Admin override price to %s VND! Submitting...", self.workflow_id, override_price)
                commit_info = await worker.override_and_submit(new_price=override_price, product_code=target_code)
                
                # Update persistent catalog
                update_catalog_price(target_code, override_price)

                updates[0]["recommended_new_price_vnd"] = override_price
                updates[0]["admin_override_price"] = override_price
                await bus.publish(
                    "mutation_executed",
                    {"updates": updates, "screenshot_b64": commit_info.get("screenshot_b64"), "override_price": override_price},
                    workflow_id=self.workflow_id,
                )
                try:
                    from app.services.telegram_bot import send_telegram_photo, send_telegram_text
                    import base64
                    confirm_text = (
                        f"✅ <b>ĐÃ CẬP NHẬT GIÁ THEO LỆNH ADMIN!</b>\n\n"
                        f"• Sản phẩm: <b>{first.get('product_name', target_code)}</b>\n"
                        f"• Giá mới áp dụng: <code>{override_price:,} VND</code>\n"
                        f"• Trạng thái: <b>Đã lưu thành công lên trang Admin và đồng bộ bảng giá shop (/catalog)!</b>"
                    )
                    b64_img = commit_info.get("screenshot_b64")
                    if b64_img:
                        await send_telegram_photo(caption=confirm_text, photo_bytes=base64.b64decode(b64_img))
                    else:
                        await send_telegram_text(confirm_text)
                except Exception as ex:
                    log.warning("Telegram override confirmation failed: %s", ex)
                decision = "OVERRIDE_APPROVE"
            else:
                log.info("[%s] HITL Rejected! Aborting mutation.", self.workflow_id)
                await bus.publish(
                    "mutation_rejected",
                    {"updates": updates},
                    workflow_id=self.workflow_id,
                )
                decision = "REJECT"

        finally:
            await worker.stop()

        return {"decision": decision, "updates": updates}

    # ----------------------------------------------------------------- run
    async def run(self) -> dict:
        # Step 1: Orchestrator Planner builds DAG
        dag = await self.plan()

        # Step 2: Parallel Browser Workers
        raw = await self.run_parallel(dag.parallel_worker_tasks)

        # Step 3: Data Normalizer & Business Rules
        norm = await run_normalizer(
            our_catalog=OUR_CATALOG,
            raw_extractions=raw,
            threshold_pct=self.threshold_pct,
            pricing_formula=dag.business_rule_evaluation.pricing_formula,
        )
        await bus.publish(
            "normalized",
            norm.model_dump(mode="json"),
            workflow_id=self.workflow_id,
        )

        flagged = [r for r in norm.comparison_table if r.action_required]
        mutation = None

        # Step 4: Sensitive Mutation & HITL Gatekeeper
        if flagged and dag.mutation_action_task.enabled:
            over_cap = [
                r
                for r in flagged
                if abs(
                    (r.recommended_new_price_vnd - r.our_current_price_vnd)
                    / r.our_current_price_vnd
                    * 100
                )
                > settings.max_price_delta_pct
            ]
            if over_cap:
                log.warning(
                    "%d item(s) exceed the %.0f%% cap — skipping mutation.",
                    len(over_cap),
                    settings.max_price_delta_pct,
                )
            else:
                updates = [
                    {
                        "product_code": r.product_code,
                        "product_name": r.product_name,
                        "our_current_price_vnd": r.our_current_price_vnd,
                        "recommended_new_price_vnd": r.recommended_new_price_vnd,
                        "reason_vn": r.reason_vn,
                    }
                    for r in flagged
                ]
                mutation = await self.stage_admin_update(
                    settings.demo_urls["admin"], updates
                )

        await bus.publish(
            "workflow_done",
            {
                "flagged": len(flagged),
                "mutation": mutation,
            },
            workflow_id=self.workflow_id,
        )
        return {
            "workflow_id": self.workflow_id,
            "dag": dag.model_dump(mode="json"),
            "extractions": raw,
            "normalization": norm.model_dump(mode="json"),
            "mutation": mutation,
        }


# ---------------------------------------------------------------------------
# Generic URL Competitor Analysis Pipeline
# ---------------------------------------------------------------------------
_ANALYSIS_CACHE: dict[tuple[str, ...], tuple[float, PriceAnalysisReport]] = {}


async def run_url_analysis(urls: list[str], chat_id: Optional[str] = None) -> PriceAnalysisReport:
    """Analyze arbitrary competitor URLs in parallel:
    - Enforces max URLs per request
    - Validates public IPs & robots.txt
    - Scrapes via JSON-LD or ReAct loop
    - Produces strategic PriceAnalysisReport
    - Caches results with configurable TTL
    """
    clean_urls = [u.strip() for u in urls if u and isinstance(u, str) and u.strip()]
    if not clean_urls:
        return PriceAnalysisReport(
            matches=[],
            our_position="PARITY",
            suggestions=["Vui lòng cung cấp ít nhất một đường dẫn (URL) trang web đối thủ."],
            failed_urls={},
        )

    # 1. Enforce max URLs limit
    allowed_urls = clean_urls[: settings.max_urls_per_request]
    dropped_urls = clean_urls[settings.max_urls_per_request :]

    failed_urls: dict[str, str] = {}
    for d in dropped_urls:
        failed_urls[d] = f"Bị bỏ qua do vượt quá giới hạn tối đa {settings.max_urls_per_request} URL mỗi lần."

    # 2. Check cache
    cache_key = tuple(sorted(allowed_urls))
    ttl_seconds = settings.analysis_cache_ttl_hours * 3600
    now = time.time()
    if cache_key in _ANALYSIS_CACHE:
        cached_ts, cached_report = _ANALYSIS_CACHE[cache_key]
        if now - cached_ts < ttl_seconds:
            log.info("Analysis cache hit for %d URLs (age: %.1f min)", len(allowed_urls), (now - cached_ts) / 60)
            # Return fresh copy with current failed_urls merged
            rep = cached_report.model_copy(deep=True)
            rep.failed_urls.update(failed_urls)
            return rep

    workflow_id = f"url_analysis_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    await bus.publish(
        "url_analysis_started",
        {"urls": allowed_urls, "total": len(allowed_urls)},
        workflow_id=workflow_id,
    )

    # 3. Scrape in parallel with isolated workers
    async def _scrape_one(target_url: str, idx: int) -> list[CompetitorProduct]:
        # Pre-validate URL before scraping
        is_valid, reason, _ = validate_public_url(target_url)
        if not is_valid:
            raise ValueError(reason)

        # 3.1. Try Firecrawl API first if configured
        if settings.firecrawl_api_key.strip():
            try:
                log.info("[%s] Scraping via Firecrawl API: %s", f"worker_{idx}", target_url)
                products = await scrape_with_firecrawl(target_url)
                if products:
                    return products
            except Exception as fc_err:
                log.warning("[%s] Firecrawl failed (%s), falling back to Playwright...", f"worker_{idx}", fc_err)

        # 3.2. Fallback to local Playwright BrowserWorker
        worker = BrowserWorker(
            worker_id=f"worker_custom_{idx}",
            target_name=target_url,
            start_url=target_url,
            goal="Trích xuất danh sách bảng giá và thông số kỹ thuật sản phẩm",
            target_fields=["product_name", "price_raw", "original_price_raw", "billing_cycle", "stock_status_raw", "specs"],
            workflow_id=workflow_id,
            park_on_hitl=False,
        )
        return await worker.scrape_generic(target_url)

    results = await asyncio.gather(
        *(_scrape_one(u, i) for i, u in enumerate(allowed_urls)),
        return_exceptions=True,
    )

    all_products: list[CompetitorProduct] = []
    for u, res in zip(allowed_urls, results):
        if isinstance(res, Exception):
            err_msg = str(res) or repr(res)
            log.warning("Scraping failed for %s: %s", u, err_msg)
            failed_urls[u] = err_msg
        elif isinstance(res, list):
            if not res:
                failed_urls[u] = "Không tìm thấy sản phẩm có bảng giá trên trang."
            else:
                all_products.extend(res)

    # 4. Run Role 5 PRICE_ANALYST to normalize and produce strategic recommendations
    report = await run_price_analyst(
        our_catalog=OUR_CATALOG,
        competitor_products=all_products,
        failed_urls=failed_urls,
    )

    # 5. Save to cache
    _ANALYSIS_CACHE[cache_key] = (now, report)

    # 6. Publish event to dashboard
    await bus.publish(
        "url_analysis_done",
        report.model_dump(mode="json"),
        workflow_id=workflow_id,
    )

    return report


async def stage_manual_price_change(
    sku: str,
    new_price: int,
    reason: str = "Điều chỉnh giá theo tư vấn chiến lược từ Admin",
) -> dict:
    """Stage a single product price change directly into Admin UI with Playwright and trigger HITL.

    Enforces hard profitability price floor to prevent loss-making price changes.
    """
    item = next((it for it in OUR_CATALOG if it["product_code"] == sku), OUR_CATALOG[0])

    # Enforce Price Floor Guardrail
    floor = item.get("floor_price_vnd")
    if not floor:
        if "bus" in sku.lower():
            floor = settings.min_price_floor_bus
        else:
            floor = settings.min_price_floor_std

    if new_price < floor:
        raise ValueError(
            f"Giá điều chỉnh {new_price:,} VND thấp hơn giá sàn an toàn {floor:,} VND cho sản phẩm {item['product_name']}."
        )

    updates = [
        {
            "product_code": item["product_code"],
            "product_name": item["product_name"],
            "our_current_price_vnd": item["our_current_price_vnd"],
            "recommended_new_price_vnd": new_price,
            "reason_vn": reason,
        }
    ]
    wf_id = f"wf_manual_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    orch = Orchestrator(instruction=f"Cập nhật giá thủ công cho {sku} thành {new_price:,} VND", threshold_pct=10.0)
    orch.workflow_id = wf_id
    admin_url = settings.demo_urls.get("admin", f"{settings.demo_base_url}/demo/admin.html")
    return await orch.stage_admin_update(admin_url, updates)

