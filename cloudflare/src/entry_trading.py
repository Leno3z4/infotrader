"""Production orchestration entrypoint: targeted sports/weather research -> decision -> execution gate."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from urllib.parse import quote_plus

from workers import fetch

from entry_crypto import Default as CryptoDefault
from gemini import GeminiRotator
from polymarket_trader import PolymarketTrader
from scanner_helpers import normalize_market, safe_float
from sports_context import build_context, classify_market

POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
POLYMARKET_SPORTS_URL = "https://gamma-api.polymarket.com/sports"
POLYMARKET_EVENTS_URL = "https://gamma-api.polymarket.com/events"
SPORTS_QUERIES = (
    "Premier League", "EPL", "Premier League soccer", "NBA", "NFL", "MLB", "NHL", "UFC", "tennis", "cricket", "Formula 1",
)
EPL_SEARCH_QUERIES = ("Premier League", "EPL", "Premier League soccer")
WEATHER_QUERIES = ("weather", "temperature", "rain", "snow")
SEARCH_LIMIT_PER_TYPE = 40
EPL_EVENT_LIMIT = 100
DAILY_TRADE_LIMITS = {"premier_league": 2, "weather": 1}


def _text_blob(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in ("question", "description", "eventTitle", "event_title", "slug", "category", "seriesSlug", "series_slug")).lower()


async def fetch_json(url: str, **options):
    response = await fetch(url, **options)
    if not response.ok:
        raise RuntimeError(f"upstream HTTP {response.status} for {url}")
    return await response.json()


def _search_markets(payload: object) -> list[dict]:
    rows: list[dict] = []
    if isinstance(payload, list):
        rows.extend(x for x in payload if isinstance(x, dict))
    elif isinstance(payload, dict):
        rows.extend(x for x in payload.get("markets", []) or [] if isinstance(x, dict))
        for event in payload.get("events", []) or []:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets", []) or []:
                if isinstance(market, dict):
                    row = {**market}
                    row.setdefault("eventId", event.get("id"))
                    row.setdefault("category", event.get("category"))
                    row.setdefault("endDate", event.get("endDate"))
                    row.setdefault("description", event.get("description"))
                    row.setdefault("eventTitle", event.get("title"))
                    row.setdefault("eventSlug", event.get("slug"))
                    rows.append(row)
    result: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        normalized = normalize_market(row)
        question = normalized.get("question") or row.get("eventTitle") or row.get("title")
        if not question:
            continue
        normalized["question"] = question
        accepting = row.get("acceptingOrders", row.get("enableOrderBook", normalized.get("accepting_orders", False)))
        normalized.update({
            "liquidity": safe_float(row.get("liquidity", normalized.get("liquidity"))),
            "volume_24h": safe_float(row.get("volume24hr", row.get("volume_24h", normalized.get("volume_24h")))),
            "open_interest": safe_float(row.get("openInterest", 0)),
            "active": bool(row.get("active", True)),
            "closed": bool(row.get("closed", False)),
            "accepting_orders": bool(accepting),
            "is_open": bool(row.get("active", True)) and not bool(row.get("closed", False)) and bool(accepting),
            "category": row.get("category"),
            "description": row.get("description"),
            "event_id": row.get("eventId"),
            "event_title": row.get("eventTitle"),
            "event_slug": row.get("eventSlug"),
            "end_date": row.get("endDate", normalized.get("end_date")),
            "slug": row.get("slug", normalized.get("slug")),
        })
        key = str(normalized.get("id") or normalized.get("condition_id") or question)
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _hours_to_end(market: dict) -> float | None:
    raw = market.get("end_date")
    if not raw:
        return None
    try:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return None


def _is_event_market(market: dict) -> bool:
    text = _text_blob(market)
    if any(term in text for term in ("season winner", "champion", "mvp", "top goalscorer", "top scorer", "win the league", "make the playoffs", "playoff qualification", "regular season", "cup winner", "tournament winner")):
        return False
    return bool(re.search(r"(?:\bvs\.?\b|\bv\b|\bat\b|@)", text))


def _researchability_score(market: dict) -> float:
    question = str(market.get("question") or "").lower()
    score = 55.0 if market.get("is_event_market") else 0.0
    if any(term in question for term in ("injury", "injuries", "lineup", "starting xi", "roster", "player")):
        score += 10.0
    if market.get("market_kind") == "weather":
        score += 45.0
    if any(term in question for term in ("nba", "nfl", "mlb", "nhl", "ufc", "tennis", "atp", "wta", "premier league", "soccer", "football", "cricket", "formula 1")):
        score += 10.0
    return min(score, 100.0)


def _horizon_score(hours: float | None) -> float:
    if hours is None or hours <= 0:
        return 0.0
    if 6 <= hours <= 36:
        return 100.0
    if hours < 6:
        return 78.0
    if hours <= 72:
        return 72.0
    if hours <= 168:
        return 38.0
    if hours <= 336:
        return 12.0
    return 0.0


def _market_opportunity_score(market: dict, liquidity_max: float, volume_max: float) -> float:
    liquidity = math.log1p(max(0.0, safe_float(market.get("liquidity"))))
    volume = math.log1p(max(0.0, safe_float(market.get("volume_24h"))))
    liq_norm = 100.0 * liquidity / max(liquidity_max, 1.0)
    vol_norm = 100.0 * volume / max(volume_max, 1.0)
    activity_score = min(100.0, vol_norm * 0.8 + (20.0 if market.get("is_open") else 0.0))
    event_score = 100.0 if market.get("is_event_market") else 0.0
    return _horizon_score(market.get("hours_to_end")) * 0.40 + event_score * 0.20 + _researchability_score(market) * 0.15 + liq_norm * 0.15 + activity_score * 0.10


def _trade_bucket(market: dict) -> str | None:
    question = _text_blob(market)
    if market.get("market_kind") == "weather":
        return "weather"
    if market.get("premier_league") or "premier league" in question or "premier-league" in question or "english premier league" in question:
        return "premier_league"
    return None


def _eligible_trade_candidates(markets: list[dict]) -> list[dict]:
    return [market for market in markets if _trade_bucket(market) is not None]


def _utc_trade_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _daily_trade_usage(store) -> dict:
    default = {"premier_league": 0, "weather": 0, "total": 0}
    try:
        usage = await store.get_json(f"trading:daily:{_utc_trade_day()}", default)
        if not isinstance(usage, dict):
            return default
        return {"premier_league": max(0, int(usage.get("premier_league", 0) or 0)), "weather": max(0, int(usage.get("weather", 0) or 0)), "total": max(0, int(usage.get("total", 0) or 0))}
    except Exception:
        return default


async def _record_daily_trade(store, bucket: str) -> dict:
    usage = await _daily_trade_usage(store)
    if bucket in DAILY_TRADE_LIMITS:
        usage[bucket] += 1
    usage["total"] = usage["premier_league"] + usage["weather"]
    await store.put_json(f"trading:daily:{_utc_trade_day()}", usage)
    return usage


async def _discover_premier_league() -> list[dict]:
    """Use every stable Gamma route we have: metadata, series events, direct search and event search."""
    found: list[dict] = []
    structured_count = 0
    try:
        sports = await fetch_json(POLYMARKET_SPORTS_URL)
        entries = sports if isinstance(sports, list) else (sports.get("sports", []) if isinstance(sports, dict) else [])
        epl_entries = [item for item in entries if isinstance(item, dict) and (str(item.get("sport", "")).lower() in {"epl", "premier league", "soccer"} or "premier league" in str(item.get("name", "")).lower() or "premier league" in str(item.get("title", "")).lower() or "epl" == str(item.get("slug", "")).lower())]
        for epl in epl_entries[:3]:
            series_ids = []
            for field in ("series", "seriesId", "series_id", "id"):
                value = epl.get(field)
                if isinstance(value, list):
                    series_ids.extend(str(x) for x in value if x)
                elif value:
                    series_ids.append(str(value))
            for series_id in dict.fromkeys(series_ids):
                for param in ("series_id", "seriesId"):
                    try:
                        events = await fetch_json(f"{POLYMARKET_EVENTS_URL}?{param}={quote_plus(series_id)}&active=true&closed=false&limit={EPL_EVENT_LIMIT}")
                        rows = _search_markets(events)
                        found.extend(rows)
                        structured_count += len(rows)
                    except Exception as exc:
                        print(f"POLYMARKET EPL EVENT ERROR: {param}={series_id}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"POLYMARKET EPL SPORTS ERROR: {type(exc).__name__}: {exc}")

    direct_count = 0
    for query in EPL_SEARCH_QUERIES:
        try:
            payload = await fetch_json(f"{POLYMARKET_SEARCH_URL}?q={quote_plus(query)}&limit_per_type={SEARCH_LIMIT_PER_TYPE}&events_status=active&search_tags=true")
            rows = _search_markets(payload)
            found.extend(rows)
            direct_count += len(rows)
        except Exception as exc:
            print(f"POLYMARKET EPL SEARCH ERROR: {query}: {type(exc).__name__}: {exc}")

    # Search the current fixture's team names too; many match markets omit the words
    # "Premier League" from the question and therefore evade a league-name query.
    team_queries = ("Aston Villa", "Nottingham Forest", "Bournemouth", "Brentford", "Chelsea", "Hull City", "Crystal Palace", "Ipswich", "Liverpool", "Fulham", "Tottenham", "Everton", "Sunderland", "Arsenal", "Coventry", "Brighton", "Manchester United", "Manchester City", "Leeds", "Newcastle")
    for query in team_queries:
        try:
            payload = await fetch_json(f"{POLYMARKET_SEARCH_URL}?q={quote_plus(query)}&limit_per_type=20&events_status=active&search_tags=true")
            rows = _search_markets(payload)
            for row in rows:
                text = _text_blob(row)
                if any(team.lower() in text for team in team_queries) and _is_event_market(row):
                    found.append(row)
        except Exception as exc:
            print(f"POLYMARKET EPL TEAM SEARCH ERROR: {query}: {type(exc).__name__}: {exc}")

    unique: dict[str, dict] = {}
    for market in found:
        if market.get("closed") or not market.get("active"):
            continue
        text = _text_blob(market)
        if "premier league" in text or "premier-league" in text or any(term in text for term in ("aston villa", "nottingham forest", "bournemouth", "brentford", "chelsea", "hull city", "crystal palace", "ipswich", "liverpool", "fulham", "tottenham", "everton", "sunderland", "arsenal", "coventry", "brighton", "manchester united", "manchester city", "leeds", "newcastle")):
            market["market_kind"] = "sports"
            market["premier_league"] = True
            market["epl_discovery"] = "structured_or_team_fallback"
            key = str(market.get("id") or market.get("condition_id") or market.get("question"))
            unique[key] = market
    print(f"POLYMARKET EPL DISCOVERY: structured={structured_count} direct={direct_count} unique={len(unique)}")
    return list(unique.values())


class Default(CryptoDefault):
    async def _discover_polymarket(self) -> list[dict]:
        found = await _discover_premier_league()
        for query in SPORTS_QUERIES[3:] + WEATHER_QUERIES:
            try:
                found.extend(_search_markets(await fetch_json(f"{POLYMARKET_SEARCH_URL}?q={quote_plus(query)}&limit_per_type={SEARCH_LIMIT_PER_TYPE}&events_status=active&search_tags=true")))
            except Exception as exc:
                print(f"POLYMARKET SEARCH ERROR: {query}: {type(exc).__name__}: {exc}")
        unique: dict[str, dict] = {}
        for market in found:
            if market.get("closed") or not market.get("active"):
                continue
            kind = classify_market(str(market.get("question") or ""))
            if market.get("premier_league"):
                kind = "sports"
            if kind not in {"sports", "weather"}:
                continue
            market["market_kind"] = kind
            market["hours_to_end"] = _hours_to_end(market)
            market["is_event_market"] = _is_event_market(market)
            text = _text_blob(market)
            if not market.get("premier_league"):
                market["premier_league"] = "premier league" in text or "premier-league" in text
            key = str(market.get("id") or market.get("condition_id") or market["question"])
            if key not in unique or market.get("premier_league"):
                unique[key] = market
        markets = list(unique.values())
        liq_max = max((math.log1p(max(0.0, safe_float(m.get("liquidity")))) for m in markets), default=1.0)
        volume_max = max((math.log1p(max(0.0, safe_float(m.get("volume_24h")))) for m in markets), default=1.0)
        for market in markets:
            market["opportunity_score"] = _market_opportunity_score(market, liq_max, volume_max)
            market["liquidity_score"] = market["opportunity_score"]
        markets.sort(key=lambda m: (m.get("opportunity_score", 0), safe_float(m.get("volume_24h")), safe_float(m.get("liquidity"))), reverse=True)
        return markets

    @staticmethod
    def _research_prompt(market: dict, context: dict, history: list[dict]) -> str:
        return json.dumps({"task": "Research this active Polymarket sports or weather market for a probabilistic trading decision.", "market": market, "database_context": context, "previous_infotrader_decisions": history[-10:], "requirements": ["Use current web research and identify important sources and publication dates.", "For sports, combine historical results, team data and player roster context with current injuries, suspensions, lineup/player availability, form, standings, matchup and advanced statistics.", "For weather, verify the current forecast for the relevant place/time and its uncertainty.", "Treat current Polymarket prices as the market-implied baseline, not as proof.", "Separate facts from estimates and never fabricate statistics, injuries, weather values or sources."]})

    @staticmethod
    def _decision_prompt(market: dict, research: dict) -> str:
        return json.dumps({"task": "Decide whether this Polymarket sports/weather market has a meaningful edge after research.", "market": market, "research": research, "output_schema": {"action": "BUY_YES | BUY_NO | PASS", "outcome": "exact outcome label or empty string", "fair_probability": "0..1", "market_probability": "0..1", "edge": "estimated edge", "confidence": "0..1", "reason": "concise evidence-based reason"}, "rules": ["PASS without a meaningful evidence-backed edge.", "Do not invent prices.", "Reduce confidence for stale news, missing lineups, injuries or weather uncertainty."]})

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
        max_markets = max(1, int(getattr(self.env, "MAX_MARKETS_PER_RUN", "8")))
        pl_markets = [m for m in markets if _trade_bucket(m) == "premier_league"]
        weather_markets = [m for m in markets if _trade_bucket(m) == "weather"]
        # Always surface EPL in the research/display set when any EPL market exists.
        selected = []
        for market in pl_markets[:2] + weather_markets[:1]:
            if market not in selected:
                selected.append(market)
        for market in markets:
            if len(selected) >= max_markets:
                break
            if market not in selected:
                selected.append(market)
        trade_candidates = _eligible_trade_candidates(markets)
        usage = await _daily_trade_usage(store)
        trade_selected: list[dict] = []
        bucket_counts = {"premier_league": 0, "weather": 0}
        for market in trade_candidates:
            bucket = _trade_bucket(market)
            if bucket is None or bucket_counts[bucket] >= DAILY_TRADE_LIMITS[bucket] or usage.get(bucket, 0) >= DAILY_TRADE_LIMITS[bucket]:
                continue
            trade_selected.append(market)
            bucket_counts[bucket] += 1
            if len(trade_selected) >= 3:
                break
        research, decisions, history_count = await self._research_and_decide(store, trade_selected)
        execution_results: list[dict] = []
        try:
            execution_client = GeminiRotator(self.env, "EXECUTION")
        except Exception as exc:
            execution_client = None
            execution_results.append({"status": "unavailable", "error": str(exc)})
        trader = PolymarketTrader(self.env)
        for decision_item in decisions[:3]:
            if not execution_client:
                break
            subject = decision_item.get("subject") or "unknown market"
            market = next((m for m in trade_selected if (m.get("question") or m.get("slug")) == subject), None)
            decision_text = decision_item.get("text")
            bucket = _trade_bucket(market or {})
            if not market or not decision_text or bucket not in DAILY_TRADE_LIMITS:
                continue
            usage = await _daily_trade_usage(store)
            if usage.get(bucket, 0) >= DAILY_TRADE_LIMITS[bucket] or usage.get("total", 0) >= 3:
                execution_results.append({"subject": subject, "status": "daily_limit", "bucket": bucket, "reason": "daily trading limit reached"})
                continue
            if not market.get("is_open"):
                execution_results.append({"subject": subject, "status": "not_open", "bucket": bucket, "reason": "market is not accepting orders"})
                continue
            prompt = json.dumps({"task": "Validate this decision for safe execution; PASS when stale, illiquid or outside limits.", "market": market, "decision": decision_text, "trade_policy": {"bucket": bucket, "daily_limit": DAILY_TRADE_LIMITS[bucket], "used_today": usage.get(bucket, 0), "total_daily_limit": 3, "total_used_today": usage.get("total", 0)}, "execution_mode": "live" if trader.live else "dry_run", "risk_limits": {"max_order_usd": trader.max_order_usd, "min_edge": trader.min_edge, "min_confidence": trader.min_confidence, "min_entry_price": trader.min_price, "max_entry_price": trader.max_price}, "output_schema": {"action": "BUY_YES | BUY_NO | PASS", "outcome": "exact outcome label", "price": "0..1", "size": "shares", "amount_usd": "USD spend", "reason": "concise reason"}})
            try:
                result = await trader.execute(market, await execution_client.execute(prompt))
                result["subject"] = subject
                result["bucket"] = bucket
                execution_results.append(result)
                await store.record_decision("polymarket_execution", subject, result)
                if trader.live and result.get("executed"):
                    await _record_daily_trade(store, bucket)
            except Exception as exc:
                execution_results.append({"subject": subject, "status": "error", "bucket": bucket, "error": f"{type(exc).__name__}: {exc}"})
        return {"source": "Polymarket Gamma + Gemini", "markets_seen": len(markets), "premier_league_markets": len(pl_markets), "weather_markets": len(weather_markets), "selected": selected, "trade_selected": trade_selected, "research": research, "decisions": decisions, "execution": execution_results, "history_count": history_count, "daily_trade_limits": DAILY_TRADE_LIMITS, "daily_trades_used": await _daily_trade_usage(store), "polymarket_live_trading": trader.live, "live_execution": bool(trader.live), "status": "ok"}

    @staticmethod
    def format_alert(scan: str, payload: dict) -> str:
        if scan != "polymarket":
            return CryptoDefault.format_alert(scan, payload)
        lines = ["📊 INFOTRADER POLYMARKET", f"Markets seen: {payload.get('markets_seen', 0)} | EPL: {payload.get('premier_league_markets', 0)} | Weather: {payload.get('weather_markets', 0)}", ""]
        selected = payload.get("selected") or []
        if selected:
            lines.append("TOP RESEARCH MARKETS")
            for market in selected[:8]:
                lines.append(f"• [{market.get('market_kind','?')}] {market.get('question') or market.get('slug')} | score {safe_float(market.get('opportunity_score')):.1f}")
        trades = payload.get("trade_selected") or []
        if trades:
            lines += ["", "TRADE CANDIDATES (max 2 EPL + 1 weather)"]
            for market in trades[:3]:
                lines.append(f"• [{_trade_bucket(market)}] {market.get('question') or market.get('slug')}")
        decisions = payload.get("decisions") or []
        if decisions:
            lines += ["", "DECISIONS"]
            for item in decisions[:3]:
                lines.append(f"• {item.get('subject')}: {item.get('text','')[:600]}")
        usage = payload.get("daily_trades_used") or {}
        lines += ["", f"Daily budget: EPL {usage.get('premier_league',0)}/2 | Weather {usage.get('weather',0)}/1 | Total {usage.get('total',0)}/3", f"Live trading: {'ON' if payload.get('polymarket_live_trading') else 'OFF'}"]
        return "\n".join(lines)[:3900]
