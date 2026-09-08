"""Production orchestration entrypoint: research -> decision -> execution gate."""

from __future__ import annotations

import json

from entry_crypto import Default as CryptoDefault
from gemini import GeminiRotator
from polymarket_trader import PolymarketTrader


class Default(CryptoDefault):
    """Preserve existing scanners while adding the execution-review stage."""

    async def scan_polymarket(self, store) -> dict:
        payload = await super().scan_polymarket(store)
        decisions = payload.get("decisions") or []
        selected = payload.get("selected") or []

        execution_results: list[dict] = []
        try:
            execution_client = GeminiRotator(self.env, "EXECUTION")
        except Exception as exc:
            execution_client = None
            execution_results.append({"status": "unavailable", "error": str(exc)})

        trader = PolymarketTrader(self.env)

        for decision_item in decisions[:5]:
            if not execution_client:
                break
            subject = decision_item.get("subject") or "unknown market"
            market = next(
                (item for item in selected if (item.get("question") or item.get("slug")) == subject),
                None,
            )
            if not market:
                execution_results.append({
                    "subject": subject,
                    "status": "rejected",
                    "reason": "market not found in selected scan set",
                })
                continue

            decision_text = decision_item.get("text")
            if not decision_text:
                execution_results.append({
                    "subject": subject,
                    "status": "rejected",
                    "reason": "decision output did not contain text",
                })
                continue

            execution_prompt = json.dumps({
                "task": "Validate whether this Polymarket decision is safe to execute and propose exact order parameters.",
                "market": market,
                "decision": decision_text,
                "execution_mode": "live" if trader.live else "dry_run",
                "risk_limits": {
                    "max_order_usd": trader.max_order_usd,
                    "min_edge": trader.min_edge,
                    "min_confidence": trader.min_confidence,
                    "min_entry_price": trader.min_price,
                    "max_entry_price": trader.max_price,
                },
                "output_schema": {
                    "action": "BUY_YES | BUY_NO | PASS",
                    "outcome": "exact outcome label or empty string",
                    "price": "current proposed entry price between 0 and 1",
                    "size": "number of shares, 0 when PASS",
                    "amount_usd": "USD spend, must respect max_order_usd",
                    "reason": "concise execution validation reason",
                },
                "rules": [
                    "Never place or claim to have placed an order.",
                    "PASS if the market moved materially, evidence is stale, liquidity is insufficient, or limits are violated.",
                    "Use only the exact market/outcome supplied in the market object.",
                ],
            })

            try:
                execution_decision = await execution_client.execute(execution_prompt)
                result = await trader.execute(market, execution_decision)
                result["subject"] = subject
                result["execution_key_role"] = "GEMINI_EXECUTION"
                execution_results.append(result)
                try:
                    await store.record_decision("polymarket_execution", subject, result)
                except Exception as exc:
                    result["storage_error"] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                execution_results.append({
                    "subject": subject,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "execution_key_role": "GEMINI_EXECUTION",
                })

        payload["execution"] = execution_results
        payload["polymarket_trading_configured"] = trader.configured
        payload["polymarket_live_trading"] = trader.live
        return payload

    def format_alert(self, scan: str, payload: dict) -> str:
        if scan != "polymarket":
            return super().format_alert(scan, payload)

        mode = "LIVE" if payload.get("polymarket_live_trading") else "DRY RUN"
        lines = [
            f"InfoTrader Polymarket scan ({mode})",
            f"Markets scanned: {payload.get('markets_seen', 0)}",
            f"Premier League matches: {payload.get('premier_league_markets', 0)}",
            f"Gemini research: {len(payload.get('research', []))}",
            f"AI decisions: {len(payload.get('decisions', []))}",
            "",
            "Top markets:",
        ]
        for item in (payload.get("selected") or [])[:5]:
            prices = item.get("outcome_prices") or []
            outcomes = item.get("outcomes") or []
            price_text = []
            for i in range(min(len(prices), len(outcomes))):
                try:
                    price_text.append(f"{outcomes[i]} {float(prices[i]):.1%}")
                except (TypeError, ValueError):
                    pass
            lines.append(
                f"• {item.get('question') or item.get('slug')}\n"
                f"  Liquidity: ${item.get('liquidity', 0):,.0f} | 24h vol: ${item.get('volume_24h', 0):,.0f}"
                + (f"\n  Prices: {', '.join(price_text)}" if price_text else "")
            )

        executions = payload.get("execution") or []
        if executions:
            lines.extend(["", "Execution review:"])
            for item in executions[:5]:
                amount = item.get("amount_usd", 0) or 0
                lines.append(
                    f"• {item.get('subject', 'unknown')}: "
                    f"{item.get('action', 'PASS')} | ${amount:,.2f} | "
                    f"{item.get('reason', item.get('error', 'no reason'))}"
                )

        return "\n".join(lines)[:3900]
