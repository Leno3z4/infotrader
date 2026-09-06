from polymarket_trader.premier_league import is_premier_league_market, priority_score, research_queries


def test_detects_premier_league_match_market():
    assert is_premier_league_market("Arsenal vs Chelsea")


def test_detects_premier_league_title_market():
    assert is_premier_league_market("Who will win the Premier League title?")


def test_non_football_market_not_pl():
    assert not is_premier_league_market("Will Bitcoin be above $100k by Friday?")


def test_premier_league_scores_above_generic():
    pl = {"question": "Arsenal vs Chelsea", "liquidity": 10000, "volume_24h": 1000}
    generic = {"question": "Will Bitcoin be above $100k?", "liquidity": 10000, "volume_24h": 1000}
    assert priority_score(pl) > priority_score(generic)


def test_pl_research_includes_team_news_and_official_searches():
    queries = research_queries({"question": "Arsenal vs Chelsea"})
    assert any("injuries" in q for q in queries)
    assert any("premierleague.com" in q for q in queries)
