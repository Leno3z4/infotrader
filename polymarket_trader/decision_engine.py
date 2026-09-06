from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.gemini_rotator import GeminiRotator

PROMPT_TEMPLATE = r'''You are the DECISION AGENT for an automated prediction-market system.
A separate research agent gathered the evidence. You must evaluate that evidence plus
current market data and position data. Do not search the web here; use the research packet.
Return one JSON object and nothing else.

MARKET:
Question: {question}
Outcomes: {outcomes}
Current prices: {prices}
24h volume: {volume}
Liquidity: {liquidity}
End date: {end_date}

RESEARCH PACKET:
{research}

RECENT HEADLINE FEED:
{news}

CURRENT POSITION:
{position}

Decision rules:
- Hold when evidence is weak, stale, ambiguous, or materially contradictory.
- Never choose an outcome not present in the outcomes list.
- Estimate fair probability first; compare it with the market price.
- Prefer no-trade when edge is small after uncertainty.
- Suggested size is only a proposal; Python hard limits are authoritative.
- sell and close require an existing position.

Return:
{{
  "action": "buy" | "sell" | "close" | "hold",
  "outcome": "<exact market outcome>" | null,
  "confidence": 0.0,
  "fair_probability": 0.0,
  "market_probability": 0.0,
  "edge": 0.0,
  "size_usd": 0.0,
  "reason": "one short sentence"
}}'''.strip()


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def normalize_decision(raw: dict[str, Any], outcomes: list[str], max_size_usd: float | None = None) -> dict:
    action = str(raw.get("action", "hold")).lower().strip()
    if action not in {"buy", "sell", "close", "hold"}:
        action = "hold"
    outcome = raw.get("outcome")
    if outcome is not None:
        outcome = str(outcome)
    if action != "hold" and outcome not in outcomes:
        return {"action": "hold", "outcome": None, "confidence": 0.0, "fair_probability": 0.0, "market_probability": 0.0, "edge": 0.0, "size_usd": 0.0, "reason": "invalid model outcome"}
    def num(name: str, default: float = 0.0) -> float:
        try:
            return float(raw.get(name, default))
        except (TypeError, ValueError):
            return default
    confidence = min(1.0, max(0.0, num("confidence")))
    fair_probability = min(1.0, max(0.0, num("fair_probability")))
    market_probability = min(1.0, max(0.0, num("market_probability")))
    edge = max(-1.0, min(1.0, num("edge")))
    size_usd = max(0.0, num("size_usd"))
    if max_size_usd is not None:
        size_usd = min(size_usd, max_size_usd)
    if action == "hold":
        outcome, size_usd = None, 0.0
    return {"action": action, "outcome": outcome, "confidence": confidence, "fair_probability": fair_probability, "market_probability": market_probability, "edge": edge, "size_usd": size_usd, "reason": str(raw.get("reason", "")).strip()[:500]}


def decide(market: dict, news: list[dict], research: dict, position: dict | None, rotator: GeminiRotator) -> dict:
    news_text = "\n".join(f"- {item.get('title', '')} ({item.get('published', 'unknown time')})" for item in news) or "(no recent news found)"
    prompt = PROMPT_TEMPLATE.format(
        question=market.get("question"), outcomes=market.get("outcomes") or [],
        prices=market.get("outcome_prices") or [], volume=market.get("volume_24h"),
        liquidity=market.get("liquidity"), end_date=market.get("end_date"),
        research=json.dumps(research, ensure_ascii=False), news=news_text, position=position or "none",
    )
    raw_text = rotator.generate(prompt)
    try:
        raw = json.loads(_strip_fences(raw_text))
    except (TypeError, json.JSONDecodeError):
        return {"action": "hold", "outcome": None, "confidence": 0.0, "fair_probability": 0.0, "market_probability": 0.0, "edge": 0.0, "size_usd": 0.0, "reason": "model JSON parse error"}
    return normalize_decision(raw, outcomes=[str(x) for x in (market.get("outcomes") or [])], max_size_usd=float(os.getenv("MAX_POSITION_USD", "5")))
