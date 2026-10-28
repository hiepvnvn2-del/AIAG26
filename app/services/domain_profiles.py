"""Domain Profiles Registry for RPA 2.0.

Provides tailored scraping strategies (selectors, popups, engine preference)
per competitor domain to maximize extraction accuracy and speed.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import ROOT_DIR

log = logging.getLogger("rpa.domain_profiles")
PROFILES_FILE = ROOT_DIR / "data" / "domain_profiles.json"


def _load_profiles() -> dict[str, Any]:
    if not PROFILES_FILE.exists():
        return {}
    try:
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Could not read domain_profiles.json: %s", exc)
        return {}


def get_profile_for_url(url: str) -> dict[str, Any]:
    """Extract domain from URL and return profile if present, else default."""
    try:
        parsed = urlparse(url)
        netloc = (parsed.netloc or "").lower().split(":")[0]
        # Remove www.
        if netloc.startswith("www."):
            netloc = netloc[4:]
    except Exception:
        netloc = ""

    profiles = _load_profiles()
    for domain, conf in profiles.items():
        if domain in netloc:
            return conf

    # Default profile
    return {
        "domain": netloc or "generic",
        "engine_preference": "auto",
        "price_card_selector": None,
        "popup_selectors": [".close", ".modal-close", "[aria-label='Close']", "#close"],
        "wait_after_load_ms": 1500,
        "currency_default": "VND",
        "notes": "Cấu hình chuẩn tự động dò tìm cấu trúc.",
    }
