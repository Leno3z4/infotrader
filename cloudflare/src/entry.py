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
        print("TELEGRAM SKIPPED: token/chat id not configured")
        return False
    try:
        response = await fetch(
            f"https://api.telegram.org/bot{token}/sendMessage",
            method="POST",
            headers={"content-type": "application/json"},
            body=json.dumps({"chat_id": chat_id, "text": message[:4000]}),
        )
        if response.ok:
            print("TELEGRAM SENT: ok")
            return True
        print(f"TELEGRAM FAILED: HTTP {response.status}")
        return False
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
            return Response.json({
                "ok": True,
                "service": "infotrader",
                "dry_run": True,
                "storage_bound": store.available,
                "gemini_configured": self._has_gemini(),
                "telegram_configured": self._has_telegram(),
                "opensea_configured": bool(getattr(self.env, "OPENSEA_API_KEY", None)),
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
        print("INFOTRADER CRON HANDLER ENTERED")
        try:
            # Use the WorkerEntrypoint environment consistently. This is the same
            # environment used by fetch(), including KV bindings and secrets.
            runtime_env = self.env
            cron = getattr(controller, "cron", None)
            scheduled_time = getattr(controller, "scheduledTime", None)
            print(f"CRON CONTROLLER: cron={cron!r} scheduledTime={scheduled_time!r}")

            store = StateStore(runtime_env)
            print(f"CRON STORAGE: available={store.available}")
            if not store.available:
                print("CRON STORAGE MISSING: INFOTRADER_STATE is not configured")
                return

            scan = (
                POLYMARKET_CRON
                if cron == POLYMARKET_CRON
                else CRYPTO_CRON
                if cron == CRYPTO_CRON
                else None
            )
            print(f"CRON DISPATCH: scan={scan!r}")

            # Send a proof-of-execution message before the scan. This also verifies
            # that scheduled() can access the configured secrets at runtime.
            telegram_sent = await send_telegram(
                runtime_env,
                f"InfoTrader cron fired (DRY RUN)\nScan: {scan or 'unknown'}\nCron: {cron or 'unknown'}",
            )
            print(f"CRON TELEGRAM DIAGNOSTIC: sent={telegram_sent}")

            try:
                await store.record_cron(cron or "unknown", scan)
                print("CRON HEARTBEAT: recorded")
            except Exception as exc:
                print(f"CRON HEARTBEAT ERROR: {type(exc).__name__}: {exc}")

            if not scan:
                print(f"CRON UNKNOWN: no scan mapped for expression={cron!r}")
                return

            print(f"CRON SCAN START: {scan}")
            result = await self.run_scan(scan, store)
            print(f"CRON SCAN COMPLETE: scan={scan} status={result.get('status')}")
        except Exception as exc:
            import traceback
            print(f"CRON FATAL ERROR: {type(exc).__name__}: {exc}")
            print(traceback.format_exc())

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
            await send_telegram(self.env, self.format_alert(scan, payload))
            return event
        except Exception as exc:
            event = await self._record_scan_error(store, scan, exc)
            await send_telegram(self.env, f"InfoTrader {scan} scan error: {event['payload']['error']}")
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
            "tickers": sorted({ticker for row in headlines for ticker in extract_tickers(row["title"])}),
            "liquid_pairs": sorted(pairs, key=lambda pair: pair["liquidity_usd"], reverse=True)[:10],
            "nft_collections": nfts,
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
            picks = payload.get("selected", [])[:3]
            lines = [
                "InfoTrader Polymarket scan (DRY RUN)",
                f"Premier League markets: {payload.get('premier_league_markets', 0)}",
            ]
            lines.extend(
                f"- {item.get('question')} | liq ${item.get('liquidity', 0):,.0f}"
                for item in picks
            )
            decisions = payload.get("decisions", [])
            for item in decisions[:3]:
                result = item.get("result") if isinstance(item.get("result"), dict) else item
                text = result.get("text") if isinstance(result, dict) else None
                if text:
                    lines.append(f"Decision for {item.get('subject')}: {text[:700]}")
            return "\n".join(lines)
        lines = [
            "InfoTrader crypto scan",
            f"Robinhood headlines: {len(payload.get('headlines', []))}",
            f"Liquid pairs: {len(payload.get('liquid_pairs', []))}",
            f"OpenSea NFT collections: {len(payload.get('nft_collections', []))}",
        ]
        lines.extend(f"- {row.get('title')}" for row in payload.get("headlines", [])[:3])
        return "\n".join(lines)
