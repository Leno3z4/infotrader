from meme_monitor.scraper import extract_tickers


def test_extract_explicit_tickers():
    assert extract_tickers("Robinhood lists $DOGE and $PEPE") == ["DOGE", "PEPE"]


def test_extracts_conservative_caps():
    assert "DOGE" in extract_tickers("DOGE surges after a new listing")
