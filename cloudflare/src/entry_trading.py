"""Production orchestration entrypoint: targeted sports/weather research -> decision -> execution gate."""
from __future__ import annotations

import json
import math
from urllib.parse import quote_plus

from workers import fetch

from entry_crypto import Default as CryptoDefault
from gemini import GeminiRotator
from polymarket_trader import PolymarketTrader
from scanner_helpers import normalize_market, safe_float
from sports_context import build_context, classify_market

POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
SPORTS_QUERIES = ("Premier League", "NBA", "NFL", "MLB", "NHL", "UFC", "tennis", "Formula 1", "cricket", "rugby", "golf", "WNBA")
WEATHER_QUERIES = ("weather", "temperature", "rain", "snow", "wind", "hurricane", "storm")

async def fetch_json(url: str, **options):
    response = await fetch(url, **options)
    if not response.ok:
        raise RuntimeError(f"upstream HTTP {response.status} for {url}")
    return await response.json()

def _search_markets(payload: object) -> list[dict]:
    rows: list[dict] = []
    if not isinstance(payload, dict):
        return rows
    if isinstance(payload.get("markets"), list):
        rows.extend(x for x in payload["markets"] if isinstance(x, dict))
    for event in payload.get("events", []) or []:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets", []) or []:
            if isinstance(market, dict):
                row = {**market}
                row.setdefault("eventId", event.get("id"))
                row.setdefault("category", event.get("category"))
                rows.append(row)
    result: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        normalized = normalize_market(row)
        question = normalized.get("question")
        if not question:
            continue
        normalized.update({
            "liquidity": safe_float(row.get("liquidity", normalized.get("liquidity"))),
            "volume_24h": safe_float(row.get("volume24hr", row.get("volume24h", normalized.get("volume_24h")))),
            "open_interest": safe_float(row.get("openInterest", 0)),
            "active": bool(row.get("active", True)),
            "closed": bool(row.get("closed", False)),
            "category": row.get("category"),
            "description": row.get("description"),
            "event_id": row.get("eventId"),
        })
        key = str(normalized.get("id") or normalized.get("condition_id") or question)
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result

def _liquidity_score(market: dict) -> float:
    return (
        math.log1p(max(0.0, safe_float(market.get("liquidity")))) * 60
        + math.log1p(max(0.0, safe_float(market.get("volume_24h")))) * 35
        + math.log1p(max(0.0, safe_float(market.get("open_interest")))) * 5
    )

class Default(CryptoDefault):
    async def _discover_polymarket(self) -> list[dict]:
        found: list[dict] = []
        for query in SPORTS_QUERIES + WEATHER_QUERIES:
            url = f"{POLYMARKET_SEARCH_URL}?q={quote_plus(query)}&limit_per_type=40&events_status=active&search_tags=true"
            try:
                found.extend(_search_markets(await fetch_json(url)))
            except Exception as exc:
                print(f"POLYMARKET SEARCH ERROR: {query}: {type(exc).__name__}: {exc}")
        unique: dict[str, dict] = {}
        for market in found:
            if market.get("closed") or not market.get("active"):
                continue
            kind = classify_market(str(market.get("question") or ""))
            if kind not in {"sports", "weather"}:
                continue
            market["market_kind"] = kind
            market["liquidity_score"] = _liquidity_score(market)
            key = str(market.get("id") or market.get("condition_id") or market["question"])
            unique[key] = market
        return sorted(unique.values(), key=lambda m: m.get("liquidity_score", 0), reverse=True)

    @staticmethod
    def _research_prompt(market: dict, context: dict, history: list[dict]) -> str:
        return json.dumps({
            "task": "Research this active Polymarket sports or weather market for a probabilistic trading decision.",
            "market": market,
            "database_context": context,
            "previous_infotrader_decisions": history[-10:],
            "requirements": [
                "Use current web research and identify important sources and publication dates.",
                "For sports, combine historical results, team data and player roster context with current injuries, suspensions, lineup/player availability, form, standings, matchup and advanced statistics.",
                "For weather, verify the current forecast for the relevant place/time and its uncertainty.",
                "Treat current Polymarket prices as the market-implied baseline, not as proof.",
                "Separate facts from estimates and never fabricate statistics, injuries, weather values or sources.",
            ],
        })

    @staticmethod
    def _decision_prompt(market: dict, research: dict) -> str:
        return json.dumps({
            "task": "Decide whether this Polymarket sports/weather market has a meaningful edge after research.",
            "market": market,
            "research": research,
            "output_schema": {
                "action": "BUY_YES | BUY_NO | PASS",
                "outcome": "exact outcome label or empty string",
                "fair_probability": "0..1", "market_probability": "0..1", "edge": "estimated edge",
                "confidence": "0..1", "reason": "concise evidence-based reason",
            },
            "rules": ["PASS without a meaningful evidence-backed edge.", "Do not invent prices.", "Reduce confidence for stale news, missing lineups, injuries or weather uncertainty."],
        })

    async def _research_and_decide(self, store, selected: list[dict]):
        research_results: list[dict] = []
        decisions: list[dict] = []
        try:
            history = await store.recent("recent:decisions", 10)
        except Exception:
            history = []
        try:
            research_client = GeminiRotator(self.env, "RESEARCH")
            decision_client = GeminiRotator(self.env, "DECISION")
        except Exception as exc:
            return [{"error": f"Gemini unavailable: {exc}"}], [], len(history)
        for market in selected[:3]:
            subject = market.get("question") or market.get("slug") or "unknown market"
            try:
                context = await build_context(fetch_json, subject)
                research = await research_client.research(self._research_prompt(market, context, history))
                research_results.append({"subject": subject, "market_kind": market.get("market_kind"), "context": context, **research})
                await store.record_research("polymarket", subject, research)
                if research.get("text"):
                    decision = await decision_client.decide(self._decision_prompt(market, research))
                    decisions.append({"subject": subject, "market_kind": market.get("market_kind"), **decision})
                    await store.record_decision("polymarket", subject, decision)
            except Exception as exc:
                research_results.append({"subject": subject, "error": f"{type(exc).__name__}: {exc}"})
        return research_results, decisions, len(history)

    async def scan_polymarket(self, store) -> dict:
        markets = await self._discover_polymarket()
        max_markets = int(getattr(self.env, "MAX_MARKETS_PER_RUN", "8"))
        selected = markets[:max_markets]
        research, decisions, history_count = await self._research_and_decide(store, selected)
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
            market = next((m for m in selected if (m.get("question") or m.get("slug")) == subject), None)
            decision_text = decision_item.get("text")
            if not market or not decision_text:
                continue
            prompt = json.dumps({
                "task": "Validate this decision for safe execution; PASS when stale, illiquid or outside limits.",
                "market": market, "decision": decision_text,
                "execution_mode": "live" if trader.live else "dry_run",
                "risk_limits": {"max_order_usd": trader.max_order_usd, "min_edge": trader.min_edge, "min_confidence": trader.min_confidence, "min_entry_price": trader.min_price, "max_entry_price": trader.max_price},
                "output_schema": {"action": "BUY_YES | BUY_NO | PASS", "outcome": "exact outcome label", "price": "0..1", "size": "shares", "amount_usd": "USD spend", "reason": "concise reason"},
            })
            try:
                result = await trader.execute(market, await execution_client.execute(prompt))
                result["subject"] = subject
                execution_results.append(result)
                await store.record_decision("polymarket_execution", subject, result)
            except Exception as exc:
                execution_results.append({"subject": subject, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return {
            "source": "Polymarket public-search + TheSportsDB + Open-Meteo + Gemini",
            "markets_seen": len(markets),
            "sports_markets": sum(m.get("market_kind") == "sports" for m in markets),
            "weather_markets": sum(m.get("market_kind") == "weather" for m in markets),
            "premier_league_markets": sum("premier league" in str(m.get("question") or "").lower() for m in markets),
            "selected": selected, "research": research, "decisions": decisions, "execution": execution_results,
            "historical_decision_records_used": history_count,
            "sports_data_source": "TheSportsDB free v1", "weather_data_source": "Open-Meteo",
            "polymarket_trading_configured": trader.configured, "polymarket_live_trading": trader.live,
            "live_execution": False,
        }

    def format_alert(self, scan: str, payload: dict) -> str:
        if scan != "polymarket":
            return super().format_alert(scan, payload)
        mode = "LIVE" if payload.get("polymarket_live_trading") else "DRY RUN"
        lines = [
            f"InfoTrader Polymarket sports/weather scan ({mode})",
            f"Markets: {payload.get('markets_seen', 0)} | Sports: {payload.get('sports_markets', 0)} | Weather: {payload.get('weather_markets', 0)}",
            f"Premier League: {payload.get('premier_league_markets', 0)} | Research: {len(payload.get('research', []))} | Decisions: {len(payload.get('decisions', []))}",
            "", "Top high-liquidity markets:",
        ]
        for item in (payload.get("selected") or [])[:5]:
            lines.append(f"• [{str(item.get('market_kind', 'market')).upper()}] {item.get('question') or item.get('slug')}\n  Liquidity: ${item.get('liquidity', 0):,.0f} | 24h vol: ${item.get('volume_24h', 0):,.0f}")
        return "\n".join(lines)[:3900]
