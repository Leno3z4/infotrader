"""InfoTrader's Cloudflare Worker-native, dry-run-only scanner."""

from __future__ import annotations

import json
from urllib.parse import quote, urlparse

from workers import Response, WorkerEntrypoint, fetch

from scanner_helpers import extract_tickers, normalize_market, priority_score, safe_float
from storage import StateStore

POLYMARKET_CRON = "*/15 * * * *"
CRYPTO_CRON = "0 * * * *"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&search=Premier%20League"
DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
NEWS_QUERIES = ("Robinhood crypto token listing", "Robinhood meme coin", "Robinhood mint token crypto")


async def fetch_json(url: str, **options):
    response = await fetch(url, **options)
    if not response.ok:
        raise RuntimeError(f"upstream HTTP {response.status} for {url}")
    return await response.json()


def _rss_items(xml: str, limit: int) -> list[dict]:
    # Google News RSS entries are XML; only title/link are required for a signal.
    import re

    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml, flags=re.DOTALL)[:limit]:
        title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", block, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", block, re.DOTALL)
        if title:
            items.append({"title": (title.group(1) or title.group(2) or "").strip(), "link": (link.group(1).strip() if link else "")})
    return items


async def send_telegram(env, message: str) -> bool:
    token, chat_id = getattr(env, "TELEGRAM_BOT_TOKEN", None), getattr(env, "TELEGRAM_CHAT_ID", None)
    if not token or not chat_id:
        return False
    response = await fetch(
        f"https://api.telegram.org/bot{token}/sendMessage",
        method="POST",
        headers={"content-type": "application/json"},
        body=json.dumps({"chat_id": chat_id, "text": message[:4000]}),
    )
    return bool(response.ok)


def authorized(request, env) -> bool:
    token = getattr(env, "CONTROL_TOKEN", None)
    return bool(token and request.headers.get("Authorization") == f"Bearer {token}")


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = urlparse(request.url).path
        store = StateStore(self.env)

        if path == "/health":
            return Response.json({"ok": True, "service": "infotrader", "dry_run": True, "storage_bound": store.available})
        if path == "/status":
            return Response.json({"service": "infotrader", "dry_run": True, **(await store.status())})
        if path.startswith("/control/") or path.startswith("/scan/"):
            if request.method != "POST" or not authorized(request, self.env):
                return Response("Not found", status=404)
            if not store.available:
                return Response.json({"error": "INFOTRADER_STATE KV binding is required"}, status=503)
            if path == "/control/pause":
                await store.set_flag("PAUSED", True)
            elif path == "/control/resume":
                await store.set_flag("PAUSED", False)
                await store.set_flag("STOP", False)
            elif path == "/control/stop":
                await store.set_flag("STOP", True)
            elif path == "/scan/polymarket":
                return Response.json(await self.run_scan("polymarket", store))
            elif path == "/scan/crypto":
                return Response.json(await self.run_scan("crypto", store))
            else:
                return Response("Not found", status=404)
            return Response.json(await store.status())

        return Response.json({"service": "infotrader", "status": "online", "dry_run": True, "endpoints": ["/health", "/status"]})

    async def scheduled(self, controller, env, ctx):
        store = StateStore(env)
        if not store.available:
            print("INFOTRADER_STATE is not configured; scheduled scan skipped")
            return
        scan = "polymarket" if controller.cron == POLYMARKET_CRON else "crypto" if controller.cron == CRYPTO_CRON else None
        if scan:
            await self.run_scan(scan, store)

    async def run_scan(self, scan: str, store: StateStore) -> dict:
        if await store.flag("STOP"):
            return await store.record_scan(scan, "stopped", {})
        if await store.flag("PAUSED"):
            return await store.record_scan(scan, "paused", {})
        try:
            payload = await self.scan_polymarket() if scan == "polymarket" else await self.scan_crypto()
            event = await store.record_scan(scan, "ok", payload)
            await send_telegram(self.env, self.format_alert(scan, payload))
            return event
        except Exception as exc:
            event = await store.record_scan(scan, "error", {"error": f"{type(exc).__name__}: {exc}"})
            await send_telegram(self.env, f"InfoTrader {scan} scan error: {event['payload']['error']}")
            return event

    async def scan_polymarket(self) -> dict:
        raw = await fetch_json(GAMMA_MARKETS_URL)
        rows = raw if isinstance(raw, list) else raw.get("markets", [])
        markets = sorted((normalize_market(row) for row in rows if isinstance(row, dict)), key=priority_score, reverse=True)
        selected = markets[:8]
        return {"source": "Polymarket Gamma", "markets_seen": len(markets), "premier_league_markets": sum(m["premier_league"] for m in markets), "selected": selected}

    async def scan_crypto(self) -> dict:
        headlines: list[dict] = []
        seen: set[str] = set()
        for query in NEWS_QUERIES:
            response = await fetch(f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en")
            if response.ok:
                for item in _rss_items(await response.text(), 4):
                    if item["link"] and item["link"] not in seen:
                        seen.add(item["link"])
                        headlines.append(item)
        boosted = await fetch_json(DEX_BOOSTS_URL)
        pairs = []
        for token in list(boosted or [])[:10]:
            if not isinstance(token, dict):
                continue
            chain, address = token.get("chainId"), token.get("tokenAddress")
            if not chain or not address:
                continue
            raw_pairs = await fetch_json(f"https://api.dexscreener.com/tokens/v1/{chain}/{address}")
            for pair in list(raw_pairs or [])[:2]:
                liquidity = safe_float(((pair.get("liquidity") or {}).get("usd")))
                if liquidity >= float(getattr(self.env, "MIN_LIQUIDITY_USD", "5000")):
                    pairs.append({"symbol": (pair.get("baseToken") or {}).get("symbol"), "chain": pair.get("chainId"), "dex": pair.get("dexId"), "liquidity_usd": liquidity, "volume_24h_usd": safe_float(((pair.get("volume") or {}).get("h24"))), "url": pair.get("url")})
        nft_collections = [slug.strip() for slug in str(getattr(self.env, "NFT_COLLECTIONS", "")).split(",") if slug.strip()][:5]
        nfts = []
        for slug in nft_collections:
            headers = {"User-Agent": "infotrader-cloudflare/1.0"}
            reservoir_key = getattr(self.env, "RESERVOIR_API_KEY", None)
            if reservoir_key:
                headers["x-api-key"] = reservoir_key
            response = await fetch(f"https://api.reservoir.tools/collections/v7?slug={quote(slug)}", headers=headers)
            if not response.ok:
                continue
            collections = (await response.json()).get("collections", [])
            if collections:
                collection = collections[0]
                nfts.append({"slug": slug, "name": collection.get("name"), "floor_price_usd": (((collection.get("floorAsk") or {}).get("price") or {}).get("amount") or {}).get("usd"), "volume_24h_usd": (collection.get("volume") or {}).get("1day")})
        return {"source": "Google News + DexScreener + Reservoir", "headlines": headlines[:12], "tickers": sorted({ticker for row in headlines for ticker in extract_tickers(row["title"])}), "liquid_pairs": sorted(pairs, key=lambda pair: pair["liquidity_usd"], reverse=True)[:10], "nft_collections": nfts}

    @staticmethod
    def format_alert(scan: str, payload: dict) -> str:
        if scan == "polymarket":
            picks = payload.get("selected", [])[:3]
            lines = ["InfoTrader Polymarket scan (DRY RUN)", f"Premier League markets: {payload.get('premier_league_markets', 0)}"]
            lines.extend(f"- {item.get('question')} | liq ${item.get('liquidity', 0):,.0f}" for item in picks)
            return "\n".join(lines)
        lines = ["InfoTrader crypto scan", f"Robinhood headlines: {len(payload.get('headlines', []))}", f"Liquid pairs: {len(payload.get('liquid_pairs', []))}"]
        lines.extend(f"- {row.get('title')}" for row in payload.get("headlines", [])[:3])
        return "\n".join(lines)
