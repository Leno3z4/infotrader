from __future__ import annotations

import json
from typing import Any

from common.gemini_rotator import GeminiRotator
from polymarket_trader.premier_league import is_premier_league_market, research_queries

BASE_PROMPT = '''You are the RESEARCH AGENT for an automated prediction-market system.
Your job is to investigate the market question using fresh public web information.
You are not the trader and must not recommend a trade.

Rules:
- Search the live web for current information; prefer primary/official sources and high-quality reporting.
- For Premier League questions, prioritize PremierLeague.com, club sites, official competition/league sources, reputable journalists, and recent reporting on injuries, suspensions, lineups, form, fixtures and schedule changes.
- Check multiple independent sources and explicitly identify disagreements.
- Distinguish confirmed facts from reports, projections, rumours and social-media claims.
- Give absolute dates/times where possible.
- Do not invent a source or URL.
- Return concise JSON only.

MARKET QUESTION:
{question}

MARKET OUTCOMES:
{outcomes}

MARKET END DATE:
{end_date}

MARKET LIQUIDITY:
{liquidity}

MARKET 24H VOLUME:
{volume}

SEARCH HINTS:
{queries}

Return this shape:
{{
  "market_type": "premier_league" | "general",
  "summary": "2-4 sentence evidence summary",
  "confidence_in_research": 0.0,
  "key_facts": ["fact 1", "fact 2"],
  "contradictions": ["..."],
  "sources": [
    {{"title":"...","url":"...","published":"...","source_type":"official|reputable|social|other","claim":"..."}}
  ]
}}
'''


def research_market(market: dict[str, Any], rotator: GeminiRotator) -> dict[str, Any]:
    question = str(market.get("question") or "")
    prompt = BASE_PROMPT.format(
        question=question,
        outcomes=market.get("outcomes") or [],
        end_date=market.get("end_date"),
        liquidity=market.get("liquidity"),
        volume=market.get("volume_24h"),
        queries="\n".join(f"- {item}" for item in research_queries(market)),
    )
    raw = rotator.generate_web(prompt)
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {
            "market_type": "premier_league" if is_premier_league_market(question) else "general",
            "summary": raw[:1000],
            "confidence_in_research": 0.0,
            "key_facts": [],
            "contradictions": ["Research agent did not return valid JSON."],
            "sources": [],
        }
    if not isinstance(result, dict):
        result = {}
    result.setdefault("market_type", "premier_league" if is_premier_league_market(question) else "general")
    result.setdefault("summary", "")
    result.setdefault("confidence_in_research", 0.0)
    result.setdefault("key_facts", [])
    result.setdefault("contradictions", [])
    result.setdefault("sources", [])
    return result
