from __future__ import annotations

import json
from typing import Any

import requests

from meme_monitor.scraper import get_news
from polymarket_trader.premier_league import priority_score

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
DATA_POSITIONS_URL = "https://data-api.polymarket.com/positions"
PL_DISCOVERY_QUERIES = ("Premier League", "Arsenal", "Liverpool", "Manchester United", "Manchester City", "Chelsea", "Tottenham")


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def get_open_markets(limit: int = 20, keyword: str | None = None) -> list[dict]:
    params = {"active": "true", "closed": "false", "limit": limit}
    if keyword:
        params["search"] = keyword
    try:
        response = requests.get(GAMMA_MARKETS_URL, params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        print(f"[market_data] Gamma API failed: {exc}")
        return []
    markets = payload if isinstance(payload, list) else payload.get("markets", [])
    normalized = []
    for m in markets:
        normalized.append({
            "id": m.get("id"), "condition_id": m.get("conditionId"),
            "question": m.get("question"), "slug": m.get("slug"),
            "clob_token_ids": _json_list(m.get("clobTokenIds")),
            "outcomes": _json_list(m.get("outcomes")),
            "outcome_prices": _json_list(m.get("outcomePrices")),
            "volume_24h": m.get("volume24hr"), "liquidity": m.get("liquidity"),
            "end_date": m.get("endDate"),
        })
    return normalized


def get_priority_markets(limit: int = 10) -> list[dict]:
    """Build a mixed discovery set, guaranteeing a Premier League scan when available."""
    combined: dict[tuple[str | None, str | None], dict] = {}
    for query in PL_DISCOVERY_QUERIES:
        for market in get_open_markets(limit=max(10, limit), keyword=query):
            combined[(market.get("id"), market.get("slug"))] = market
    for market in get_open_markets(limit=max(30, limit * 4)):
        combined[(market.get("id"), market.get("slug"))] = market
    ranked = sorted(combined.values(), key=priority_score, reverse=True)
    return ranked[:limit]


def get_market_news(question: str, max_items: int = 6) -> list[dict]:
    words = [word for word in question.split() if len(word) > 2]
    return get_news(" ".join(words[:10]), max_items=max_items)


def get_positions(user_address: str, condition_id: str | None = None) -> list[dict]:
    params = {"user": user_address}
    if condition_id:
        params["market"] = condition_id
    try:
        response = requests.get(DATA_POSITIONS_URL, params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        print(f"[market_data] Positions API failed: {exc}")
        return []
    return payload if isinstance(payload, list) else []


def get_position_for_market(user_address: str, market: dict) -> dict | None:
    token_ids = set(market.get("clob_token_ids") or [])
    positions = get_positions(user_address, condition_id=market.get("condition_id"))
    for position in positions:
        if position.get("asset") in token_ids:
            return position
    return positions[0] if positions else None
