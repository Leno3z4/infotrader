"""Crypto scanner specialization: enrich DexScreener boost candidates into ranked tokens."""

from __future__ import annotations

import math
from urllib.parse import quote

from workers import fetch

from entry_scheduled import Default as ScheduledDefault
from scanner_helpers import extract_tickers, safe_float

DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
NEWS_QUERIES = (
    "Robinhood crypto token listing",
    "Robinhood meme coin",
    "Robinhood mint token crypto",
)
OPENSEA_BASE = "https://api.opensea.io/api/v2"


class Default(ScheduledDefault):
    """Use corrected cron dispatch plus an enriched crypto scanner."""

    async def scan_crypto(self, store) -> dict:
        headlines: list[dict] = []
        news_errors: list[str] = []
        seen: set[str] = set()

        for query in NEWS_QUERIES:
            try:
                response = await fetch(
                    f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
                )
                if not response.ok:
                    news_errors.append(f"{query}: HTTP {response.status}")
                    continue
                text = await response.text()
                for item in self._rss_items(text, 4):
                    if item["link"] and item["link"] not in seen:
                        seen.add(item["link"])
                        headlines.append(item)
            except Exception as exc:
                news_errors.append(f"{query}: {type(exc).__name__}: {exc}")

        boosted = await self._fetch_json(DEX_BOOSTS_URL)
        min_liquidity = float(getattr(self.env, "MIN_LIQUIDITY_USD", "5000"))
        min_volume = float(getattr(self.env, "MIN_VOLUME_24H_USD", "1000"))
        max_boosts = int(getattr(self.env, "MAX_BOOST_CANDIDATES", "25"))
        max_tokens = int(getattr(self.env, "MAX_POTENTIAL_TOKENS", "10"))

        potential_tokens: list[dict] = []
        enrichment_errors: list[str] = []

        for boost in list(boosted or [])[:max_boosts]:
            if not isinstance(boost, dict):
                continue
            chain = boost.get("chainId")
            address = boost.get("tokenAddress")
            if not chain or not address:
                continue

            try:
                raw_pairs = await self._fetch_json(
                    f"https://api.dexscreener.com/tokens/v1/{quote(str(chain), safe='')}/{quote(str(address), safe='')}"
                )
            except Exception as exc:
                enrichment_errors.append(f"{chain}:{address}: {type(exc).__name__}: {exc}")
                continue

            pairs = [pair for pair in list(raw_pairs or []) if isinstance(pair, dict)]
            if not pairs:
                continue

            # A token can have multiple pools. Pick its best-liquidity pool so
            # the result represents the actual market rather than the first API row.
            pair = max(
                pairs,
                key=lambda row: (
                    safe_float(((row.get("liquidity") or {}).get("usd"))),
                    safe_float(((row.get("volume") or {}).get("h24"))),
                ),
            )

            liquidity = safe_float(((pair.get("liquidity") or {}).get("usd")))
            volume_24h = safe_float(((pair.get("volume") or {}).get("h24")))
            if liquidity < min_liquidity or volume_24h < min_volume:
                continue

            base = pair.get("baseToken") or {}
            quote_token = pair.get("quoteToken") or {}
            txns = pair.get("txns") or {}
            tx_24h = txns.get("h24") or {}
            tx_1h = txns.get("h1") or {}
            price_change = pair.get("priceChange") or {}

            buys_24h = int(safe_float(tx_24h.get("buys")))
            sells_24h = int(safe_float(tx_24h.get("sells")))
            tx_count_24h = buys_24h + sells_24h
            boost_amount = safe_float(boost.get("totalAmount"))

            # Stable ranking: liquidity first, then volume, then transaction depth.
            quality_score = (
                math.log10(max(liquidity, 1.0)) * 0.55
                + math.log10(max(volume_24h, 1.0)) * 0.30
                + math.log10(max(tx_count_24h, 1.0)) * 0.10
                + math.log10(max(boost_amount, 1.0)) * 0.05
            )

            socials = [
                link for link in (boost.get("links") or [])
                if isinstance(link, dict) and link.get("url")
            ]

            potential_tokens.append({
                "rank_score": round(quality_score, 4),
                "symbol": base.get("symbol"),
                "name": base.get("name"),
                "chain": pair.get("chainId") or chain,
                "token_address": base.get("address") or address,
                "pair_address": pair.get("pairAddress"),
                "dex": pair.get("dexId"),
                "pair_url": pair.get("url") or boost.get("url"),
                "price_usd": safe_float(pair.get("priceUsd")),
                "price_change_5m_pct": safe_float(price_change.get("m5")),
                "price_change_1h_pct": safe_float(price_change.get("h1")),
                "price_change_6h_pct": safe_float(price_change.get("h6")),
                "price_change_24h_pct": safe_float(price_change.get("h24")),
                "liquidity_usd": liquidity,
                "liquidity_base": safe_float(((pair.get("liquidity") or {}).get("base"))),
                "liquidity_quote": safe_float(((pair.get("liquidity") or {}).get("quote"))),
                "volume_5m_usd": safe_float(((pair.get("volume") or {}).get("m5"))),
                "volume_1h_usd": safe_float(((pair.get("volume") or {}).get("h1"))),
                "volume_6h_usd": safe_float(((pair.get("volume") or {}).get("h6"))),
                "volume_24h_usd": volume_24h,
                "market_cap_usd": safe_float(pair.get("marketCap")),
                "fdv_usd": safe_float(pair.get("fdv")),
                "txns_1h": {
                    "buys": int(safe_float(tx_1h.get("buys"))),
                    "sells": int(safe_float(tx_1h.get("sells"))),
                },
                "txns_24h": {
                    "buys": buys_24h,
                    "sells": sells_24h,
                    "total": tx_count_24h,
                },
                "pair_created_at": pair.get("pairCreatedAt"),
                "boost_amount": boost_amount,
                "description": boost.get("description"),
                "project_links": socials,
                "quote_symbol": quote_token.get("symbol"),
                "source": "DexScreener Boosts + token pair enrichment",
            })

        potential_tokens.sort(key=lambda row: row["rank_score"], reverse=True)
        potential_tokens = potential_tokens[:max_tokens]

        nft_collections = [
            slug.strip()
            for slug in str(getattr(self.env, "NFT_COLLECTIONS", "")).split(",")
            if slug.strip()
        ][:5]
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
        if self._has_gemini() and (headlines or potential_tokens):
            try:
                from gemini import GeminiRotator
                client = GeminiRotator(self.env, "RESEARCH")
                gemini_notes = await client.research(
                    self._crypto_research_prompt(headlines, potential_tokens, nfts)
                )
                try:
                    await store.record_research("crypto", "Robinhood/crypto intelligence", gemini_notes)
                except Exception as exc:
                    gemini_notes = {**gemini_notes, "storage_error": f"{type(exc).__name__}: {exc}"}
            except Exception as exc:
                gemini_notes = {"error": f"{type(exc).__name__}: {exc}"}

        return {
            "source": "Google News + DexScreener + OpenSea + Gemini",
            "headlines": headlines[:12],
            "news_errors": news_errors,
            "tickers": sorted({ticker for row in headlines for ticker in extract_tickers(row["title"])}),
            "potential_tokens": potential_tokens,
            "candidate_count": len(potential_tokens),
            "min_liquidity_usd": min_liquidity,
            "min_volume_24h_usd": min_volume,
            "enrichment_errors": enrichment_errors[:20],
            "nft_collections": nfts,
            "opensea_configured": bool(opensea_key),
            "nft_collections_configured": bool(nft_collections),
            "research": gemini_notes,
        }

    async def _fetch_json(self, url: str):
        response = await fetch(url, headers={"accept": "application/json"})
        if not response.ok:
            raise RuntimeError(f"upstream HTTP {response.status} for {url}")
        return await response.json()

    @staticmethod
    def _rss_items(xml: str, limit: int) -> list[dict]:
        items: list[dict] = []
        import re
        for block in re.findall(r"<item>(.*?)</item>", xml, flags=re.DOTALL)[:limit]:
            title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", block, re.DOTALL)
            link = re.search(r"<link>(.*?)</link>", block, re.DOTALL)
            if title:
                items.append({
                    "title": (title.group(1) or title.group(2) or "").strip(),
                    "link": link.group(1).strip() if link else "",
                })
        return items

    @staticmethod
    def format_alert(scan: str, payload: dict) -> str:
        if scan != "crypto":
            return ScheduledDefault.format_alert(scan, payload)

        tokens = payload.get("potential_tokens", [])
        lines = [
            "InfoTrader crypto scan (DRY RUN)",
            f"Potential tokens: {len(tokens)}",
            f"Liquidity floor: ${payload.get('min_liquidity_usd', 0):,.0f}",
            f"24h volume floor: ${payload.get('min_volume_24h_usd', 0):,.0f}",
            f"Robinhood headlines: {len(payload.get('headlines', []))}",
            "",
            "🔥 Potential tokens with liquidity + volume",
        ]

        for index, token in enumerate(tokens[:8], 1):
            symbol = token.get("symbol") or "UNKNOWN"
            name = token.get("name") or symbol
            links = token.get("project_links") or []
            social_text = ", ".join(
                str(link.get("url")) for link in links[:3] if isinstance(link, dict) and link.get("url")
            )
            lines.extend([
                f"{index}. {name} (${symbol})",
                f"   Chain/Dex: {token.get('chain')} / {token.get('dex')}",
                f"   Price: ${token.get('price_usd', 0):,.8f}",
                f"   Liquidity: ${token.get('liquidity_usd', 0):,.0f}",
                f"   24h volume: ${token.get('volume_24h_usd', 0):,.0f}",
                f"   24h change: {token.get('price_change_24h_pct', 0):+.2f}%",
                f"   Market cap: ${token.get('market_cap_usd', 0):,.0f}",
                f"   FDV: ${token.get('fdv_usd', 0):,.0f}",
                f"   24h txns: {token.get('txns_24h', {}).get('total', 0):,} "
                f"(buys {token.get('txns_24h', {}).get('buys', 0):,} / sells {token.get('txns_24h', {}).get('sells', 0):,})",
                f"   Contract: {token.get('token_address')}",
                f"   Pair: {token.get('pair_url')}",
            ])
            if social_text:
                lines.append(f"   Links: {social_text}")
            if token.get("description"):
                description = " ".join(str(token["description"]).split())
                lines.append(f"   About: {description[:280]}")
            lines.append("")

        headlines = payload.get("headlines", [])
        if headlines:
            lines.extend(["Latest Robinhood/crypto headlines:"])
            lines.extend(f"• {row.get('title')}" for row in headlines[:5])
        return "\n".join(lines)[:3900]
