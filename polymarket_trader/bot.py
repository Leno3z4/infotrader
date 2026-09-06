from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.gemini_rotator import AllKeysExhaustedError, GeminiRotator
from common.notifier import send_telegram
from polymarket_trader import risk_manager
from polymarket_trader.decision_engine import decide
from polymarket_trader.executor import DRY_RUN, place_order
from polymarket_trader.market_data import get_market_news, get_position_for_market, get_priority_markets
from polymarket_trader.premier_league import is_premier_league_market
from polymarket_trader.researcher import research_market

MAX_MARKETS_PER_RUN = int(os.getenv("MAX_MARKETS_PER_RUN", "8"))
POLYMARKET_PROFILE_ADDRESS = os.getenv("POLYMARKET_PROFILE_ADDRESS")
STATE_DIR = Path(os.getenv("STATE_DIR", "/app/state"))
PAUSED_FILE = STATE_DIR / "PAUSED"
STOP_FILE = STATE_DIR / "STOP"


def _find_token_index(market: dict, outcome: str | None) -> int | None:
    outcomes = [str(value) for value in (market.get("outcomes") or [])]
    if outcome is None or outcome not in outcomes:
        return None
    return outcomes.index(outcome)


def _signal_log(line: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "signals.log").open("a", encoding="utf-8") as handle:
        handle.write(line.replace("\n", " ") + "\n")


def run_once() -> None:
    if STOP_FILE.exists() or PAUSED_FILE.exists():
        send_telegram(f"⏸️ Polymarket bot skipped (kill_switch={STOP_FILE.exists()}, paused={PAUSED_FILE.exists()}).")
        return

    researcher = GeminiRotator(role="research")
    decision_agent = GeminiRotator(role="decision")
    try:
        markets = get_priority_markets(limit=MAX_MARKETS_PER_RUN)
    except Exception as exc:
        send_telegram(f"⚠️ Polymarket market discovery failed: {type(exc).__name__}: {exc}")
        return

    if not markets:
        send_telegram("Polymarket bot: no priority markets found.")
        return

    pl_count = sum(is_premier_league_market(str(m.get("question") or "")) for m in markets)
    summary = [f"<b>Polymarket AI run</b> — {len(markets)} markets, {pl_count} Premier League"]
    for market in markets:
        try:
            research = research_market(market, researcher)
            news = get_market_news(market["question"])
            position = get_position_for_market(POLYMARKET_PROFILE_ADDRESS, market) if POLYMARKET_PROFILE_ADDRESS else None
            decision = decide(market, news, research=research, position=position, rotator=decision_agent)
        except AllKeysExhaustedError as exc:
            summary.append(f"⚠️ Gemini role exhausted: {exc}")
            break
        except Exception as exc:
            summary.append(f"⚠️ {market.get('question', 'market')[:70]} — analysis error: {type(exc).__name__}")
            continue

        allowed, reason = risk_manager.check(decision)
        question = market.get("question", "")[:80]
        line = (
            f"• {'⚽' if is_premier_league_market(question) else '🌐'} {question} → "
            f"<b>{decision['action']}</b> {decision.get('outcome') or ''} "
            f"conf {decision['confidence']:.2f}, edge {decision.get('edge', 0):+.2f}, ${decision['size_usd']:.2f}"
        )
        if not allowed:
            summary.append(f"{line} — skipped: {reason}")
            continue
        if decision["action"] in {"sell", "close"} and not position:
            summary.append(f"{line} — skipped: no matching position.")
            continue
        outcome_index = _find_token_index(market, decision.get("outcome"))
        token_ids, prices = market.get("clob_token_ids") or [], market.get("outcome_prices") or []
        if outcome_index is None or outcome_index >= len(token_ids):
            summary.append(f"{line} — skipped: outcome/token mapping unavailable.")
            continue
        try:
            price = float(prices[outcome_index])
        except (TypeError, ValueError, IndexError):
            summary.append(f"{line} — skipped: price unavailable.")
            continue
        side = "sell" if decision["action"] in {"sell", "close"} else "buy"
        try:
            result = place_order(token_ids[outcome_index], side, price, decision["size_usd"], decision.get("reason", ""))
            line += f" — {result.get('message', 'submitted')}"
        except RuntimeError as exc:
            line += f" — execution blocked: {exc}"
        summary.append(line)
        _signal_log(line)

    message = "\n".join(summary)
    print(message)
    send_telegram(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        run_once()
        return
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"[polymarket] {type(exc).__name__}: {exc}")
            send_telegram(f"⚠️ Polymarket bot error: {type(exc).__name__}: {exc}")
        time.sleep(3600)


if __name__ == "__main__":
    main()
