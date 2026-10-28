"""Multi-provider LLM client supporting Anthropic (Claude-3.5/Sonnet-5) and Gemini with rotation.

Features:
- Seamless routing between Anthropic (`claude-sonnet-5`) and Gemini (`gemini-2.5-flash`).
- Supports Unified System Prompt with `active_role` directive.
- Enforces strict JSON output validation via Pydantic models.
- Auto-repair on JSON validation error.
- Multi-key rotation and cooldown for Gemini when configured.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.services.prompts import UNIFIED_SYSTEM_PROMPT

log = logging.getLogger("rpa.llm")

T = TypeVar("T", bound=BaseModel)

_QUOTA_MARKERS = ("429", "quota", "rate limit", "resource_exhausted", "exceeded")
_COOLDOWN_SECONDS = 60.0


class NoKeysAvailable(RuntimeError):
    pass


class _KeyState:
    __slots__ = ("key", "parked_until")

    def __init__(self, key: str) -> None:
        self.key = key
        self.parked_until = 0.0

    @property
    def ready(self) -> bool:
        return time.monotonic() >= self.parked_until


class UnifiedLLMClient:
    def __init__(self) -> None:
        self._provider = (settings.llm_provider or "anthropic").lower()
        self._keys = [_KeyState(k) for k in settings.key_list]
        self._cursor = 0
        self._lock = asyncio.Lock()

    # ---------- role-based core call ----------
    async def generate_role_json(
        self,
        active_role: str,
        prompt: str,
        schema: type[T],
        temperature: float = 0.1,
        max_output_tokens: int = 2500,
        attempts: int = 3,
    ) -> T:
        """Call LLM with Unified System Prompt and active_role context."""
        role_instruction = (
            f"{UNIFIED_SYSTEM_PROMPT}\n\n"
            f"[SYSTEM RUNTIME DIRECTIVE: You are now actively executing as {active_role}. "
            f"Fulfill all tasks and produce strict JSON conforming to the requested schema.]"
        )
        role_prompt = f"[ACTIVE_ROLE: {active_role}]\n\n{prompt}"
        return await self.generate_json(
            system_instruction=role_instruction,
            prompt=role_prompt,
            schema=schema,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            attempts=attempts,
        )

    # ---------- core call ----------
    async def generate_json(
        self,
        prompt: str,
        schema: type[T],
        system_instruction: str | None = None,
        system: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 2500,
        attempts: int = 3,
    ) -> T:
        """Call LLM, coercing the reply into `schema`. Dispatches by provider."""
        effective_sys = system_instruction or system or UNIFIED_SYSTEM_PROMPT

        # Try Anthropic first if configured or selected
        if self._provider == "anthropic" or settings.anthropic_api_key:
            try:
                return await self._call_anthropic(
                    prompt=prompt,
                    schema=schema,
                    system_instruction=effective_sys,
                    temperature=temperature,
                    max_tokens=max_output_tokens,
                    attempts=attempts,
                )
            except Exception as exc:
                log.warning("Anthropic call failed (%s) — checking Gemini fallback", exc)
                if not settings.key_list:
                    raise

        # Fallback to Gemini
        return await self._call_gemini(
            prompt=prompt,
            schema=schema,
            system_instruction=effective_sys,
            temperature=temperature,
            max_tokens=max_output_tokens,
            attempts=attempts,
        )

    # ---------- Anthropic provider ----------
    async def _call_anthropic(
        self,
        prompt: str,
        schema: type[T],
        system_instruction: str,
        temperature: float,
        max_tokens: int,
        attempts: int,
    ) -> T:
        base_url = settings.anthropic_base_url.rstrip("/")
        api_key = settings.anthropic_api_key.strip()
        model = settings.llm_model.strip() or "claude-sonnet-5"

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        # Include schema structure hint
        schema_hint = f"\n\nOutput STRICT JSON matching this schema: {json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
        curr_prompt = prompt + schema_hint

        last_error = None
        for attempt in range(1, attempts + 1):
            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_instruction,
                "messages": [{"role": "user", "content": curr_prompt}],
                "temperature": temperature,
            }

            try:
                async with httpx.AsyncClient(timeout=45.0) as client:
                    resp = await client.post(f"{base_url}/v1/messages", json=payload, headers=headers)
                    if resp.status_code != 200:
                        raise RuntimeError(f"Anthropic API returned {resp.status_code}: {resp.text[:300]}")
                    data = resp.json()
                    text = ""
                    for block in data.get("content", []):
                        if block.get("type") == "text":
                            text += block.get("text", "")

                    parsed = self._parse(text, schema)
                    if parsed is not None:
                        return parsed

                    log.warning("[%s] Anthropic JSON schema mismatch (attempt %d). Trying repair...", model, attempt)
                    curr_prompt = (
                        f"{prompt}\n\nYour previous reply was invalid JSON for the required schema. "
                        f"Reply with ONLY valid JSON for {schema.__name__}.\nPrevious text:\n{text[:1000]}"
                    )
            except Exception as exc:
                last_error = exc
                log.warning("Anthropic request failed (attempt %d): %s", attempt, exc)
                await asyncio.sleep(1.0 * attempt)

        raise RuntimeError(f"Anthropic call failed after {attempts} attempts: {last_error}")

    # ---------- Gemini provider ----------
    async def _next_gemini_key(self) -> _KeyState:
        async with self._lock:
            n = len(self._keys)
            if n == 0:
                raise NoKeysAvailable("No Gemini API keys configured")
            now = time.monotonic()
            for offset in range(n):
                st = self._keys[(self._cursor + offset) % n]
                if st.ready:
                    self._cursor = (self._cursor + offset + 1) % n
                    return st
            soonest = min(s.parked_until for s in self._keys) - now
            raise NoKeysAvailable(f"All {n} Gemini keys cooling down ({max(soonest, 0):.1f}s)")

    def _park_gemini(self, st: _KeyState) -> None:
        st.parked_until = time.monotonic() + _COOLDOWN_SECONDS
        masked = st.key[:6] + "…" + st.key[-4:] if len(st.key) > 12 else "***"
        log.warning("Parking Gemini key %s for %.0fs", masked, _COOLDOWN_SECONDS)

    async def _call_gemini(
        self,
        prompt: str,
        schema: type[T],
        system_instruction: str,
        temperature: float,
        max_tokens: int,
        attempts: int,
    ) -> T:
        import google.generativeai as genai

        model_name = settings.gemini_model or "gemini-2.5-flash"
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                st = await self._next_gemini_key()
            except NoKeysAvailable as exc:
                if attempt == attempts:
                    raise
                await asyncio.sleep(2.0)
                last_error = exc
                continue

            try:
                def _blocking():
                    genai.configure(api_key=st.key)
                    m = genai.GenerativeModel(
                        model_name,
                        system_instruction=system_instruction,
                        generation_config=genai.types.GenerationConfig(
                            temperature=temperature,
                            max_output_tokens=max_tokens,
                            response_mime_type="application/json",
                        ),
                    )
                    r = m.generate_content(prompt)
                    return (r.text or "").strip()

                text = await asyncio.to_thread(_blocking)
                parsed = self._parse(text, schema)
                if parsed is not None:
                    return parsed
            except Exception as exc:
                last_error = exc
                msg = str(exc).lower()
                if any(m in msg for m in _QUOTA_MARKERS):
                    self._park_gemini(st)
                    continue
                log.warning("Gemini call failed (attempt %d): %s", attempt, exc)
                await asyncio.sleep(1.5 * attempt)

        raise RuntimeError(f"Gemini call failed after {attempts} attempts: {last_error}")

    @staticmethod
    def _parse(text: str, schema: type[T]) -> T | None:
        for candidate in (text, _strip_fences(text), _extract_object(text)):
            if not candidate:
                continue
            try:
                return schema.model_validate(json.loads(candidate))
            except (json.JSONDecodeError, ValidationError):
                continue
        return None


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return m.group(1).strip() if m else ""


def _extract_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else ""


rotator = UnifiedLLMClient()
llm = rotator
