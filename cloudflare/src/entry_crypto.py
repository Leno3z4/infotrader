"""Crypto scanner: resilient discovery, compact data, and text intelligence."""

from __future__ import annotations

import math
import re
from urllib.parse import quote

from workers import fetch

from entry_scheduled import Default as ScheduledDefault
from scanner_helpers import extract_tickers, safe_float

DEX_BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_BOOSTS_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKENS_URL = "https://api.dexscreener.com/tokens/v1"
NEWS_QUERIES = (
    "crypto meme coin",
    "new meme coin crypto",
    "Robinhood crypto listing",
    "meme coin news",
)
OPENSEA_BASE = "https://api.opensea.io/api/v2"


class Default(ScheduledDefault):
    """Use corrected cron dispatch plus resilient, ranked crypto intelligence."""

    async def _json(self, url: str) -> tuple[object | None, str | None]:
        try:
            response = await fetch(url, headers={"accept": "application/json"})
            if response.ok:
                return await response.json(), None
            return None, f"HTTP {response.status}"
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    async def scan_crypto(self, store) -> dict:
        headlines: list[dict] = []
        news_errors: list[str] = []
        seen_news: set[str] = set()
        for query in NEWS_QUERIES:
            try:
                response = await fetch(
                    f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
                )
                if not response.ok:
                    news_errors.append(f"{query}: HTTP {response.status}")
                    continue
                for item in self._rss_items(await response.text(), 4):
                    if item["link"] and item["link"] not in seen_news:
                        seen_news.add(item["link"])
                        headlines.append(item)
            except Exception as exc:
                news_errors.append(f"{query}: {type(exc).__name__}: {exc}")

        candidates: dict[str, dict] = {}
        boost_data, boost_error = await self._json(DEX_BOOSTS_TOP_URL)
        source_used = "top boosts"
        if boost_data is None:
            boost_data, fallback_error = await self._json(DEX_BOOSTS_LATEST_URL)
            source_used = "latest boosts fallback"
            if boost_data is None:
                boost_error = f"top={boost_error}; latest={fallback_error}"
        for row in list(boost_data or [])[:40]:
            if not isinstance(row, dict):
                continue
            chain, address = row.get("chainId"), row.get("tokenAddress")
            if chain and address:
                candidates[f"{str(chain).lower()}:{str(address).lower()}"] = row

        if not candidates:
            profile_data, profile_error = await self._json(DEX_PROFILES_URL)
            if profile_data is not None:
                source_used = "latest token profiles fallback"
                for row in list(profile_data or [])[:40]:
                    if not isinstance(row, dict):
                        continue
                    chain, address = row.get("chainId"), row.get("tokenAddress")
                    if chain and address:
                        candidates[f"{str(chain).lower()}:{str(address).lower()}"] = row
            else:
                boost_error = f"{boost_error}; profiles={profile_error}"

        min_liquidity = float(getattr(self.env, "MIN_LIQUIDITY_USD", "5000"))
        min_volume = float(getattr(self.env, "MIN_VOLUME_24H_USD", "1000"))
        max_candidates = min(40, int(getattr(self.env, "MAX_BOOST_CANDIDATES", "25")))
        max_tokens = int(getattr(self.env, "MAX_POTENTIAL_TOKENS", "10"))

        candidates = dict(list(candidates.items())[:max_candidates])
        grouped: dict[str, list[str]] = {}
        for key in candidates:
            chain, address = key.split(":", 1)
            grouped.setdefault(chain, []).append(address)

        pair_rows: dict[str, dict] = {}
        enrichment_errors: list[str] = []
        for chain, addresses in grouped.items():
            for start in range(0, len(addresses), 30):
                chunk = addresses[start:start + 30]
                url = f"{DEX_TOKENS_URL}/{quote(chain, safe='')}/{','.join(quote(a, safe='') for a in chunk)}"
                data, error = await self._json(url)
                if error:
                    enrichment_errors.append(f"{chain}: {error}")
                    continue
                rows = data if isinstance(data, list) else []
                for pair in rows:
                    if not isinstance(pair, dict):
                        continue
                    base = pair.get("baseToken") or {}
                    addr = base.get("address")
                    if not addr:
                        continue
                    key = f"{str(pair.get('chainId') or chain).lower()}:{str(addr).lower()}"
                    current = pair_rows.get(key)
                    pair_liq = safe_float(((pair.get("liquidity") or {}).get("usd")))
                    pair_vol = safe_float(((pair.get("volume") or {}).get("h24")))
                    if current is None or (pair_liq, pair_vol) > (
                        safe_float(((current.get("liquidity") or {}).get("usd"))),
                        safe_float(((current.get("volume") or {}).get("h24"))),
                    ):
                        pair_rows[key] = pair

        by_token: dict[str, dict] = {}
        for key, candidate in candidates.items():
            pair = pair_rows.get(key)
            if not pair:
                continue
            liquidity = safe_float(((pair.get("liquidity") or {}).get("usd")))
            volume_24h = safe_float(((pair.get("volume") or {}).get("h24")))
            if liquidity < min_liquidity or volume_24h < min_volume:
                continue
            base = pair.get("baseToken") or {}
            txns = pair.get("txns") or {}
            h24 = txns.get("h24") or {}
            changes = pair.get("priceChange") or {}
            buys = int(safe_float(h24.get("buys")))
            sells = int(safe_float(h24.get("sells")))
            tx_count = buys + sells
            boost_amount = safe_float(candidate.get("totalAmount"))
            momentum = max(-50.0, min(100.0, safe_float(changes.get("h24"))))
            buy_ratio = buys / max(1, tx_count)
            score = (
                math.log10(max(liquidity, 1.0)) * 0.50
                + math.log10(max(volume_24h, 1.0)) * 0.28
                + math.log10(max(tx_count, 1.0)) * 0.12
                + (buy_ratio - 0.5) * 0.06
                + math.log10(max(boost_amount, 1.0)) * 0.04
                + max(-0.2, min(0.2, momentum / 250.0))
            )
            info = pair.get("info") or {}
            by_token[key] = {
                "rank_score": round(score, 4),
                "name": base.get("name") or "Unknown",
                "symbol": base.get("symbol") or "UNKNOWN",
                "chain": pair.get("chainId"),
                "dex": pair.get("dexId"),
                "price_usd": safe_float(pair.get("priceUsd")),
                "change_1h_pct": safe_float(changes.get("h1")),
                "change_6h_pct": safe_float(changes.get("h6")),
                "change_24h_pct": momentum,
                "liquidity_usd": liquidity,
                "volume_24h_usd": volume_24h,
                "market_cap_usd": safe_float(pair.get("marketCap")),
                "fdv_usd": safe_float(pair.get("fdv")),
                "txns_24h": {"buys": buys, "sells": sells, "total": tx_count},
                "buy_ratio_24h": round(buy_ratio, 3),
                "boost_amount": boost_amount,
                "description": (candidate.get("description") or "")[:300],
                "websites": [x.get("url") for x in list(info.get("websites") or [])[:2] if isinstance(x, dict) and x.get("url")],
                "socials": [x for x in list(info.get("socials") or [])[:2] if isinstance(x, dict)],
                "pair_url": pair.get("url") or candidate.get("url"),
                "token_address": base.get("address"),
                "source": "DexScreener",
            }

        potential_tokens = sorted(by_token.values(), key=lambda x: x["rank_score"], reverse=True)[:max_tokens]
        enrichment_available = bool(pair_rows)

        nfts: list[dict] = []
        nft_collections = [s.strip() for s in str(getattr(self.env, "NFT_COLLECTIONS", "")).split(",") if s.strip()][:5]
        opensea_key = getattr(self.env, "OPENSEA_API_KEY", None)
        for slug in nft_collections:
            if not opensea_key:
                break
            data, _ = await self._json(f"{OPENSEA_BASE}/collections/{quote(slug)}/stats")
            if isinstance(data, dict):
                total = data.get("total") or {}
                nfts.append({"slug": slug, "floor_price": total.get("floor_price"), "volume": total.get("volume"), "sales": total.get("sales"), "owners": total.get("num_owners"), "source": "OpenSea"})

        ai_text = ""
        ai_error = None
        if self._has_gemini() and potential_tokens:
            try:
                from gemini import GeminiRotator
                client = GeminiRotator(self.env, "RESEARCH")
                packet = {
                    "task": "Rank the top potential meme coins from current on-chain metrics plus current online information.",
                    "tokens": [
                        {k: t.get(k) for k in ("name", "symbol", "chain", "dex", "price_usd", "change_1h_pct", "change_6h_pct", "change_24h_pct", "liquidity_usd", "volume_24h_usd", "market_cap_usd", "txns_24h", "buy_ratio_24h", "boost_amount", "description", "websites", "socials", "pair_url")}
                        for t in potential_tokens
                    ],
                    "news": headlines[:8],
                    "rules": [
                        "Use Google Search grounding to check recent online information for the named tokens.",
                        "Prioritize credible/current sources and distinguish facts from hype.",
                        "Consider liquidity, volume, transaction balance, momentum, narrative/news, project credibility and obvious scam/risk signals.",
                        "Do not invent token facts, partnerships, listings, audits, holders, or prices.",
                        "Return concise plain text, not JSON or markdown tables.",
                        "Give a 1-10 ranking, why it ranks there, key bullish/bearish facts, risk, and whether it is worth further monitoring.",
                        "Keep the whole report under 1100 words.",
                    ],
                }
                result = await client.research(json.dumps(packet, separators=(",", ":")))
                ai_text = str(result.get("text") or "").strip()
                await store.record_research("crypto", "meme-coin top-10", result)
            except Exception as exc:
                ai_error = f"{type(exc).__name__}: {exc}"

        text_report = self._build_text_report(potential_tokens, ai_text, headlines, candidates, source_used, boost_error, enrichment_errors, enrichment_available)
        return {
            "source": "DexScreener + Google News + Gemini",
            "candidate_source": source_used,
            "candidate_count": len(potential_tokens),
            "potential_tokens": potential_tokens,
            "top_10": potential_tokens,
            "headlines": headlines[:8],
            "news_errors": news_errors,
            "dex_errors": [x for x in [boost_error] if x] + enrichment_errors[:10],
            "nft_collections": nfts,
            "opensea_configured": bool(opensea_key),
            "ai_report": ai_text,
            "ai_error": ai_error,
            "text_report": text_report,
        }

    @staticmethod
    def _build_text_report(
        tokens: list[dict],
        ai_text: str,
        headlines: list[dict],
        candidates: dict[str, dict],
        source_used: str,
        boost_error: str | None,
        enrichment_errors: list[str],
        enrichment_available: bool,
    ) -> str:
        lines = ["🔥 INFOTRADER MEME-COIN INTELLIGENCE"]
        if tokens:
            lines.append(f"Top {len(tokens)} current candidates by on-chain quality + current information.")
        elif candidates and not enrichment_available:
            lines.append("Candidate tokens were found, but on-chain enrichment is temporarily unavailable.")
            lines.append("This run is NOT treating the outage as evidence that there are zero candidates.")
        else:
            lines.append("No qualifying on-chain candidates passed the current liquidity/volume filters in this run.")
        lines.append("")
        for i, t in enumerate(tokens, 1):
            lines.append(
                f"{i}. {t.get('name')} (${t.get('symbol')}) — {t.get('chain')} / {t.get('dex')}\n"
                f"   Liquidity ${t.get('liquidity_usd', 0):,.0f} | 24h vol ${t.get('volume_24h_usd', 0):,.0f} | "
                f"24h {t.get('change_24h_pct', 0):+.2f}% | buys/sells {t.get('txns_24h', {}).get('buys', 0):,}/{t.get('txns_24h', {}).get('sells', 0):,}\n"
                f"   Pair: {t.get('pair_url')}"
            )
        if ai_text:
            lines.extend(["", "🧠 GEMINI ONLINE READ", ai_text])
        if headlines:
            lines.extend(["", "📰 RECENT CRYPTO / MEME-COIN NEWS"])
            lines.extend(f"• {h.get('title')}" for h in headlines[:5])
        diagnostics = [x for x in [boost_error] if x] + enrichment_errors[:3]
        if diagnostics:
            lines.extend(["", f"⚠️ DATA NOTES ({source_used})"])
            lines.extend(f"• {x}" for x in diagnostics)
        return "\n".join(lines)[:3900]

    @staticmethod
    def _rss_items(xml: str, limit: int) -> list[dict]:
        items: list[dict] = []
        for block in re.findall(r"<item>(.*?)</item>", xml, flags=re.DOTALL)[:limit]:
            title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", block, re.DOTALL)
            link = re.search(r"<link>(.*?)</link>", block, re.DOTALL)
            if title:
                items.append({"title": (title.group(1) or title.group(2) or "").strip(), "link": link.group(1).strip() if link else ""})
        return items

    @staticmethod
    def format_alert(scan: str, payload: dict) -> str:
        if scan != "crypto":
            return ScheduledDefault.format_alert(scan, payload)
        return payload.get("text_report") or "InfoTrader crypto scan: no current candidates found."
