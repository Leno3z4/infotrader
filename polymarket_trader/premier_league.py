from __future__ import annotations

import re
from datetime import datetime, timezone

PL_TERMS = (
    "premier league",
    "arsenal", "aston villa", "bournemouth", "brentford", "brighton",
    "burnley", "chelsea", "crystal palace", "everton", "fulham",
    "leeds", "liverpool", "manchester city", "man city", "manchester united",
    "man utd", "newcastle", "nottingham forest", "sunderland", "tottenham",
    "spurs", "west ham", "wolves", "wolverhampton", "ipswich", "coventry", "hull",
)
PL_MARKET_PATTERNS = (
    r"\bpremier league\b",
    r"\btop\s*4\b",
    r"\bfinish\s+(?:top|in)\b",
    r"\bwin\s+the\s+title\b",
    r"\brelegat(?:ed|ion)\b",
    r"\b(?:win|beat|draw with|lose to)\b",
)


def is_premier_league_market(question: str) -> bool:
    q = (question or "").lower()
    if any(term in q for term in PL_TERMS[:1]):
        return True
    team_hits = sum(term in q for term in PL_TERMS[1:])
    return team_hits >= 2 or any(re.search(pattern, q) for pattern in PL_MARKET_PATTERNS) and team_hits >= 1


def _hours_to_event(end_date: str | None) -> float | None:
    if not end_date:
        return None
    try:
        text = end_date.replace("Z", "+00:00")
        when = datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (when - datetime.now(timezone.utc)).total_seconds() / 3600
    except ValueError:
        return None


def priority_score(market: dict) -> tuple[int, float]:
    question = str(market.get("question") or "")
    is_pl = is_premier_league_market(question)
    liquidity = float(market.get("liquidity") or 0)
    volume = float(market.get("volume_24h") or 0)
    hours = _hours_to_event(market.get("end_date"))
    score = (100 if is_pl else 0)
    score += min(25, max(0, liquidity) / 10000)
    score += min(15, max(0, volume) / 10000)
    if hours is not None and hours >= 0:
        if hours <= 2:
            score += 35
        elif hours <= 12:
            score += 25
        elif hours <= 48:
            score += 12
        elif hours <= 168:
            score += 5
    return (1 if is_pl else 0, score)


def research_queries(market: dict) -> list[str]:
    question = str(market.get("question") or "").strip()
    q = [question, f"{question} latest news", f"{question} official announcement"]
    if is_premier_league_market(question):
        q.extend([
            f"{question} injuries suspensions lineup",
            f"{question} predicted lineup team news",
            f"{question} form table goals xG",
            f"{question} site:premierleague.com",
            f"{question} site:bbc.com/sport/football",
            f"{question} site:reuters.com sports",
        ])
    return q[:9]
