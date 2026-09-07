import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "cloudflare" / "src"))

from scanner_helpers import extract_tickers, is_premier_league_market, normalize_market, priority_score


def test_worker_classifier_prioritizes_premier_league_market():
    pl = normalize_market({"id": "1", "question": "Arsenal vs Chelsea", "liquidity": "10000", "volume24hr": "1000"})
    generic = normalize_market({"id": "2", "question": "Will Bitcoin rise?", "liquidity": "10000", "volume24hr": "1000"})
    assert is_premier_league_market(pl["question"])
    assert priority_score(pl) > priority_score(generic)


def test_worker_normalizes_encoded_market_lists_and_tickers():
    market = normalize_market({"question": "Premier League winner", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.6", "0.4"]'})
    assert market["outcomes"] == ["Yes", "No"]
    assert market["outcome_prices"] == ["0.6", "0.4"]
    assert extract_tickers("Robinhood lists $DOGE and $PEPE") == ["DOGE", "PEPE"]
