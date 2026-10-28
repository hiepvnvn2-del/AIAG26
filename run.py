"""Launch the app with a stubbed LLM for offline / stage demos.

Usage:
  python run.py              # real Gemini keys from .env
  python run.py --stub       # no API keys, no network, deterministic
"""
from __future__ import annotations

import argparse
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rpa")


def main() -> None:
    ap = argparse.ArgumentParser(description="RPA 2.0 MVP server")
    ap.add_argument("--stub", action="store_true", help="Use a stubbed LLM (no API keys).")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    if args.stub:
        import app.stub_llm  # noqa: F401 — installs the stub before imports resolve
        log.info("LLM stub active — no external API calls will be made.")

    from app.config import settings
    host = args.host or settings.host
    port = args.port or settings.port

    import uvicorn
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()