from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

STATE_FILE = os.getenv("TRADING_STATE_FILE", str(Path(__file__).with_name("daily_state.json")))
KILL_SWITCH_FILE = os.getenv("KILL_SWITCH_FILE", str(Path(__file__).with_name("STOP")))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "5"))
MAX_DAILY_LOSS_USD = float(os.getenv("MAX_DAILY_LOSS_USD", "15"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.65"))


def _load_state() -> dict:
    path = Path(STATE_FILE)
    if not path.exists():
        return {"date": str(date.today()), "realized_pnl_usd": 0.0, "trades_today": 0}
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("date") != str(date.today()):
        state = {"date": str(date.today()), "realized_pnl_usd": 0.0, "trades_today": 0}
    return state


def _save_state(state: dict) -> None:
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state))
    temp_path.replace(path)


def record_trade_pnl(pnl_usd: float) -> None:
    state = _load_state()
    state["realized_pnl_usd"] += float(pnl_usd)
    state["trades_today"] += 1
    _save_state(state)


def check(decision: dict) -> tuple[bool, str]:
    if Path(KILL_SWITCH_FILE).exists():
        return False, "Kill switch active."
    action = decision.get("action")
    if action not in {"buy", "sell", "close"}:
        return False, "Decision was hold/invalid."
    try:
        confidence = float(decision.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < MIN_CONFIDENCE:
        return False, f"Confidence {confidence:.2f} below {MIN_CONFIDENCE:.2f}."
    try:
        size = float(decision.get("size_usd", 0))
    except (TypeError, ValueError):
        size = 0.0
    if not 0 < size <= MAX_POSITION_USD:
        return False, f"Requested size ${size:.2f} outside 0 < size <= ${MAX_POSITION_USD:.2f}."
    state = _load_state()
    if float(state["realized_pnl_usd"]) <= -MAX_DAILY_LOSS_USD:
        return False, f"Daily loss cap ${MAX_DAILY_LOSS_USD:.2f} reached."
    if int(state["trades_today"]) >= MAX_TRADES_PER_DAY:
        return False, f"Daily trade limit {MAX_TRADES_PER_DAY} reached."
    return True, "ok"
