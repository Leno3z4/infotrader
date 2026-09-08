"""Compact multi-sport + weather context for market research."""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode

FetchJson = Callable[..., Awaitable[Any]]
SPORT_TERMS = ("premier league", "football", "soccer", "nba", "wnba", "nfl", "mlb", "nhl", "ufc", "mma", "tennis", "atp", "wta", "formula 1", "f1", "cricket", "rugby", "golf", "boxing", "ncaa", "college football", "champions league", "la liga", "bundesliga", "serie a", "ligue 1", "euroleague", "baseball", "basketball", "hockey", "motogp", "nascar", "indian premier league")
WEATHER_TERMS = ("weather", "temperature", "temp", "rain", "precipitation", "snow", "wind", "hurricane", "tornado", "storm", "forecast", "heat", "frost", "humidity")
SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def classify_market(question: str) -> str | None:
    text = (question or "").lower()
    if any(term in text for term in WEATHER_TERMS):
        return "weather"
    return "sports" if any(term in text for term in SPORT_TERMS) else None


def participants_from_question(question: str) -> list[str]:
    text = re.sub(r"\b(will|win|beat|versus|against|today|tonight|tomorrow)\b", " ", question or "", flags=re.I)
    pieces = re.split(r"\s+(?:v|vs\.?|at|@)\s+|\s+-\s+|\s+\|\s+", text, flags=re.I)
    return [p.strip(" ?:-") for p in pieces[:2] if len(p.strip()) >= 2] if len(pieces) >= 2 else []


def _compact_team(team: dict[str, Any]) -> dict[str, Any]:
    return {"id": team.get("idTeam"), "name": team.get("strTeam"), "sport": team.get("strSport"), "league": team.get("strLeague"), "country": team.get("strCountry"), "stadium": team.get("strStadium"), "formed": team.get("intFormedYear")}


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {"date": event.get("dateEvent"), "home": event.get("strHomeTeam"), "away": event.get("strAwayTeam"), "home_score": event.get("intHomeScore"), "away_score": event.get("intAwayScore"), "league": event.get("strLeague"), "status": event.get("strStatus")}


async def _team_context(fetch_json: FetchJson, name: str) -> dict[str, Any] | None:
    data = await fetch_json(f"{SPORTSDB_BASE}/searchteams.php?t={quote(name)}")
    teams = data.get("teams") if isinstance(data, dict) else None
    if not teams:
        return None
    team = teams[0]
    team_id = team.get("idTeam")
    recent_data = await fetch_json(f"{SPORTSDB_BASE}/eventslast.php?id={quote(str(team_id))}")
    roster_data = await fetch_json(f"{SPORTSDB_BASE}/lookup_all_players.php?id={quote(str(team_id))}")
    events = recent_data.get("results", []) if isinstance(recent_data, dict) else []
    players = roster_data.get("player", []) if isinstance(roster_data, dict) else []
    return {
        "team": _compact_team(team),
        "recent_results": [_compact_event(e) for e in events[:5] if isinstance(e, dict)],
        "players": [{"name": p.get("strPlayer"), "position": p.get("strPosition")} for p in players[:15] if isinstance(p, dict)],
    }


async def _weather_context(fetch_json: FetchJson, query: str) -> dict[str, Any] | None:
    geo_q = re.sub(r"\b(weather|temperature|temp|rain|precipitation|snow|wind|forecast|chance|will|be|on|in|at|for)\b", " ", query or "", flags=re.I)
    geo_q = re.sub(r"\s+", " ", geo_q).strip(" ?,:.-")
    if not geo_q:
        return None
    geo = await fetch_json(f"{GEOCODING_URL}?{urlencode({'name': geo_q[:80], 'count': 1, 'language': 'en', 'format': 'json'})}")
    results = geo.get("results") if isinstance(geo, dict) else None
    if not results:
        return None
    place = results[0]
    forecast = await fetch_json(f"{FORECAST_URL}?{urlencode({'latitude': place.get('latitude'), 'longitude': place.get('longitude'), 'current': 'temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m', 'hourly': 'temperature_2m,precipitation_probability,precipitation,wind_speed_10m', 'forecast_days': 3, 'timezone': 'auto'})}")
    hourly = forecast.get("hourly", {}) if isinstance(forecast, dict) else {}
    return {"location": {"name": place.get("name"), "country": place.get("country"), "latitude": place.get("latitude"), "longitude": place.get("longitude")}, "current": forecast.get("current", {}) if isinstance(forecast, dict) else {}, "next_12h": {k: list(hourly.get(k, []))[:12] for k in ("time", "temperature_2m", "precipitation_probability", "precipitation", "wind_speed_10m")}}


async def build_context(fetch_json: FetchJson, question: str) -> dict[str, Any]:
    kind = classify_market(question)
    context: dict[str, Any] = {"kind": kind, "sportsdb": None, "weather": None}
    if kind == "sports":
        participants = participants_from_question(question)
        teams = []
        for participant in participants:
            try:
                row = await _team_context(fetch_json, participant)
            except Exception as exc:
                row = {"lookup": participant, "error": f"{type(exc).__name__}: {exc}"}
            if row:
                teams.append(row)
        context["sportsdb"] = {"source": "TheSportsDB free v1", "participants": participants, "teams": teams}
        stadium = next(((t.get("team") or {}).get("stadium") for t in teams if (t.get("team") or {}).get("stadium")), None)
        if stadium:
            try:
                context["weather"] = await _weather_context(fetch_json, stadium)
            except Exception as exc:
                context["weather"] = {"error": f"{type(exc).__name__}: {exc}"}
    elif kind == "weather":
        try:
            context["weather"] = await _weather_context(fetch_json, question)
        except Exception as exc:
            context["weather"] = {"error": f"{type(exc).__name__}: {exc}"}
    return context
