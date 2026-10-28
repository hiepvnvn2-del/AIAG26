"""Conversation Memory Manager per Chat ID.

Stores the last 6-10 turns of dialogue, active SKU under discussion,
recently suggested pricing options, and cached competitor reports with TTL.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.config import settings
from app.schemas import PriceAnalysisReport

log = logging.getLogger("rpa.memory")


class ChatSession:
    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id
        self.messages: list[dict[str, Any]] = []
        self.active_sku: str | None = None
        self.suggested_options: dict[str, Any] = {}
        self.suggested_at: float = 0.0
        self.latest_scan_result: dict[str, Any] | None = None
        self.latest_scan_at: float = 0.0
        self.latest_url_report: PriceAnalysisReport | None = None
        self.latest_url_at: float = 0.0

    def add_message(self, role: str, text: str) -> None:
        self.messages.append({
            "role": role,
            "text": text,
            "timestamp": time.time(),
        })
        # Keep last 10 turns
        if len(self.messages) > 10:
            self.messages = self.messages[-10:]

    def get_recent(self, limit: int = 6) -> list[dict[str, str]]:
        return [{"role": m["role"], "text": m["text"]} for m in self.messages[-limit:]]

    def set_options(self, options: dict[str, Any]) -> None:
        self.suggested_options = options
        self.suggested_at = time.time()

    def get_valid_options(self, ttl_seconds: float = 1800.0) -> dict[str, Any]:
        if time.time() - self.suggested_at > ttl_seconds:
            return {}
        return self.suggested_options

    def set_scan(self, result: dict[str, Any]) -> None:
        self.latest_scan_result = result
        self.latest_scan_at = time.time()

    def get_valid_scan(self, ttl_seconds: float = 7200.0) -> dict[str, Any] | None:
        if not self.latest_scan_result:
            return None
        if time.time() - self.latest_scan_at > ttl_seconds:
            return None
        return self.latest_scan_result

    def set_url_report(self, report: PriceAnalysisReport) -> None:
        self.latest_url_report = report
        self.latest_url_at = time.time()

    def get_valid_url_report(self, ttl_seconds: float = 7200.0) -> PriceAnalysisReport | None:
        if not self.latest_url_report:
            return None
        if time.time() - self.latest_url_at > ttl_seconds:
            return None
        return self.latest_url_report


class ConversationMemory:
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}

    def get_session(self, chat_id: str) -> ChatSession:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = ChatSession(chat_id)
        return self._sessions[chat_id]

    def add_user_message(self, chat_id: str, text: str) -> None:
        self.get_session(chat_id).add_message("user", text)

    def add_assistant_message(self, chat_id: str, text: str) -> None:
        self.get_session(chat_id).add_message("assistant", text)

    def set_active_sku(self, chat_id: str, sku: str | None) -> None:
        self.get_session(chat_id).active_sku = sku

    def get_active_sku(self, chat_id: str) -> str | None:
        return self.get_session(chat_id).active_sku

    def set_suggested_options(self, chat_id: str, options: dict[str, Any]) -> None:
        self.get_session(chat_id).set_options(options)

    def get_suggested_options(self, chat_id: str) -> dict[str, Any]:
        return self.get_session(chat_id).get_valid_options()

    def set_scan_result(self, chat_id: str, result: dict[str, Any]) -> None:
        self.get_session(chat_id).set_scan(result)

    def get_scan_result(self, chat_id: str) -> dict[str, Any] | None:
        return self.get_session(chat_id).get_valid_scan()

    def set_url_report(self, chat_id: str, report: PriceAnalysisReport) -> None:
        self.get_session(chat_id).set_url_report(report)

    def get_url_report(self, chat_id: str) -> PriceAnalysisReport | None:
        return self.get_session(chat_id).get_valid_url_report()

    def get_context(self, chat_id: str, has_pending_hitl: bool = False) -> dict[str, Any]:
        sess = self.get_session(chat_id)
        has_cache = bool(sess.get_valid_scan() or sess.get_valid_url_report())
        cache_ts = None
        if sess.latest_url_report:
            cache_ts = time.strftime("%H:%M:%S", time.localtime(sess.latest_url_at))
        elif sess.latest_scan_result:
            cache_ts = time.strftime("%H:%M:%S", time.localtime(sess.latest_scan_at))

        return {
            "has_pending_hitl": has_pending_hitl,
            "has_competitor_cache": has_cache,
            "cache_timestamp": cache_ts,
            "active_sku": sess.active_sku,
            "last_suggested_options": sess.get_valid_options(),
            "recent_messages": sess.get_recent(6),
        }


# Singleton instance
memory = ConversationMemory()
