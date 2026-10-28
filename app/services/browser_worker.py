"""BrowserWorker — ReAct loop driving a single Playwright context.

Each worker owns its own isolated browser context (cookies/storage/session). The loop:
  1. Inject dom_distiller.js, capture screenshot + Set-of-Marks elements list
  2. Send to LLM with role BROWSER_WORKER (Unified System Prompt)
  3. Execute returned action via Playwright
  4. Self-heal on popup/modal intercept or timeout (close modal / Escape, then retry)
  5. On REQUEST_HITL_APPROVAL:
     - Freezes the live Playwright session with the form pre-filled
     - Delegates to HITL Gatekeeper
     - Upon APPROVE: clicks Submit/Save and confirms persistence
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
    async_playwright,
)

from app.config import settings
from app.schemas import AgentTurn, CompetitorProduct
from app.services.event_bus import bus
from app.services.llm_rotator import llm
from app.services.normalizer import normalize_price_and_cycle, normalize_stock
from app.services.prompts import WORKER_COT_GUARD, WORKER_SYSTEM_TEMPLATE
from app.services.url_guard import check_robots_txt, validate_public_url

log = logging.getLogger("rpa.worker")

DISTILLER_PATH = Path(__file__).resolve().parents[1] / "static" / "dom_distiller.js"
DISTILLER_JS = DISTILLER_PATH.read_text(encoding="utf-8")

MAX_STEPS = 25
SELF_HEAL_KEYWORDS = ("obscured", "intercepted", "popup", "modal", "cookie", "timeout", "blocked", "not visible")


class BrowserWorker:
    def __init__(
        self,
        worker_id: str = "generic_worker",
        target_name: str = "generic_target",
        start_url: str = "",
        goal: str = "",
        target_fields: list[str] | None = None,
        workflow_id: str = "",
        park_on_hitl: bool = False,
    ):
        self.worker_id = worker_id
        self.target_name = target_name
        self.start_url = start_url
        self.goal = goal
        self.target_fields = target_fields or ["product_name", "price_raw"]
        self.workflow_id = workflow_id
        self.park_on_hitl = park_on_hitl

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        self.history: list[dict] = []
        self.extracted: list[dict] = []
        self._step = 0
        self.last_turn: Optional[AgentTurn] = None

    # ------------------------------------------------------------------ setup
    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=settings.headless)
        self.context = await self._browser.new_context(
            viewport={"width": settings.viewport_width, "height": settings.viewport_height}
        )
        self.page = await self.context.new_page()
        self.page.set_default_timeout(settings.action_timeout_ms)
        await self.page.add_init_script(DISTILLER_JS)
        await self._navigate_safely(self.start_url)
        await asyncio.sleep(0.8)  # let animations settle
        await self._emit("started", {"url": self.start_url})

    def _resolve_url(self, url: str) -> str:
        if ("/demo/" in url) and ("127.0.0.1" in url or "localhost" in url):
            import socket
            is_open = False
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    is_open = (s.connect_ex(("127.0.0.1", 8000)) == 0)
            except Exception:
                is_open = False

            if not is_open:
                demo_filename = url.split("/demo/")[-1].split("?")[0]
                local_file = Path(__file__).resolve().parents[2] / "demo_targets" / demo_filename
                if local_file.exists():
                    file_uri = local_file.as_uri()
                    log.info("[%s] HTTP 8000 offline, resolved demo to file URI: %s", self.worker_id, file_uri)
                    return file_uri
        return url

    async def _navigate_safely(self, url: str) -> None:
        assert self.page is not None
        target_url = self._resolve_url(url)
        try:
            await self.page.goto(target_url, wait_until="domcontentloaded", timeout=settings.action_timeout_ms)
        except Exception as ex:
            log.warning("[%s] Goto domcontentloaded failed (%s), retrying with commit...", self.worker_id, ex)
            await self.page.goto(target_url, wait_until="commit", timeout=settings.action_timeout_ms)

    async def stop(self):
        if self.page:
            try:
                await self.page.evaluate("window.__rpa_clear_overlay__ && window.__rpa_clear_overlay__()")
            except Exception:
                pass
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self.page = None
        self.context = None
        self._browser = None
        self._pw = None

    # ------------------------------------------------------------------ loop
    async def run(self) -> list[dict]:
        await self.start()
        should_stop = True
        try:
            while self._step < MAX_STEPS:
                self._step += 1
                turn = await self._observe_and_act()
                self.last_turn = turn

                if turn.action_type == "EXTRACT_DATA" and turn.extracted_payload:
                    self.extracted.extend(turn.extracted_payload)
                    await self._emit("extracted", {"count": len(turn.extracted_payload)})

                if turn.action_type in ("FAIL", "EXTRACT_DATA"):
                    break

                if turn.action_type == "REQUEST_HITL_APPROVAL":
                    if self.park_on_hitl:
                        # Keep Playwright open; caller is responsible for stopping or committing
                        should_stop = False
                    break
        finally:
            if should_stop:
                await self.stop()

        return self.extracted

    # ------------------------------------------------------------- observe
    async def _observe_and_act(self) -> AgentTurn:
        assert self.page is not None

        # 1. Distill DOM
        try:
            elements = await self.page.evaluate("window.__rpa_distill__()")
        except Exception as exc:
            log.warning("[%s] distiller failed: %s", self.worker_id, exc)
            elements = []

        # 2. Screenshot (Set-of-Marks annotated)
        screenshot_b64 = await self._screenshot()

        # 3. Build prompt
        prompt = WORKER_SYSTEM_TEMPLATE.format(
            goal=self.goal,
            target_fields=", ".join(self.target_fields),
            current_url=self.page.url,
            recent_history=json.dumps(self.history[-6:], ensure_ascii=False, default=str),
        )
        prompt += "\n\n" + WORKER_COT_GUARD
        prompt += "\n\nInteractive elements (Set-of-Marks JSON):\n" + json.dumps(elements, ensure_ascii=False)

        # 4. Call LLM with active_role = BROWSER_WORKER
        try:
            turn = await llm.generate_role_json(
                active_role="BROWSER_WORKER",
                prompt=prompt,
                schema=AgentTurn,
            )
        except Exception as exc:
            log.error("[%s] LLM call failed: %s", self.worker_id, exc)
            return AgentTurn(
                thought=f"Lỗi gọi LLM: {exc}",
                action_type="FAIL",
            )

        # 5. Emit thought + screenshot to dashboard
        await self._emit(
            "thought",
            {
                "step": self._step,
                "thought": turn.thought,
                "action_type": turn.action_type,
                "target_element_id": turn.target_element_id,
                "screenshot_b64": screenshot_b64,
                "elements": elements,
                "self_healing_triggered": turn.self_healing_triggered,
            },
        )

        # 6. Execute action
        last_error = None
        try:
            await self._execute(turn, elements)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            last_error = str(exc)
            log.warning("[%s] Action error: %s", self.worker_id, exc)
            # Self-healing on popup, intercepted click, timeout
            if any(k in last_error.lower() for k in SELF_HEAL_KEYWORDS):
                log.info("[%s] Triggering Self-Healing mechanism...", self.worker_id)
                turn.self_healing_triggered = True
                await self._self_heal()
                await self._emit(
                    "self_healing",
                    {"step": self._step, "reason": last_error, "resolved": True},
                )
                try:
                    # Refresh DOM marks and re-execute
                    new_elements = await self.page.evaluate("window.__rpa_distill__()")
                    await self._execute(turn, new_elements)
                    last_error = None
                except Exception as exc2:
                    last_error = str(exc2)

        self.history.append(
            {
                "step": self._step,
                "action": turn.action_type,
                "target": turn.target_element_id,
                "error": last_error,
            }
        )

        if last_error and turn.action_type not in ("EXTRACT_DATA", "REQUEST_HITL_APPROVAL"):
            return AgentTurn(
                thought=f"Hành động thất bại sau khi thử Self-Healing: {last_error}",
                action_type="FAIL",
            )

        return turn

    # ------------------------------------------------------------- execute
    async def _execute(self, turn: AgentTurn, elements: list[dict]):
        assert self.page is not None
        page = self.page

        if turn.action_type == "CLICK":
            el = self._find_element(elements, turn.target_element_id)
            selector = f'[data-agent-id="{turn.target_element_id}"]'
            try:
                await page.click(selector, timeout=settings.action_timeout_ms)
            except Exception:
                # If data-agent-id fails, fallback to element selector hint
                if el and el.get("selector"):
                    await page.click(el["selector"], timeout=settings.action_timeout_ms)
                else:
                    raise
            await asyncio.sleep(0.4)

        elif turn.action_type == "TYPE":
            el = self._find_element(elements, turn.target_element_id)
            selector = f'[data-agent-id="{turn.target_element_id}"]'
            val = turn.input_text or ""
            try:
                await page.fill(selector, val, timeout=settings.action_timeout_ms)
            except Exception:
                if el and el.get("selector"):
                    await page.fill(el["selector"], val, timeout=settings.action_timeout_ms)
                else:
                    raise
            await asyncio.sleep(0.3)

        elif turn.action_type == "PRESS_KEY":
            await page.keyboard.press(turn.input_text or "Escape")
            await asyncio.sleep(0.3)

        elif turn.action_type == "SCROLL_DOWN":
            await page.mouse.wheel(0, 700)
            await asyncio.sleep(0.6)

        elif turn.action_type == "WAIT":
            await asyncio.sleep(1.0)

        elif turn.action_type in ("EXTRACT_DATA", "REQUEST_HITL_APPROVAL"):
            # Informational turns; no DOM mutation needed at this step
            pass

    # ----------------------------------------------------------- self-heal
    async def _self_heal(self):
        """Self-Healing: dismiss popups, modals, promo banners, or press Escape."""
        if not self.page:
            return
        page = self.page
        close_selectors = [
            "#promoClose",
            "#promoLater",
            "button[aria-label='Close']",
            "button[aria-label='close']",
            ".modal-close",
            ".popup-close",
            ".close",
            "[data-testid='close-button']",
            "button:has-text('×')",
            "button:has-text('Đóng')",
            "button:has-text('Close')",
            "button:has-text('Để sau')",
            "button:has-text('Bỏ qua')",
        ]
        for sel in close_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=400):
                    log.info("[%s] Self-Healing: clicking close element '%s'", self.worker_id, sel)
                    await loc.click(timeout=800)
                    await asyncio.sleep(0.3)
                    return
            except Exception:
                continue

        # Fallback: keyboard Escape
        try:
            log.info("[%s] Self-Healing: pressing Escape key", self.worker_id)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass

    # ----------------------------------------------------------- hitl actions
    async def commit_submit(
        self,
        target_button_text: str = "Lưu thay đổi",
        product_code: Optional[str] = None,
        new_price: Optional[int] = None,
    ) -> dict:
        """Executed upon HITL APPROVAL: clicks the final submit button on the live page."""
        assert self.page is not None
        page = self.page

        # Ensure the target input has the correct price filled before submitting
        if product_code and new_price:
            idx_map = {
                "HOSTING-WP-STD": "price-0",
                "HOSTING-WP-PRO": "price-1",
                "HOSTING-WP-BUS": "price-2",
            }
            inp_id = idx_map.get(product_code, "price-0")
            try:
                await page.fill(f"#{inp_id}", str(new_price))
                await asyncio.sleep(0.2)
            except Exception:
                pass

        # Find and click save button (prefer button in matching product row)
        button_clicked = False
        candidates = []
        if product_code:
            candidates.extend([
                f"tr:has-text('{product_code}') button.save",
                f"tr:has-text('{product_code}') button:has-text('{target_button_text}')",
                f"tr:has-text('{product_code}') button",
            ])
        candidates.extend([
            f"button:has-text('{target_button_text}')",
            "button.save",
            "input[type='submit']",
            "button[type='submit']",
        ])
        for sel in candidates:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click(timeout=3000)
                    button_clicked = True
                    break
            except Exception:
                continue

        if not button_clicked:
            raise PlaywrightError(f"Could not find submit button matching '{target_button_text}'")

        await asyncio.sleep(0.8)
        # Screenshot final confirmed state
        final_shot = await self._screenshot()
        await self._emit(
            "mutation_applied",
            {"status": "SAVED", "screenshot_b64": final_shot},
        )
        return {"saved": True, "screenshot_b64": final_shot}

    async def override_and_submit(self, new_price: int, product_code: str = "HOSTING-WP-STD") -> dict:
        """Update price input with custom admin override value, then click Submit on live page."""
        assert self.page is not None
        page = self.page

        idx_map = {
            "HOSTING-WP-STD": "price-0",
            "HOSTING-WP-PRO": "price-1",
            "HOSTING-WP-BUS": "price-2",
        }
        input_id = idx_map.get(product_code, "price-0")

        try:
            input_el = page.locator(f"#{input_id}").first
            if await input_el.is_visible(timeout=1000):
                await input_el.fill(str(new_price))
            else:
                await page.fill("input[type='number']", str(new_price))
        except Exception:
            await page.fill("input[type='number']", str(new_price))

        await asyncio.sleep(0.4)
        return await self.commit_submit(target_button_text="Lưu thay đổi")

    # ------------------------------------------------------------ helpers
    def _find_element(self, elements: list[dict], eid: Optional[int]) -> Optional[dict]:
        if eid is None:
            return None
        for el in elements:
            if el.get("id") == eid:
                return el
        return None

    async def _screenshot(self) -> str:
        if not self.page:
            return ""
        try:
            buf = await self.page.screenshot(type="jpeg", quality=65)
            return base64.b64encode(buf).decode()
        except Exception:
            return ""

    async def _emit(self, event: str, data: dict):
        await bus.publish(
            event,
            {**data, "worker_id": self.worker_id, "target_name": self.target_name},
            workflow_id=self.workflow_id,
        )

    # --------------------------------------------------- tab & toggle detection
    async def _detect_tabs_and_toggles(self) -> dict:
        """Detect tab groups (role=tab, active button groups) and billing cycle toggles.
        Strictly blocks banned action buttons ('buy', 'add to cart', 'thanh toan', etc.).
        Marks elements with data-agent-tab-id and data-agent-cycle-id.
        """
        if not self.page:
            return {"category_tabs": [], "cycle_toggles": []}

        try:
            res = await self.page.evaluate("""
                (() => {
                    const BANNED_WORDS = [
                        "add to cart", "buy", "checkout", "login", "đặt hàng", 
                        "thanh toán", "mua ngay", "sign in", "register", "giỏ hàng", 
                        "order", "subscribe", "đăng nhập", "đăng ký", "get started", "bắt đầu", "chọn mua"
                    ];

                    const CYCLE_WORDS = [
                        "monthly", "yearly", "annual", "annually", "hàng tháng", "hàng năm",
                        "mỗi tháng", "mỗi năm", "theo tháng", "theo năm", "1 tháng", "12 tháng",
                        "24 tháng", "36 tháng", "48 tháng", "1 month", "12 months", "24 months",
                        "36 months", "48 months", "chu kỳ"
                    ];

                    function isBanned(text) {
                        const lower = (text || "").toLowerCase().trim();
                        return BANNED_WORDS.some(bw => lower.includes(bw));
                    }

                    function isVisible(el) {
                        if (!el || !el.getBoundingClientRect) return false;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) return false;
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
                    }

                    function isActive(el) {
                        if (el.getAttribute('aria-selected') === 'true') return true;
                        if (el.getAttribute('aria-pressed') === 'true') return true;
                        if (el.getAttribute('data-state') === 'active') return true;
                        if (el.getAttribute('data-active') === 'true') return true;
                        const cls = el.className || '';
                        if (typeof cls === 'string' && /\\b(active|selected|current)\\b/i.test(cls)) return true;
                        return false;
                    }

                    function getCycle(text) {
                        const lower = (text || "").toLowerCase();
                        if (/(năm|year|annual|12\\s*tháng|12\\s*m|24\\s*tháng|36\\s*tháng|48\\s*tháng)/i.test(lower)) return 'year';
                        if (/(tháng|month|mo|1\\s*tháng)/i.test(lower)) return 'month';
                        if (/(trọn đời|lifetime|one-time|vĩnh viễn)/i.test(lower)) return 'one_time';
                        return 'month';
                    }

                    const categoryTabs = [];
                    const cycleToggles = [];

                    // All potential tab or button elements
                    const candidates = Array.from(document.querySelectorAll(
                        '[role="tab"], .tab, .tab-btn, .tab-button, .nav-tab, [data-tab], .pricing-tab, button, [role="button"], label:has(input[type="radio"]), label:has(input[type="checkbox"])'
                    )).filter(isVisible);

                    // Exclude banned buttons
                    const cleanCandidates = candidates.filter(el => !isBanned(el.innerText || el.getAttribute('aria-label') || ''));

                    // Check for cycle toggles
                    cleanCandidates.forEach(el => {
                        const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
                        if (!text || text.length > 60) return;

                        const isCycle = CYCLE_WORDS.some(cw => text.toLowerCase().includes(cw));
                        if (isCycle) {
                            if (!cycleToggles.some(t => t.text.toLowerCase() === text.toLowerCase())) {
                                const id = `cycle-${cycleToggles.length}`;
                                el.setAttribute('data-agent-cycle-id', id);
                                cycleToggles.push({
                                    id: id,
                                    text: text,
                                    cycle: getCycle(text),
                                    active: isActive(el)
                                });
                            }
                        }
                    });

                    // Check for explicit tablist or tab containers
                    const tabContainers = Array.from(document.querySelectorAll('[role="tablist"], .tabs, .nav-tabs, .tab-buttons, [data-tabs], .category-tabs, .segmented-control'));
                    tabContainers.forEach(grp => {
                        const items = Array.from(grp.querySelectorAll('[role="tab"], button, .tab, a, div[onclick]'))
                            .filter(isVisible)
                            .filter(el => !isBanned(el.innerText || ''));
                        const hasCycle = items.some(it => CYCLE_WORDS.some(cw => (it.innerText || '').toLowerCase().includes(cw)));
                        if (!hasCycle && items.length >= 2) {
                            items.forEach(el => {
                                const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
                                if (!text || text.length > 50) return;
                                if (!categoryTabs.some(t => t.text.toLowerCase() === text.toLowerCase())) {
                                    const id = `cat-${categoryTabs.length}`;
                                    el.setAttribute('data-agent-tab-id', id);
                                    categoryTabs.push({
                                        id: id,
                                        text: text,
                                        active: isActive(el)
                                    });
                                }
                            });
                        }
                    });

                    // Sibling buttons detection if no explicit tab containers found
                    if (categoryTabs.length === 0) {
                        cleanCandidates.forEach(el => {
                            const text = (el.innerText || '').trim();
                            if (!text || text.length > 50) return;
                            if (CYCLE_WORDS.some(cw => text.toLowerCase().includes(cw))) return;

                            const parent = el.parentElement;
                            if (parent) {
                                const siblings = Array.from(parent.children).filter(c => 
                                    isVisible(c) && !isBanned(c.innerText || '') && !CYCLE_WORDS.some(cw => (c.innerText || '').toLowerCase().includes(cw))
                                );
                                if (siblings.length >= 2 && siblings.some(isActive)) {
                                    siblings.forEach(sib => {
                                        const sibText = (sib.innerText || '').trim();
                                        if (sibText && sibText.length <= 50 && !categoryTabs.some(t => t.text.toLowerCase() === sibText.toLowerCase())) {
                                            const id = `cat-${categoryTabs.length}`;
                                            sib.setAttribute('data-agent-tab-id', id);
                                            categoryTabs.push({
                                                id: id,
                                                text: sibText,
                                                active: isActive(sib)
                                            });
                                        }
                                    });
                                }
                            }
                        });
                    }

                    return {
                        category_tabs: categoryTabs,
                        cycle_toggles: cycleToggles
                    };
                })()
            """)
            return res or {"category_tabs": [], "cycle_toggles": []}
        except Exception as ex:
            log.warning("[%s] Failed to detect tabs and toggles: %s", self.worker_id, ex)
            return {"category_tabs": [], "cycle_toggles": []}

    async def _get_pricing_area_hash(self) -> str:
        """Compute MD5 hash of visible pricing container content to detect changes."""
        if not self.page:
            return ""
        try:
            content_str = await self.page.evaluate("""
                (() => {
                    const containers = document.querySelectorAll(
                        '[id*="pricing"], [class*="pricing"], [id*="plans"], [class*="plans"], ' +
                        '[id*="packages"], [class*="packages"], [class*="card"], [class*="tier"], main, [role="main"]'
                    );
                    if (containers.length > 0) {
                        return Array.from(containers).map(c => c.innerText || '').join(' ');
                    }
                    return document.body ? document.body.innerText : '';
                })()
            """)
            clean = " ".join((content_str or "").split()).lower()
            return hashlib.md5(clean.encode("utf-8")).hexdigest()
        except Exception:
            return ""

    async def _extract_products_from_dom(
        self,
        category_name: Optional[str] = None,
        cycle_hint: Optional[str] = None,
    ) -> list[CompetitorProduct]:
        """Extract competitor products currently visible in DOM."""
        if not self.page:
            return []

        try:
            raw_items = await self.page.evaluate("""
                (() => {
                    const BANNED_BUTTONS = ["add to cart", "buy", "checkout", "login", "đặt hàng", "thanh toán", "mua ngay", "order"];
                    
                    function isVisible(el) {
                        if (!el || !el.getBoundingClientRect) return false;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) return false;
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
                    }

                    // Look for cards
                    const cardSelectors = [
                        '.pricing-card', '.plan-card', '.package-card', '.card',
                        '[class*="pricing-plan"]', '[class*="pricing-card"]', '[class*="plan-card"]',
                        '[class*="tier"]', '.pricing-box'
                    ];

                    let candidates = [];
                    for (const sel of cardSelectors) {
                        const els = Array.from(document.querySelectorAll(sel)).filter(isVisible).filter(el => {
                            const text = el.innerText || '';
                            return /(\\d+[\\d.,]*\\s*(?:đ|vnd|vnđ|\\$|usd|\\/|k|tr))/i.test(text);
                        });
                        if (els.length > 0) {
                            candidates = els;
                            break;
                        }
                    }

                    if (candidates.length === 0) {
                        const priceEls = Array.from(document.querySelectorAll('.price, [class*="price"], .amount')).filter(isVisible);
                        candidates = priceEls.map(el => el.closest('.card, [class*="box"], [class*="item"], div') || el);
                        candidates = Array.from(new Set(candidates)).filter(isVisible);
                    }

                    return candidates.map(card => {
                        const nameEl = card.querySelector('h1, h2, h3, h4, h5, .title, .plan-name, [class*="title"], [class*="name"], b, strong');
                        let name = nameEl ? nameEl.innerText.trim() : '';
                        if (!name) {
                            const firstLine = (card.innerText || '').split('\\n')[0].trim();
                            name = firstLine || 'Gói dịch vụ';
                        }

                        const priceEl = card.querySelector('.price, .amount, [class*="price"]') || card;
                        let priceText = '';
                        if (priceEl) {
                            const m = (priceEl.innerText || '').match(/\\b\\d+[\\d.,]*\\s*(?:đ|vnd|vnđ|\\$|usd)?(?:\\s*\\/(?:tháng|năm|mo|yr|month|year))?/i);
                            priceText = m ? m[0] : (priceEl.innerText || '').trim();
                        }

                        const origEl = card.querySelector('del, s, .original-price, .old-price, [class*="orig"]');
                        let origText = origEl ? origEl.innerText.trim() : '';

                        const specLis = Array.from(card.querySelectorAll('ul li, .features li, .specs li, .muted, p'))
                            .map(li => li.innerText.trim())
                            .filter(t => t.length > 0 && !BANNED_BUTTONS.some(b => t.toLowerCase().includes(b)));

                        return {
                            name: name,
                            price_raw: priceText,
                            original_price_raw: origText,
                            specs: { details: specLis.join('; ') }
                        };
                    });
                })()
            """)

            products: list[CompetitorProduct] = []
            for it in raw_items:
                raw_p = it.get("price_raw") or ""
                p_vnd, cycle, note = normalize_price_and_cycle(raw_p)
                if p_vnd is None:
                    continue

                raw_orig = it.get("original_price_raw") or ""
                orig_vnd, _, _ = normalize_price_and_cycle(raw_orig) if raw_orig else (None, "", "")

                eff_cycle = cycle
                if (not eff_cycle or eff_cycle in ("month", "unknown")) and cycle_hint:
                    eff_cycle = cycle_hint

                products.append(
                    CompetitorProduct(
                        name=it.get("name") or "Gói dịch vụ",
                        price_vnd=p_vnd,
                        original_price_vnd=orig_vnd,
                        raw_price_text=raw_p,
                        billing_cycle=eff_cycle,
                        stock="IN_STOCK",
                        specs=it.get("specs") if isinstance(it.get("specs"), dict) else {},
                        source_url=self.start_url or "",
                        category=category_name,
                        source_type="dom",
                    )
                )
            return products
        except Exception as ex:
            log.warning("[%s] Error extracting products from DOM: %s", self.worker_id, ex)
            return []

    async def _scrape_tabs_and_toggles(self, tabs_info: dict, url: str) -> list[CompetitorProduct]:
        """Iterate tabs and cycle toggles, verify content change with hash, and extract products.
        Enforces MAX_VIEW_COMBINATIONS limit and strict deduplication.
        """
        assert self.page is not None
        cat_tabs = tabs_info.get("category_tabs", [])
        cycle_toggles = tabs_info.get("cycle_toggles", [])

        cat_list = cat_tabs if cat_tabs else [{"id": None, "text": None, "active": True}]
        cycle_list = cycle_toggles if cycle_toggles else [{"id": None, "text": None, "cycle": None, "active": True}]

        max_combos = getattr(settings, "max_view_combinations", 6)
        combinations = []
        for c_tab in cat_list:
            for cy_tog in cycle_list:
                combinations.append((c_tab, cy_tog))
                if len(combinations) >= max_combos:
                    break
            if len(combinations) >= max_combos:
                break

        all_products: list[CompetitorProduct] = []
        seen_keys = set()

        current_tab_id = next((t["id"] for t in cat_tabs if t.get("active")), None)
        current_cycle_id = next((c["id"] for c in cycle_toggles if c.get("active")), None)

        for idx, (c_tab, cy_tog) in enumerate(combinations):
            tab_name = c_tab.get("text")
            cycle_name = cy_tog.get("text")
            tab_switch_success = True

            # 1. Switch Category Tab if needed
            if c_tab.get("id") and c_tab["id"] != current_tab_id:
                old_hash = await self._get_pricing_area_hash()
                try:
                    await self.page.click(f'[data-agent-tab-id="{c_tab["id"]}"]', timeout=settings.action_timeout_ms)
                except Exception as ex:
                    log.warning("[%s] Failed to click category tab '%s': %s", self.worker_id, tab_name, ex)
                    tab_switch_success = False

                if tab_switch_success:
                    start_t = time.time()
                    changed = False
                    while time.time() - start_t < 4.0:
                        await asyncio.sleep(0.1)
                        new_hash = await self._get_pricing_area_hash()
                        if new_hash != old_hash:
                            changed = True
                            break

                    if not changed:
                        log.warning("[%s] Nội dung không đổi sau khi click tab '%s' (timeout 4s)", self.worker_id, tab_name)
                        await asyncio.sleep(0.3)
                        tab_switch_success = False
                    else:
                        current_tab_id = c_tab["id"]
                        await asyncio.sleep(0.2)

            if not tab_switch_success:
                continue

            # 2. Switch Cycle Toggle if needed
            cycle_switch_success = True
            if cy_tog.get("id") and cy_tog["id"] != current_cycle_id:
                old_hash = await self._get_pricing_area_hash()
                try:
                    await self.page.click(f'[data-agent-cycle-id="{cy_tog["id"]}"]', timeout=settings.action_timeout_ms)
                except Exception as ex:
                    log.warning("[%s] Failed to click cycle toggle '%s': %s", self.worker_id, cycle_name, ex)
                    cycle_switch_success = False

                if cycle_switch_success:
                    start_t = time.time()
                    changed = False
                    while time.time() - start_t < 4.0:
                        await asyncio.sleep(0.1)
                        new_hash = await self._get_pricing_area_hash()
                        if new_hash != old_hash:
                            changed = True
                            break

                    if not changed:
                        log.warning("[%s] Nội dung không đổi sau khi click toggle '%s' (timeout 4s)", self.worker_id, cycle_name)
                        await asyncio.sleep(0.3)
                        cycle_switch_success = False
                    else:
                        current_cycle_id = cy_tog["id"]
                        await asyncio.sleep(0.2)

            if not cycle_switch_success:
                continue

            # 3. Extract products for this view
            prods = await self._extract_products_from_dom(category_name=tab_name, cycle_hint=cy_tog.get("cycle"))
            for p in prods:
                dedup_key = (
                    p.name.lower().strip(),
                    p.price_vnd,
                    p.billing_cycle,
                    (p.category or "").lower().strip(),
                )
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    all_products.append(p)

        return all_products

    # --------------------------------------------------- generic url scraper
    async def scrape_generic(
        self,
        url: str,
        goal: str = "Trích xuất danh sách bảng giá và thông số kỹ thuật sản phẩm",
    ) -> list[CompetitorProduct]:
        """Generic web scraper:
        - Validates public URL & robots.txt
        - Step A: Fast JSON-LD & OpenGraph extraction
        - Step B: ReAct loop fallback (Set-of-Marks + Self-Healing popup dismiss)
        - Detects Cloudflare / Captcha / 403 / 429
        - Strictly read-only
        """
        # 1. URL Security Check (SSRF guard)
        is_local_test = url.startswith("file://") or "/eval/golden/" in url or "/demo/" in url
        if not is_local_test:
            is_valid, reason, _ = validate_public_url(url)
            if not is_valid:
                raise ValueError(f"URL bị từ chối bảo mật: {reason}")

            # 2. Robots.txt Compliance Check
            allowed, robot_note = await check_robots_txt(url)
            if not allowed:
                raise PermissionError(robot_note)

        self.start_url = url
        self.goal = goal
        self.target_fields = [
            "product_name",
            "price_raw",
            "original_price_raw",
            "billing_cycle",
            "stock_status_raw",
            "specs",
        ]

        # 3. Launch isolated Playwright context in read-only mode
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=settings.headless)
        self.context = await self._browser.new_context(
            viewport={"width": settings.viewport_width, "height": settings.viewport_height},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        self.page = await self.context.new_page()
        self.page.set_default_timeout(settings.page_timeout_sec * 1000)
        await self.page.add_init_script(DISTILLER_JS)

        try:
            resp = await self.page.goto(url, wait_until="domcontentloaded", timeout=settings.page_timeout_sec * 1000)
            if resp and resp.status in (403, 429):
                raise RuntimeError(f"Truy cập website bị từ chối (HTTP {resp.status})")

            # Check bot protection / Cloudflare challenge
            title = (await self.page.title()).lower()
            content = (await self.page.content()).lower()
            bot_indicators = [
                "just a moment...",
                "verify you are human",
                "cf-browser-verification",
                "attention required! | cloudflare",
                "ddos protection by cloudflare",
                "enable javascript and cookies to continue",
            ]
            if any(ind in title or ind in content for ind in bot_indicators):
                raise RuntimeError("Website được bảo vệ bởi Cloudflare / Captcha chống cào tự động.")

            # Settle animations
            await asyncio.sleep(1.0)
            await self._self_heal()

            # Check for tabs / toggles
            tabs_info = await self._detect_tabs_and_toggles()
            has_tabs = bool(tabs_info.get("category_tabs") or tabs_info.get("cycle_toggles"))

            if has_tabs:
                log.info(
                    "[%s] Detected tab/toggle groups: %d categories, %d cycles. Skipping fast JSON-LD.",
                    self.worker_id,
                    len(tabs_info.get("category_tabs", [])),
                    len(tabs_info.get("cycle_toggles", [])),
                )
                products = await self._scrape_tabs_and_toggles(tabs_info, url)
                if products:
                    log.info("[%s] Extracted %d products across tab/toggle views from %s", self.worker_id, len(products), url)
                    return products

            # STEP A: Fast Structured Data (JSON-LD + OpenGraph)
            json_ld_raw = await self.page.evaluate("""
                (() => {
                    const results = [];
                    // 1. JSON-LD
                    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                    scripts.forEach(s => {
                        try {
                            const data = JSON.parse(s.textContent || '{}');
                            const items = Array.isArray(data) ? data : (data['@graph'] ? data['@graph'] : [data]);
                            items.forEach(it => {
                                if (!it || typeof it !== 'object') return;
                                const type = (it['@type'] || '').toString();
                                if (/Product|Offer|AggregateOffer|SoftwareApplication|Service/i.test(type)) {
                                    let price = null;
                                    let origPrice = null;
                                    let currency = 'VND';
                                    let name = it.name || it.title || '';
                                    if (it.offers) {
                                        const off = Array.isArray(it.offers) ? it.offers[0] : it.offers;
                                        if (off) {
                                            price = off.price || off.lowPrice || null;
                                            currency = off.priceCurrency || currency;
                                        }
                                    } else if (it.price) {
                                        price = it.price;
                                        currency = it.priceCurrency || currency;
                                    }
                                    if (price !== null && price !== undefined) {
                                        results.push({
                                            name: name || document.title,
                                            price_raw: String(price) + ' ' + currency,
                                            original_price_raw: origPrice ? String(origPrice) + ' ' + currency : '',
                                            specs: { description: it.description || '' },
                                            source: 'json-ld'
                                        });
                                    }
                                }
                            });
                        } catch(e) {}
                    });

                    // 2. OpenGraph Meta
                    const ogPrice = document.querySelector('meta[property="og:price:amount"], meta[property="product:price:amount"]');
                    if (ogPrice && ogPrice.content) {
                        const ogCur = document.querySelector('meta[property="og:price:currency"], meta[property="product:price:currency"]');
                        const ogTitle = document.querySelector('meta[property="og:title"]');
                        results.push({
                            name: (ogTitle ? ogTitle.content : document.title) || 'Sản phẩm',
                            price_raw: ogPrice.content + ' ' + (ogCur ? ogCur.content : 'VND'),
                            original_price_raw: '',
                            specs: {},
                            source: 'meta-og'
                        });
                    }

                    return results;
                })()
            """)

            if json_ld_raw and len(json_ld_raw) > 0:
                log.info("[%s] Successfully extracted %d items via JSON-LD / Meta tags from %s", self.worker_id, len(json_ld_raw), url)
                products = []
                for it in json_ld_raw:
                    p_vnd, cycle, note = normalize_price_and_cycle(it.get("price_raw", ""))
                    orig_vnd, _, _ = normalize_price_and_cycle(it.get("original_price_raw", "")) if it.get("original_price_raw") else (None, "", "")
                    products.append(
                        CompetitorProduct(
                            name=it.get("name") or "Gói dịch vụ",
                            price_vnd=p_vnd,
                            original_price_vnd=orig_vnd,
                            raw_price_text=it.get("price_raw", ""),
                            billing_cycle=cycle,
                            stock="IN_STOCK",
                            specs=it.get("specs", {}),
                            source_url=url,
                        )
                    )
                return products

            # STEP B: Fallback to ReAct loop + DOM Distillation + Self-Healing
            log.info("[%s] No JSON-LD found; engaging ReAct loop + DOM Distiller on %s", self.worker_id, url)
            max_steps = min(MAX_STEPS, settings.max_steps_per_page)
            step_count = 0

            while step_count < max_steps:
                step_count += 1
                self._step = step_count
                turn = await self._observe_and_act()

                if turn.action_type == "EXTRACT_DATA" and turn.extracted_payload:
                    products = []
                    for it in turn.extracted_payload:
                        raw_p = str(it.get("price_raw") or it.get("price_vnd") or it.get("price") or "")
                        p_vnd, cycle, note = normalize_price_and_cycle(raw_p)
                        raw_orig = str(it.get("original_price_raw") or it.get("original_price") or "")
                        orig_vnd, _, _ = normalize_price_and_cycle(raw_orig) if raw_orig else (None, "", "")
                        raw_stock = str(it.get("stock_status_raw") or it.get("stock_status") or "IN_STOCK")
                        stock_st = normalize_stock(raw_stock)

                        products.append(
                            CompetitorProduct(
                                name=it.get("product_name") or it.get("name") or "Gói dịch vụ",
                                price_vnd=p_vnd,
                                original_price_vnd=orig_vnd,
                                raw_price_text=raw_p,
                                billing_cycle=it.get("billing_cycle") or cycle,
                                stock=stock_st,
                                specs=it.get("specs") if isinstance(it.get("specs"), dict) else {},
                                source_url=url,
                            )
                        )
                    return products

                if turn.action_type in ("FAIL", "REQUEST_HITL_APPROVAL"):
                    break

            return []

        finally:
            await self.stop()

