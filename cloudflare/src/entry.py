"""InfoTrader Cloudflare Worker: scheduled intelligence pipeline, dry-run only."""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlparse

from workers import Response, WorkerEntrypoint, fetch

from gemini import GeminiRotator
from scanner_helpers import extract_tickers, normalize_market, priority_score, safe_float
from storage import StateStore

POLYMARKET_CRON = "*/15 * * * *"
CRYPTO_CRON = "0 * * * *"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
NEWS_QUERIES = (
    "Robinhood crypto token listing",
    "Robinhood meme coin",
    "Robinhood mint token crypto",
)
OPENSEA_BASE = "https://api.opensea.io/api/v2"


async def fetch_json(url: str, **options):
    response = await fetch(url, **options)
    if not response.ok:
        raise RuntimeError(f"upstream HTTP {response.status} for {url}")
    return await response.json()


def _rss_items(xml: str, limit: int) -> list[dict]:
    items: list[dict] = []
    for block in re.findall(r"<item>(.*?)</item>", xml, flags=re.DOTALL)[:limit]:
        title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", block, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", block, re.DOTALL)
        if title:
            items.append({
                "title": (title.group(1) or title.group(2) or "").strip(),
                "link": (link.group(1).strip() if link else ""),
            })
    return items


async def send_telegram(env, message: str) -> bool:
    token = getattr(env, "TELEGRAM_BOT_TOKEN", None)
    chat_id = getattr(env, "TELEGRAM_CHAT_ID", None)
    if not token or not chat_id:
        print("TELEGRAM NOT CONFIGURED")
        return False
    try:
        response = await fetch(
            f"https://api.telegram.org/bot{token}/sendMessage",
            method="POST",
            headers={"content-type": "application/json"},
            body=json.dumps({"chat_id": chat_id, "text": message[:4000]}),
        )
        if not response.ok:
            print(f"TELEGRAM HTTP ERROR: status={response.status}")
            return False
        print("TELEGRAM SENT")
        return True
    except Exception as exc:
        print(f"TELEGRAM ERROR: {type(exc).__name__}: {exc}")
        return False


def authorized(request, env) -> bool:
    token = getattr(env, "CONTROL_TOKEN", None)
    return bool(token and request.headers.get("Authorization") == f"Bearer {token}")


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = urlparse(request.url).path
        store = StateStore(self.env)
        if path == "/health":
            opensea_key_configured = bool(getattr(self.env, "OPENSEA_API_KEY", None))
            nft_collections_configured = bool(str(getattr(self.env, "NFT_COLLECTIONS", "")).strip())
            return Response.json({
                "ok": True,
                "service": "infotrader",
                "dry_run": True,
                "storage_bound": store.available,
                "gemini_configured": self._has_gemini(),
                "telegram_configured": self._has_telegram(),
                "opensea_configured": opensea_key_configured,
                "opensea_status": (
                    "ready"
                    if opensea_key_configured and nft_collections_configured
                    else "missing_api_key"
                    if not opensea_key_configured
                    else "missing_nft_collections"
                ),
            })
        if path == "/status":
            try:
                status = await store.status()
                return Response.json({"service": "infotrader", "dry_run": True, **status})
            except Exception as exc:
                import traceback
                error_type = type(exc).__name__
                error_message = str(exc)
                print(f"STATUS ERROR: {error_type}: {error_message}")
                print(traceback.format_exc())
                return Response.json({
                    "ok": False,
                    "service": "infotrader",
                    "dry_run": True,
                    "error": error_type,
                    "message": error_message,
                }, status=500)
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

        return Response.json({
            "service": "infotrader",
            "status": "online",
            "dry_run": True,
            "endpoints": ["/health", "/status"],
        })

    async def scheduled(self, controller, env, ctx):
        """Run the scheduled scan; Telegram is reserved for useful scan results/errors."""
        print("CRON HANDLER ENTERED")
        cron = getattr(controller, "cron", None)
        print(f"CRON CONTROLLER: {cron!r}")

        store = StateStore(env)
        print(f"CRON STORAGE AVAILABLE: {store.available}")

        scan = (
            POLYMARKET_CRON
            if cron == POLYMARKET_CRON
            else CRYPTO_CRON
            if cron == CRYPTO_CRON
            else None
        )
        print(f"CRON DISPATCH: scan={scan!r}")

        if not store.available:
            print("CRON STORAGE MISSING: INFOTRADER_STATE is not configured; scheduled scan skipped")
            return

        try:
            await store.record_cron(cron or "unknown", scan)
            print("CRON HEARTBEAT RECORDED")
        except Exception as exc:
            print(f"CRON HEARTBEAT ERROR: {type(exc).__name__}: {exc}")

        if not scan:
            print(f"CRON UNKNOWN: no scan mapped for expression={cron!r}")
            return

        try:
            result = await self.run_scan(scan, store)
            print(f"CRON COMPLETE: scan={scan} status={result.get('status')}")
        except Exception as exc:
            print(f"SCHEDULE ERROR: scan={scan}: {type(exc).__name__}: {exc}")
            await send_telegram(
                env,
                f"InfoTrader scheduled scan error\nType: {type(exc).__name__}\nError: {exc}",
            )

    def _has_gemini(self) -> bool:
        for name in (
            "GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
            "GEMINI_API_KEY_4", "GEMINI_API_KEY_5", "GEMINI_API_KEY",
        ):
            if getattr(self.env, name, None):
                return True
        return False

    def _has_telegram(self) -> bool:
        return bool(
            getattr(self.env, "TELEGRAM_BOT_TOKEN", None)
            and getattr(self.env, "TELEGRAM_CHAT_ID", None)
        )

    async def _record_scan_error(self, store: StateStore, scan: str, exc: Exception) -> dict:
        payload = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            return await store.record_scan(scan, "error", payload)
        except Exception as record_exc:
            print(f"SCAN RECORD ERROR: {scan}: {type(record_exc).__name__}: {record_exc}")
            return {
                "scan": scan,
                "status": "error",
                "payload": payload,
                "storage_error": f"{type(record_exc).__name__}: {record_exc}",
            }

    async def run_scan(self, scan: str, store: StateStore) -> dict:
        try:
            if await store.flag("STOP"):
                return await store.record_scan(scan, "stopped", {})
            if await store.flag("PAUSED"):
                return await store.record_scan(scan, "paused", {})
            payload = await self.scan_polymarket(store) if scan == "polymarket" else await self.scan_crypto(store)
            event = await store.record_scan(scan, "ok", payload)
            telegram_sent = await send_telegram(self.env, self.format_alert(scan, payload))
            print(f"SCAN TELEGRAM: sent={telegram_sent}")
            return event
        except Exception as exc:
            event = await self._record_scan_error(store, scan, exc)
            telegram_sent = await send_telegram(self.env, f"InfoTrader {scan} scan error\nError: {event['payload']['error']}")
            print(f"SCAN TELEGRAM ERROR ALERT: sent={telegram_sent}")
            return event

    async def scan_polymarket(self, store: StateStore) -> dict:
        raw = await fetch_json(GAMMA_MARKETS_URL)
        rows = raw if isinstance(raw, list) else raw.get("markets", [])
        markets = sorted(
            (normalize_market(row) for row in rows if isinstance(row, dict)),
            key=priority_score,
            reverse=True,
        )
        max_markets = int(getattr(self.env, "MAX_MARKETS_PER_RUN", "8"))
        selected = markets[:max_markets]

        research_results = []
        decisions = []
        research_client = None
        decision_client = None
        if self._has_gemini():
            try:
                research_client = GeminiRotator(self.env, "RESEARCH")
                decision_client = GeminiRotator(self.env, "DECISION")
            except Exception as exc:
                research_results.append({"error": f"Gemini unavailable: {exc}"})

        for market in selected[: min(3, len(selected))]:
            subject = market.get("question") or market.get("slug") or "unknown market"
            research = None
            if research_client:
                query = self._research_prompt(market)
                try:
                    research = await research_client.research(query)
                    research_results.append({"subject": subject, **research})
                    try:
                        await store.record_research("polymarket", subject, research)
                    except Exception as exc:
                        research_results.append({"subject": subject, "storage_error": f"{type(exc).__name__}: {exc}"})
                except Exception as exc:
                    research_results.append({"subject": subject, "error": f"{type(exc).__name__}: {exc}"})
            if decision_client and research and research.get("text"):
                try:
                    decision = await decision_client.decide(self._decision_prompt(market, research))
                    decisions.append({"subject": subject, **decision})
                    try:
                        await store.record_decision("polymarket", subject, decision)
                    except Exception as exc:
                        decisions.append({"subject": subject, "storage_error": f"{type(exc).__name__}: {exc}"})
                except Exception as exc:
                    decisions.append({"subject": subject, "error": f"{type(exc).__name__}: {exc}"})

        return {
            "source": "Polymarket Gamma + Gemini",
            "markets_seen": len(markets),
            "premier_league_markets": sum(m["premier_league"] for m in markets),
            "selected": selected,
            "research": research_results,
            "decisions": decisions,
            "live_execution": False,
        }

    async def scan_crypto(self, store: StateStore) -> dict:
        headlines: list[dict] = []
        news_errors: list[str] = []
        seen: set[str] = set()
        for query in NEWS_QUERIES:
            response = await fetch(f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en")
            if not response.ok:
                news_errors.append(f"{query}: HTTP {response.status}")
                continue
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
                    pairs.append({
                        "symbol": (pair.get("baseToken") or {}).get("symbol"),
                        "chain": pair.get("chainId"),
                        "dex": pair.get("dexId"),
                        "liquidity_usd": liquidity,
                        "volume_24h_usd": safe_float(((pair.get("volume") or {}).get("h24"))),
                        "url": pair.get("url"),
                    })

        nft_collections = [slug.strip() for slug in str(getattr(self.env, "NFT_COLLECTIONS", "")).split(",") if slug.strip()][:5]
        nfts = []
        opensea_key = getattr(self.env, "OPENSEA_API_KEY", None)
        for slug in nft_collections:
            if not opensea_key:
                break
            response = await fetch(
                f"{OPENSEA_BASE}/collections/{quote(slug)}/stats",
                headers={"x-api-key": opensea_key, "accept": "application/json"},
            )
            if not response.ok:
                continue
            stats = await response.json()
            total = stats.get("total") or {}
            nfts.append({
                "slug": slug,
                "floor_price": total.get("floor_price"),
                "volume": total.get("volume"),
                "sales": total.get("sales"),
                "owners": total.get("num_owners"),
                "source": "OpenSea",
            })

        gemini_notes = None
        if self._has_gemini() and (headlines or pairs):
            try:
                client = GeminiRotator(self.env, "RESEARCH")
                gemini_notes = await client.research(self._crypto_research_prompt(headlines, pairs, nfts))
                try:
                    await store.record_research("crypto", "Robinhood/crypto intelligence", gemini_notes)
                except Exception as exc:
                    gemini_notes = {
                        **gemini_notes,
                        "storage_error": f"{type(exc).__name__}: {exc}",
                    }
            except Exception as exc:
                gemini_notes = {"error": f"{type(exc).__name__}: {exc}"}

        return {
            "source": "Google News + DexScreener + OpenSea + Gemini",
            "headlines": headlines[:12],
            "news_errors": news_errors,
            "tickers": sorted({ticker for row in headlines for ticker in extract_tickers(row["title"])}),
            "liquid_pairs": sorted(pairs, key=lambda pair: pair["liquidity_usd"], reverse=True)[:10],
            "nft_collections": nfts,
            "opensea_configured": bool(opensea_key),
            "nft_collections_configured": bool(nft_collections),
            "research": gemini_notes,
        }

    @staticmethod
    def _research_prompt(market: dict) -> str:
        return json.dumps({
            "task": "Research this Polymarket market using current web information.",
            "market": market,
            "requirements": [
                "Find the most decision-relevant recent facts.",
                "For Premier League markets, prioritize official Premier League/team sources plus reputable sports reporting.",
                "Check injuries, suspensions, lineup/team news, form, table position and xG when relevant.",
                "State what is known, what is uncertain, and the publication dates of important facts.",
                "Do not fabricate odds, sources, injuries, or events.",
            ],
        })

    @staticmethod
    def _decision_prompt(market: dict, research: dict) -> str:
        return json.dumps({
            "task": "Evaluate this prediction market. Return JSON only.",
            "market": market,
            "research": research,
            "output_schema": {
                "action": "BUY_YES | BUY_NO | PASS",
                "outcome": "exact outcome label from the market, or empty string",
                "fair_probability": "number between 0 and 1",
                "market_probability": "number between 0 and 1 when available",
                "edge": "fair_probability - market_probability for the selected outcome, or 0",
                "confidence": "number between 0 and 1",
                "reason": "concise evidence-based explanation",
            },
            "rules": [
                "PASS when evidence is insufficient or edge is not meaningful.",
                "Never claim that an order was placed.",
            ],
        })

    @staticmethod
    def _crypto_research_prompt(headlines: list[dict], pairs: list[dict], nfts: list[dict]) -> str:
        return json.dumps({
            "task": "Assess current crypto intelligence for potential material developments.",
            "headlines": headlines[:10],
            "liquid_pairs": pairs[:10],
            "nft_collections": nfts[:5],
            "requirements": [
                "Focus on Robinhood-related listings/mints and material meme-coin developments.",
                "Distinguish confirmed announcements from speculation.",
                "Highlight symbols or developments worth monitoring next cycle.",
                "Use current web sources and dates; do not invent facts.",
            ],
        })

    @staticmethod
    def format_alert(scan: str, payload: dict) -> str:
        if scan == "polymarket":
            picks = payload.get("selected", [])[:5]
            lines = [
                "InfoTrader Polymarket scan (DRY RUN)",
                f"Markets scanned: {payload.get('markets_seen', 0)}",
                f"Premier League matches: {payload.get('premier_league_markets', 0)}",
                f"Gemini research: {len(payload.get('research', []))}",
                f"AI decisions: {len(payload.get('decisions', []))}",
                "",
                "Top markets:",
            ]
            for item in picks:
                prices = item.get("outcome_prices") or []
                price_text = ", ".join(
                    f"{item.get('outcomes', [])[i] if i < len(item.get('outcomes', [])) else 'Outcome'} {float(prices[i]):.1%}"
                    for i in range(min(len(prices), len(item.get("outcomes") or [])))
                    if str(prices[i]).replace('.', '', 1).isdigit()
                )
                lines.append(
                    f"• {item.get('question') or item.get('slug')}\n"
                    f"  Liquidity: ${item.get('liquidity', 0):,.0f} | 24h vol: ${item.get('volume_24h', 0):,.0f}"
                    + (f"\n  Prices: {price_text}" if price_text else "")
                )
            decisions = payload.get("decisions", [])
            if decisions:
                lines.extend(["", "AI decisions:"])
                for item in decisions[:3]:
                    result = item.get("result") if isinstance(item.get("result"), dict) else item
                    text = result.get("text") if isinstance(result, dict) else None
                    if text:
                        lines.append(f"• {item.get('subject')}: {text[:900]}")
            return "\n".join(lines)

        pairs = payload.get("liquid_pairs", [])[:5]
        lines = [
            "InfoTrader crypto scan (DRY RUN)",
            f"Robinhood headlines: {len(payload.get('headlines', []))}",
            f"Liquid pairs: {len(payload.get('liquid_pairs', []))}",
            f"Tickers detected: {', '.join(payload.get('tickers', [])[:8]) or 'none'}",
            f"OpenSea API: {'configured' if payload.get('opensea_configured') else 'not configured'}",
            f"NFT collections watched: {len(payload.get('nft_collections', []))}",
        ]
        news_errors = payload.get("news_errors", [])
        if news_errors:
            lines.append(f"News feed errors: {len(news_errors)}")
        if pairs:
            lines.extend(["", "Top liquid pairs:"])
            for pair in pairs:
                lines.append(
                    f"• {pair.get('symbol') or '?'} on {pair.get('dex') or '?'} ({pair.get('chain') or '?'}) "
                    f"liq ${pair.get('liquidity_usd', 0):,.0f} | 24h ${pair.get('volume_24h_usd', 0):,.0f}"
                )
        headlines = payload.get("headlines", [])
        if headlines:
            lines.extend(["", "Latest headlines:"])
            lines.extend(f"• {row.get('title')}" for row in headlines[:5])
        research = payload.get("research")
        if isinstance(research, dict) and research.get("text"):
            lines.extend(["", "AI assessment:", research["text"][:1200]])
        elif isinstance(research, dict) and research.get("error"):
            lines.extend(["", f"AI assessment error: {research['error']}"])
        return "\n".join(lines)
