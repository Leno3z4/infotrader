"""Polymarket trading adapter with a hard dry-run safety gate.

The Cloudflare Python Worker uses Pyodide/WASM, so the native-heavy Polymarket
Python SDK is intentionally not bundled. Live execution remains disabled until
a Worker-compatible signing/execution adapter is provided.
"""

from __future__ import annotations

import json
import re
from typing import Any


class PolymarketTradingError(RuntimeError):
    pass


class PolymarketTrader:
    """Validate execution decisions and safely simulate orders in the Worker."""

    def __init__(self, env: Any):
        self.env = env
        requested_live = str(getattr(env, "POLYMARKET_LIVE_TRADING", "false")).lower() == "true"
        self.live = False
        self.live_requested = requested_live
        self.enabled = bool(
            getattr(env, "POLYMARKET_PRIVATE_KEY", None)
            and getattr(env, "POLYMARKET_WALLET_ADDRESS", None)
        )
        self.max_order_usd = float(getattr(env, "POLYMARKET_MAX_ORDER_USD", "10"))
        self.min_edge = float(getattr(env, "POLYMARKET_MIN_EDGE", "0.05"))
        self.min_confidence = float(getattr(env, "POLYMARKET_MIN_CONFIDENCE", "0.70"))
        self.max_price = float(getattr(env, "POLYMARKET_MAX_ENTRY_PRICE", "0.95"))
        self.min_price = float(getattr(env, "POLYMARKET_MIN_ENTRY_PRICE", "0.05"))

    @property
    def configured(self) -> bool:
        return self.enabled

    @staticmethod
    def _parse_decision(value: dict[str, Any]) -> dict[str, Any]:
        raw = value.get("text") if isinstance(value, dict) else value
        if not isinstance(raw, str):
            raise PolymarketTradingError("Execution input does not contain Gemini text")
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise PolymarketTradingError(f"Gemini decision is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise PolymarketTradingError("Gemini decision JSON is not an object")
        return parsed

    def _validate(self, decision: dict[str, Any], current_price: float | None = None) -> tuple[bool, str]:
        action = str(decision.get("action", "PASS")).upper()
        if action not in {"BUY_YES", "BUY_NO", "PASS"}:
            return False, "unsupported action"
        if action == "PASS":
            return False, "execution decision is PASS"

        try:
            edge = float(decision.get("edge", 0) or 0)
            confidence = float(decision.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            return False, "edge/confidence must be numeric"
        if edge < self.min_edge:
            return False, f"edge {edge:.4f} below minimum {self.min_edge:.4f}"
        if confidence < self.min_confidence:
            return False, f"confidence {confidence:.4f} below minimum {self.min_confidence:.4f}"

        try:
            price = current_price if current_price is not None else float(decision.get("market_probability", 0) or 0)
        except (TypeError, ValueError):
            return False, "entry price must be numeric"
        if price < self.min_price or price > self.max_price:
            return False, f"entry price {price:.4f} outside [{self.min_price:.4f}, {self.max_price:.4f}]"
        return True, "risk checks passed"

    async def execute(self, market: dict[str, Any], execution_decision: dict[str, Any]) -> dict[str, Any]:
        parsed = self._parse_decision(execution_decision)
        action = str(parsed.get("action", "PASS")).upper()
        outcome = str(parsed.get("outcome", "")).strip()
        try:
            price = float(parsed.get("price", parsed.get("market_probability", 0)) or 0)
            size = float(parsed.get("size", 0) or 0)
        except (TypeError, ValueError):
            return {"executed": False, "live": False, "action": action, "outcome": outcome, "reason": "invalid numeric order parameters"}

        valid, reason = self._validate(parsed, price)
        if not valid:
            return {
                "executed": False,
                "live": False,
                "action": action,
                "outcome": outcome,
                "reason": reason,
            }

        if size <= 0:
            requested_usd = min(self.max_order_usd, float(parsed.get("amount_usd", self.max_order_usd) or self.max_order_usd))
            size = requested_usd / price
        if size * price > self.max_order_usd:
            size = self.max_order_usd / price
        if size <= 0:
            return {
                "executed": False,
                "live": False,
                "action": action,
                "outcome": outcome,
                "reason": "computed order size is zero",
            }

        if self.live_requested:
            return {
                "executed": False,
                "live": False,
                "simulated": True,
                "action": action,
                "outcome": outcome,
                "price": price,
                "size": round(size, 4),
                "amount_usd": round(size * price, 2),
                "reason": "live trading requested but disabled: Polymarket native SDK is not Pyodide-compatible in this Worker",
                "next_step": "Use a Worker-compatible signing/execution adapter or a dedicated JS trading service.",
            }

        return {
            "executed": False,
            "live": False,
            "simulated": True,
            "action": action,
            "outcome": outcome,
            "price": price,
            "size": round(size, 4),
            "amount_usd": round(size * price, 2),
            "reason": "dry-run: live trading disabled",
        }
