from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

import requests

DEXSCREENER_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/tokens/v1"
RESERVOIR_COLLECTION_URL = "https://api.reservoir.tools/collections/v7"
HEADERS = {"User-Agent": "info-trader/1.0"}
HTTP_TIMEOUT = 15


def _get_json(url: str, **kwargs) -> object:
    response = requests.get(url, timeout=HTTP_TIMEOUT, headers=HEADERS, **kwargs)
    response.raise_for_status()
    return response.json()


def _pair_summary(pair: dict) -> dict:
    liquidity = float(((pair.get("liquidity") or {}).get("usd")) or 0)
    volume_24h = float(((pair.get("volume") or {}).get("h24")) or 0)
    price_change_24h = float(((pair.get("priceChange") or {}).get("h24")) or 0)
    h24_txns = ((pair.get("txns") or {}).get("h24")) or {}
    return {
        "chain": pair.get("chainId"), "dex": pair.get("dexId"),
        "pair_address": pair.get("pairAddress"),
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "name": (pair.get("baseToken") or {}).get("name"),
        "token_address": (pair.get("baseToken") or {}).get("address"),
        "price_usd": float(pair["priceUsd"]) if pair.get("priceUsd") else None,
        "liquidity_usd": liquidity, "volume_24h_usd": volume_24h,
        "price_change_24h_pct": price_change_24h,
        "buys_24h": int(h24_txns.get("buys") or 0),
        "sells_24h": int(h24_txns.get("sells") or 0), "url": pair.get("url"),
    }


def get_trending_pairs(min_liquidity_usd: float = 5000, limit: int = 25) -> list[dict]:
    try:
        boosted = _get_json(DEXSCREENER_BOOSTS_URL)
    except (requests.RequestException, ValueError) as exc:
        print(f"[scraper] DexScreener boosts failed: {exc}")
        return []
    results = []
    for item in list(boosted or [])[:limit]:
        chain_id, token_address = item.get("chainId"), item.get("tokenAddress")
        if not chain_id or not token_address:
            continue
        try:
            pairs = _get_json(f"{DEXSCREENER_TOKENS_URL}/{chain_id}/{token_address}")
        except (requests.RequestException, ValueError):
            continue
        for pair in pairs or []:
            summary = _pair_summary(pair)
            if summary["liquidity_usd"] >= min_liquidity_usd:
                results.append(summary)
    return sorted(results, key=lambda row: row["liquidity_usd"], reverse=True)


def search_pairs(query: str, min_liquidity_usd: float = 1000, limit: int = 10) -> list[dict]:
    if not query.strip():
        return []
    try:
        payload = _get_json(DEXSCREENER_SEARCH_URL, params={"q": query.strip()})
    except (requests.RequestException, ValueError) as exc:
        print(f"[scraper] DexScreener search failed for {query!r}: {exc}")
        return []
    results = []
    for pair in (payload or {}).get("pairs", [])[:limit]:
        summary = _pair_summary(pair)
        if summary["liquidity_usd"] >= min_liquidity_usd:
            results.append(summary)
    return sorted(results, key=lambda row: row["liquidity_usd"], reverse=True)


def extract_tickers(text: str) -> list[str]:
    candidates = re.findall(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b", text)
    candidates += re.findall(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,7})(?![A-Za-z0-9])", text)
    blocked = {"USD", "US", "CEO", "ETF", "API", "NFT", "THE", "AND"}
    seen, results = set(), []
    for candidate in candidates:
        symbol = candidate.upper()
        if symbol in blocked or symbol in seen:
            continue
        seen.add(symbol)
        results.append(symbol)
    return results[:6]


def get_news(query: str, max_items: int = 8) -> list[dict]:
    feed_url = "https://news.google.com/rss/search?" f"q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        response = requests.get(feed_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError) as exc:
        print(f"[scraper] News fetch failed for {query!r}: {exc}")
        return []
    results = []
    for item in root.findall("./channel/item")[:max_items]:
        def text(name: str) -> str:
            node = item.find(name)
            return (node.text or "").strip() if node is not None else ""
        results.append({"title": text("title"), "link": text("link"), "published": text("pubDate"), "summary": re.sub(r"<[^>]+>", " ", text("description")).strip()})
    return results


def get_robinhood_news(max_items_per_query: int = 4) -> list[dict]:
    queries = ['"Robinhood" crypto token listing', '"Robinhood" meme coin', '"Robinhood" mint token crypto']
    results, seen_links = [], set()
    for query in queries:
        for item in get_news(query, max_items=max_items_per_query):
            link = item.get("link")
            if link and link in seen_links:
                continue
            if link:
                seen_links.add(link)
            results.append(item)
    return results


def get_nft_collection_stats(slug: str) -> dict | None:
    headers = dict(HEADERS)
    api_key = os.getenv("RESERVOIR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    try:
        response = requests.get(RESERVOIR_COLLECTION_URL, params={"slug": slug}, timeout=HTTP_TIMEOUT, headers=headers)
        response.raise_for_status()
        collections = response.json().get("collections", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"[scraper] Reservoir failed for {slug}: {exc}")
        return None
    if not collections:
        return None
    collection = collections[0]
    floor = ((collection.get("floorAsk") or {}).get("price", {}).get("amount")) or {}
    return {"slug": slug, "name": collection.get("name"), "floor_price_usd": floor.get("usd"), "volume_24h_usd": (collection.get("volume") or {}).get("1day")}
