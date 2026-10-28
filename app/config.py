"""Application settings loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # LLM Configuration
    llm_provider: str = "anthropic"  # 'anthropic' or 'gemini'
    llm_model: str = "claude-sonnet-5"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.mwapi.dev"

    # Gemini settings
    gemini_api_key: str = ""
    gemini_api_keys: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    allowed_chat_ids: str = ""
    telegram_enabled: int = 1

    # Firecrawl Scraper API (Optional high-performance scraper for URL analysis)
    firecrawl_api_key: str = ""
    firecrawl_base_url: str = "https://api.firecrawl.dev/v1"

    # Browser
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 800
    action_timeout_ms: int = 30000

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Safety & Pricing Guardrails
    max_price_delta_pct: float = 30.0
    max_single_drop_pct: float = 30.0
    min_price_floor_std: int = 150000  # Floor price for HOSTING-WP-STD (VND)
    min_price_floor_bus: int = 350000  # Floor price for HOSTING-WP-BUS (VND)
    max_urls_per_request: int = 5
    max_steps_per_page: int = 15
    page_timeout_sec: int = 30
    analysis_cache_ttl_hours: int = 6
    max_view_combinations: int = 6  # Max tab/toggle views to scan per page

    # Ablation Flags (Evaluation & Research)
    enable_json_ld: bool = True
    enable_set_of_marks: bool = True
    enable_few_shot: bool = True
    enable_self_healing: bool = True

    # Demo targets (served by uvicorn from demo_targets/)
    demo_base_url: str = "http://127.0.0.1:8000"

    @property
    def key_list(self) -> list[str]:
        keys = []
        if self.gemini_api_key:
            keys.append(self.gemini_api_key.strip())
        if self.gemini_api_keys:
            keys.extend([k.strip() for k in self.gemini_api_keys.split(",") if k.strip()])
        return list(dict.fromkeys(keys))

    @property
    def demo_urls(self) -> dict[str, str]:
        b = self.demo_base_url.rstrip("/")
        return {
            "competitor_a": f"{b}/demo/competitor_a.html",
            "competitor_b": f"{b}/demo/competitor_b.html",
            "admin": f"{b}/demo/admin.html",
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
