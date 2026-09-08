"""Polymarket trading adapter with a hard dry-run safety gate."""

from __future__ import annotations

import json
import re
from typing import Any


class PolymarketTradingError(RuntimeError):
    pass


class PolymarketTrader:
    """Use Polymarket's official Python SDK for authenticated account actions.

    Live order submission is disabled unless POLYMARKET_LIVE_TRADING is exactly
    "true". The execution Gemini role is still used in dry-run mode so the
    complete decision -> execution validation path can be tested safely.
    """

    def __init__(self, env: Any):
        self.env = env
        self.live = str(getattr(env, "POLYMARKET_LIVE_TRADING", "false")).lower() == "true"
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

        edge = float(decision.get("edge", 0) or 0)
        confidence = float(decision.get("confidence", 0) or 0)
        if edge < self.min_edge:
            return False, f"edge {edge:.4f} below minimum {self.min_edge:.4f}"
        if confidence < self.min_confidence:
            return False, f"confidence {confidence:.4f} below minimum {self.min_confidence:.4f}"

        price = current_price if current_price is not None else float(decision.get("market_probability", 0) or 0)
        if price < self.min_price or price > self.max_price:
            return False, f"entry price {price:.4f} outside [{self.min_price:.4f}, {self.max_price:.4f}]"
        return True, "risk checks passed"

    async def get_market(self, slug: str):
        if not slug:
            raise PolymarketTradingError("Market slug is required")
        try:
            from polymarket import AsyncSecureClient
        except ImportError as exc:
            raise PolymarketTradingError(
                "polymarket-client is not installed in the Worker build"
            ) from exc

        if not self.configured:
            raise PolymarketTradingError(
                "POLYMARKET_PRIVATE_KEY and POLYMARKET_WALLET_ADDRESS are required"
            )

        return await AsyncSecureClient.create(
            private_key=getattr(self.env, "POLYMARKET_PRIVATE_KEY"),
            wallet=getattr(self.env, "POLYMARKET_WALLET_ADDRESS"),
        )

    async def execute(self, market: dict[str, Any], execution_decision: dict[str, Any]) -> dict[str, Any]:
        parsed = self._parse_decision(execution_decision)
        action = str(parsed.get("action", "PASS")).upper()
        outcome = str(parsed.get("outcome", "")).strip()
        price = float(parsed.get("price", parsed.get("market_probability", 0)) or 0)
        size = float(parsed.get("size", 0) or 0)

        valid, reason = self._validate(parsed, price)
        if not valid:
            return {
                "executed": False,
                "live": self.live,
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
                "live": self.live,
                "action": action,
                "outcome": outcome,
                "reason": "computed order size is zero",
            }

        if not self.live:
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

        client = await self.get_market(str(market.get("slug") or ""))
        fetched_market = await client.get_market(slug=str(market.get("slug") or ""))
        token = fetched_market.outcomes.yes.token_id if action == "BUY_YES" else fetched_market.outcomes.no.token_id
        if not token:
            raise PolymarketTradingError(f"No token ID for outcome {outcome or action}")

        response = await client.place_limit_order(
            token_id=token,
            side="BUY",
            price=str(price),
            size=str(round(size, 2)),
        )
        if not response.ok:
            raise PolymarketTradingError(
                f"Polymarket order rejected: {response.code}: {response.message}"
            )

        return {
            "executed": True,
            "live": True,
            "simulated": False,
            "action": action,
            "outcome": outcome,
            "price": price,
            "size": round(size, 4),
            "amount_usd": round(size * price, 2),
            "order_id": response.order_id,
            "status": response.status,
            "trade_ids": list(response.trade_ids or []),
            "transaction_hashes": list(response.transactions_hashes or []),
        }
