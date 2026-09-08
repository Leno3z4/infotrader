"""Pure helpers shared by the Cloudflare scheduled scanners.

This module deliberately has no Workers SDK imports, so it is easy to test with
normal CPython. Network I/O lives in ``entry.py`` and uses Workers ``fetch``.
"""

from __future__ import annotations

import json
import re
from typing import Any

PL_TERMS = (
    "premier league", "arsenal", "aston villa", "bournemouth", "brentford",
    "brighton", "burnley", "chelsea", "crystal palace", "everton", "fulham",
    "leeds", "liverpool", "manchester city", "man city", "manchester united",
    "man utd", "newcastle", "nottingham forest", "sunderland", "tottenham",
    "spurs", "west ham", "wolves", "wolverhampton",
)


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def is_premier_league_market(question: str) -> bool:
    text = (question or "").lower()
    if "premier league" in text:
        return True
    return sum(term in text for term in PL_TERMS[1:]) >= 2


def priority_score(market: dict[str, Any]) -> tuple[int, float]:
    question = str(market.get("question") or "")
    is_pl = is_premier_league_market(question)
    liquidity = safe_float(market.get("liquidity"))
    volume = safe_float(market.get("volume_24h"))
    return (1 if is_pl else 0, (100 if is_pl else 0) + min(25, liquidity / 10_000) + min(15, volume / 10_000))


def normalize_market(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "condition_id": item.get("conditionId"),
        "question": item.get("question"),
        "slug": item.get("slug"),
        "outcomes": json_list(item.get("outcomes")),
        "outcome_prices": json_list(item.get("outcomePrices")),
        "clob_token_ids": json_list(item.get("clobTokenIds")),
        "tick_size": item.get("orderPriceMinTickSize") or item.get("tickSize"),
        "neg_risk": bool(item.get("negRisk", False)),
        "accepting_orders": bool(item.get("acceptingOrders", item.get("enableOrderBook", True))),
        "liquidity": safe_float(item.get("liquidity")),
        "volume_24h": safe_float(item.get("volume24hr")),
        "end_date": item.get("endDate"),
        "premier_league": is_premier_league_market(str(item.get("question") or "")),
    }


def extract_tickers(text: str) -> list[str]:
    candidates = re.findall(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b", text)
    candidates += re.findall(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,7})(?![A-Za-z0-9])", text)
    blocked = {"USD", "US", "CEO", "ETF", "API", "NFT", "THE", "AND"}
    seen: set[str] = set()
    results: list[str] = []
    for candidate in candidates:
        symbol = candidate.upper()
        if symbol not in blocked and symbol not in seen:
            seen.add(symbol)
            results.append(symbol)
    return results[:6]


def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
