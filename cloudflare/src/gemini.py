"""Worker-native Gemini REST client with role-based key rotation."""

from __future__ import annotations

import json
from typing import Any

from workers import fetch


class GeminiError(RuntimeError):
    pass


DEFAULT_ROLE_KEYS = {
    "RESEARCH": ("GEMINI_API_KEY_1", "GEMINI_API_KEY_2"),
    "DECISION": ("GEMINI_API_KEY_3", "GEMINI_API_KEY_4"),
    "EXECUTION": ("GEMINI_API_KEY_5",),
}


class GeminiRotator:
    """Fail over across Gemini API keys assigned to a specific role.

    Explicit GEMINI_<ROLE>_KEYS values override the defaults. With the default
    layout, keys 1-2 are research, 3-4 are decision, and 5 is execution.
    """

    def __init__(self, env: Any, role: str):
        self.env = env
        self.role = role.upper()
        configured = [
            name.strip()
            for name in str(getattr(env, f"GEMINI_{self.role}_KEYS", "") or "").split(",")
            if name.strip()
        ]
        names = configured or list(DEFAULT_ROLE_KEYS.get(self.role, ()))
        keys = [getattr(env, name, None) for name in names]
        if not keys:
            fallback = getattr(env, "GEMINI_API_KEY", None)
            if fallback:
                keys = [fallback]
        self.key_names = [name for name, key in zip(names, keys) if key]
        self.keys = [key for key in keys if key]
        if not self.keys:
            raise GeminiError(f"No Gemini credentials configured for role {role!r}")
        self.model = getattr(env, "GEMINI_MODEL", "gemini-3.8-flash")

    @staticmethod
    def _text(data: dict[str, Any]) -> str:
        for candidate in data.get("candidates", []) or []:
            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
            if text.strip():
                return text.strip()
        raise GeminiError("Gemini returned no text")

    async def generate(self, prompt: str, *, research: bool = False, temperature: float = 0.2) -> dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload: dict[str, Any] = {
            "system_instruction": {
                "parts": [{"text": self._system_instruction(self.role, research)}]
            },
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 1600,
            },
        }
        if research:
            payload["tools"] = [{"google_search": {}}]

        last_error: str | None = None
        for index, key in enumerate(self.keys):
            response = await fetch(
                url,
                method="POST",
                headers={
                    "content-type": "application/json",
                    "x-goog-api-key": key,
                },
                body=json.dumps(payload),
            )
            data = await response.json()
            if response.ok:
                return {
                    "text": self._text(data),
                    "role": self.role,
                    "key_slot": self.key_names[index] if index < len(self.key_names) else None,
                    "grounding_metadata": (data.get("candidates", [{}])[0] or {}).get("groundingMetadata"),
                }
            last_error = f"HTTP {response.status}: {data}"
            if response.status not in (401, 403, 429, 500, 502, 503):
                break

        raise GeminiError(last_error or "Gemini request failed")

    @staticmethod
    def _system_instruction(role: str, research: bool) -> str:
        if role == "RESEARCH" or research:
            return (
                "You are InfoTrader's research analyst. Use Google Search grounding when useful. "
                "Prefer recent, primary, official and reputable sources. Separate facts from inference, "
                "include dates, and never invent injuries, odds, prices, trades, or sources."
            )
        if role == "EXECUTION":
            return (
                "You are InfoTrader's execution risk analyst. You do not have wallet access and you never place trades. "
                "Review a proposed Polymarket decision plus current market constraints. Return JSON only with fields: "
                "action (BUY_YES|BUY_NO|PASS), outcome, price, size, amount_usd, reason. Reject stale, overpriced, "
                "illiquid, undersized, or insufficient-edge trades. Never claim an order was submitted."
            )
        return (
            "You are InfoTrader's decision analyst. Given a market and research packet, estimate fair probability, "
            "identify edge versus the market price, and recommend PASS unless the evidence supports a meaningful edge. "
            "Return JSON only. Do not claim a trade was executed."
        )

    async def research(self, prompt: str) -> dict[str, Any]:
        return await self.generate(prompt, research=True, temperature=0.15)

    async def decide(self, prompt: str) -> dict[str, Any]:
        return await self.generate(prompt, research=False, temperature=0.1)

    async def execute(self, prompt: str) -> dict[str, Any]:
        return await self.generate(prompt, research=False, temperature=0.05)
