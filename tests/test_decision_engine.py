from polymarket_trader.decision_engine import normalize_decision


def test_normalize_decision_accepts_valid_trade():
    result = normalize_decision({"action": "buy", "outcome": "Yes", "confidence": 0.91, "size_usd": 3, "reason": "fresh evidence"}, ["Yes", "No"])
    assert result["action"] == "buy"
    assert result["outcome"] == "Yes"
    assert result["confidence"] == 0.91
    assert result["size_usd"] == 3.0


def test_normalize_decision_rejects_unknown_outcome():
    result = normalize_decision({"action": "buy", "outcome": "Maybe", "confidence": 0.91, "size_usd": 3}, ["Yes", "No"])
    assert result["action"] == "hold"
    assert result["confidence"] == 0.0


def test_normalize_decision_clamps_confidence_and_size():
    result = normalize_decision({"action": "buy", "outcome": "Yes", "confidence": 2, "size_usd": 999}, ["Yes"], max_size_usd=5)
    assert result["confidence"] == 1.0
    assert result["size_usd"] == 5.0
