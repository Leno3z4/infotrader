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
from polymarket_trader.market_data import get_market_news, get_open_markets, get_position_for_market

MARKET_KEYWORD = os.getenv("POLYMARKET_KEYWORD")
MAX_MARKETS_PER_RUN = int(os.getenv("MAX_MARKETS_PER_RUN", "5"))
POLYMARKET_PROFILE_ADDRESS = os.getenv("POLYMARKET_PROFILE_ADDRESS")
STATE_DIR = Path(os.getenv("STATE_DIR", "/app/state"))
PAUSED_FILE = STATE_DIR / "PAUSED"
STOP_FILE = STATE_DIR / "STOP"


def _find_token_index(market: dict, outcome: str | None) -> int | None:
    outcomes = [str(value) for value in (market.get("outcomes") or [])]
    if outcome is None or outcome not in outcomes:
        return None
    return outcomes.index(outcome)


def run_once() -> None:
    if STOP_FILE.exists() or PAUSED_FILE.exists():
        send_telegram(f"⏸️ Polymarket bot skipped (kill_switch={STOP_FILE.exists()}, paused={PAUSED_FILE.exists()}).")
        return

    rotator = GeminiRotator()
    markets = get_open_markets(limit=MAX_MARKETS_PER_RUN, keyword=MARKET_KEYWORD)
    if not markets:
        send_telegram("Polymarket bot: no markets found.")
        return

    summary = [f"<b>Polymarket intelligence run</b> (dry_run={DRY_RUN})"]
    for market in markets:
        news = get_market_news(market["question"])
        position = get_position_for_market(POLYMARKET_PROFILE_ADDRESS, market) if POLYMARKET_PROFILE_ADDRESS else None
        try:
            decision = decide(market, news, position=position, rotator=rotator)
        except AllKeysExhaustedError as exc:
            summary.append(f"⚠️ Gemini unavailable: {exc}")
            break
        allowed, reason = risk_manager.check(decision)
        line = f"• {market['question'][:90]} → <b>{decision['action']}</b> (conf {decision['confidence']:.2f}, ${decision['size_usd']:.2f})"
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
