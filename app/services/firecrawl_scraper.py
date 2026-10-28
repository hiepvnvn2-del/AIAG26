"""Firecrawl API Scraper Integration.

Provides cloud-based web scraping and markdown extraction using Firecrawl API,
bypassing anti-bot protections (Cloudflare, Captcha) and eliminating local
Chromium subprocess dependencies.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.config import settings
from app.schemas import CompetitorProduct
from app.services.llm_rotator import llm

log = logging.getLogger("rpa.firecrawl")


class ExtractedProductsWrapper(BaseModel):
    products: list[CompetitorProduct] = Field(default_factory=list)


async def scrape_with_firecrawl(
    url: str,
    goal: str = "Trích xuất danh sách bảng giá và thông số kỹ thuật sản phẩm",
) -> list[CompetitorProduct]:
    """Scrape web page via Firecrawl API and extract structured CompetitorProduct items.

    Raises:
        ValueError: If FIRECRAWL_API_KEY is not set or URL invalid.
        RuntimeError: If Firecrawl API fails or returns error.
    """
    api_key = settings.firecrawl_api_key.strip()
    if not api_key:
        raise ValueError("FIRECRAWL_API_KEY chưa được cấu hình.")

    endpoint = f"{settings.firecrawl_base_url.rstrip('/')}/scrape"
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "waitFor": 2000,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    log.info("[Firecrawl] Scraping %s via %s", url, endpoint)
    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            resp = await client.post(endpoint, json=payload, headers=headers)
        except Exception as exc:
            raise RuntimeError(f"Firecrawl connection failed: {exc}") from exc

    if resp.status_code != 200:
        err_detail = resp.text[:300]
        raise RuntimeError(f"Firecrawl API error (HTTP {resp.status_code}): {err_detail}")

    body = resp.json()
    if not body.get("success", False):
        raise RuntimeError(f"Firecrawl scrape failed: {body.get('error', 'Unknown error')}")

    data = body.get("data", {})
    markdown = data.get("markdown", "") or ""
    if not markdown.strip():
        log.warning("[Firecrawl] Empty markdown returned for %s", url)
        return []

    # Truncate markdown to avoid hitting LLM context ceiling
    truncated_md = markdown[:28000]

    prompt = (
        f"MỤC TIÊU: {goal}\n"
        f"WEBSITE NGUỒN: {url}\n\n"
        "Hãy đọc nội dung Markdown từ website đối thủ bên dưới và trích xuất tất cả các sản phẩm / gói dịch vụ kèm bảng giá.\n"
        "YÊU CẦU ĐẶC BIỆT:\n"
        "- product_name: Tên sản phẩm hoặc gói dịch vụ rõ ràng (vd: 'Hosting WordPress Standard', 'Tên miền .VN')\n"
        "- price_raw: Giá niêm yết (vd: '250.000đ/tháng', '590.000 VND')\n"
        "- original_price_raw: Giá gạch ngang / giá gốc trước khi giảm (nếu có, không có để null)\n"
        "- billing_cycle: 'month', 'year', hoặc 'one_time'\n"
        "- stock_status_raw: 'IN_STOCK' hoặc 'OUT_OF_STOCK'\n"
        "- specs: Các thông số kỹ thuật (RAM, SSD, vCPU, Băng thông...) dạng key-value object\n"
        f"- source_url: Điền chính xác '{url}'\n\n"
        f"--- NỘI DUNG TRANG WEB (MARKDOWN) ---\n{truncated_md}"
    )

    log.info("[Firecrawl] Calling LLM to parse extracted markdown (%d chars)...", len(truncated_md))
    try:
        extracted = await llm.generate_role_json(
            active_role="BROWSER_WORKER",
            prompt=prompt,
            schema=ExtractedProductsWrapper,
        )
        return extracted.products
    except Exception as exc:
        log.error("[Firecrawl] LLM parsing markdown failed: %s", exc)
        raise RuntimeError(f"Lỗi khi LLM trích xuất bảng giá từ Markdown: {exc}") from exc
