"""Smoke test: verify dom_distiller.js finds interactive elements on all demo pages."""
import asyncio, json, sys
from pathlib import Path
from playwright.async_api import async_playwright

DISTILLER = (Path(__file__).parent / "app" / "static" / "dom_distiller.js").read_text(encoding="utf-8")
BASE = "http://127.0.0.1:8000/demo"


async def main():
    pages = {
        "competitor_a": f"{BASE}/competitor_a.html",
        "competitor_b": f"{BASE}/competitor_b.html",
        "admin": f"{BASE}/admin.html",
    }
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for name, url in pages.items():
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(url, wait_until="domcontentloaded")
            await page.add_init_script(DISTILLER)
            await page.evaluate(DISTILLER)  # ensure fn exists in this context
            await page.wait_for_timeout(400)

            els = await page.evaluate("window.__rpa_distill__()")
            ids = [e["id"] for e in els]
            print(f"\n=== {name} :: {len(els)} elements ===")
            for e in els[:6]:
                print(f"  [{e['id']}] {e['tag']:<8} {e['text'][:44]!r} bbox={e['bbox']}")
            assert len(els) > 0, f"{name}: distiller found nothing"
            assert ids == sorted(ids) and len(set(ids)) == len(ids), f"{name}: ids not unique/ordered"

            # Overlay must actually be drawn
            overlay = await page.evaluate("document.getElementById('__rpa_som_overlay__')?.childElementCount || 0")
            assert overlay == len(els), f"{name}: overlay marks {overlay} != {len(els)}"
            print(f"  overlay marks: {overlay} OK")
            await page.close()
        await browser.close()
    print("\nDISTILLER SMOKE TEST PASSED")


asyncio.run(main())