import json

import pytest

import polymarket_trader.risk_manager as rm


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / "daily_state.json"
    stop = tmp_path / "STOP"
    monkeypatch.setattr(rm, "STATE_FILE", str(state))
    monkeypatch.setattr(rm, "KILL_SWITCH_FILE", str(stop))
    monkeypatch.setattr(rm, "MAX_POSITION_USD", 5.0)
    monkeypatch.setattr(rm, "MAX_DAILY_LOSS_USD", 15.0)
    monkeypatch.setattr(rm, "MAX_TRADES_PER_DAY", 5)
    monkeypatch.setattr(rm, "MIN_CONFIDENCE", 0.65)
    return state, stop


def test_hold_is_rejected(isolated_state):
    allowed, reason = rm.check({"action": "hold", "confidence": 0.9, "size_usd": 0})
    assert not allowed
    assert "hold" in reason.lower()


def test_size_and_confidence_are_enforced(isolated_state):
    allowed, _ = rm.check({"action": "buy", "confidence": 0.9, "size_usd": 6})
    assert not allowed
    allowed, _ = rm.check({"action": "buy", "confidence": 0.5, "size_usd": 1})
    assert not allowed


def test_kill_switch_blocks(isolated_state):
    _, stop = isolated_state
    stop.touch()
    allowed, reason = rm.check({"action": "buy", "confidence": 0.9, "size_usd": 1})
    assert not allowed
    assert "kill switch" in reason.lower()


def test_daily_loss_cap_blocks(isolated_state):
    state, _ = isolated_state
    state.write_text(json.dumps({"date": str(rm.date.today()), "realized_pnl_usd": -15.0, "trades_today": 2}))
    allowed, reason = rm.check({"action": "buy", "confidence": 0.9, "size_usd": 1})
    assert not allowed
    assert "daily loss cap" in reason.lower()
