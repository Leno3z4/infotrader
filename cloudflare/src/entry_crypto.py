"""Crypto scanner: X-first meme discovery with resilient on-chain validation."""

from __future__ import annotations

import json
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
DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search/?q="
X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
X_QUERY = "(memecoin OR \"meme coin\" OR $SOL OR $ETH) lang:en -is:retweet"
OPENSEA_BASE = "https://api.opensea.io/api/v2"


class Default(ScheduledDefault):
    async def _json(self, url: str, headers: dict | None = None) -> tuple[object | None, str | None]:
        try:
            response = await fetch(url, headers={"accept": "application/json", **(headers or {})})
            if response.ok:
                return await response.json(), None
            return None, f"HTTP {response.status}"
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    async def _x_posts(self) -> tuple[list[dict], str | None]:
        token = str(getattr(self.env, "X_BEARER_TOKEN", "") or "").strip()
        if not token:
            return [], "X_BEARER_TOKEN is not configured"
        params = "?query=" + quote(str(getattr(self.env, "X_MEME_QUERY", X_QUERY)))
        params += "&max_results=25&tweet.fields=created_at,public_metrics,text"
        data, error = await self._json(X_SEARCH_URL + params, {"authorization": f"Bearer {token}"})
        if error or not isinstance(data, dict):
            return [], error or "invalid X response"
        posts = []
        for row in list(data.get("data") or [])[:25]:
            if isinstance(row, dict) and row.get("text"):
                posts.append({"id": row.get("id"), "text": str(row.get("text"))[:500], "created_at": row.get("created_at"), "metrics": row.get("public_metrics") or {}})
        return posts, None

    async def _discover_from_x(self, posts: list[dict], limit: int) -> tuple[dict[str, dict], list[str]]:
        symbols: dict[str, int] = {}
        for post in posts:
            for symbol in extract_tickers(post.get("text") or ""):
                symbol = str(symbol).upper().strip("$")
                if 2 <= len(symbol) <= 12 and symbol not in {"USD", "USDT", "USDC", "BTC", "ETH", "SOL"}:
                    symbols[symbol] = symbols.get(symbol, 0) + 1
        candidates: dict[str, dict] = {}
        errors: list[str] = []
        for symbol, mentions in sorted(symbols.items(), key=lambda x: (-x[1], x[0]))[:limit]:
            data, error = await self._json(DEX_SEARCH_URL + quote(symbol))
            if error:
                errors.append(f"{symbol}: {error}")
                continue
            for pair in list((data or {}).get("pairs") or [])[:8]:
                if not isinstance(pair, dict):
                    continue
                base = pair.get("baseToken") or {}
                if str(base.get("symbol") or "").upper() != symbol:
                    continue
                addr = base.get("address")
                chain = pair.get("chainId")
                if not addr or not chain:
                    continue
                key = f"{str(chain).lower()}:{str(addr).lower()}"
                old = candidates.get(key)
                row = {"chainId": chain, "tokenAddress": addr, "x_mentions": mentions, "x_source": True, "pair": pair}
                if old is None or safe_float(((pair.get("liquidity") or {}).get("usd"))) > safe_float((((old.get("pair") or {}).get("liquidity") or {}).get("usd"))):
                    candidates[key] = row
        return candidates, errors

    async def scan_crypto(self, store) -> dict:
        max_candidates = min(40, int(getattr(self.env, "MAX_BOOST_CANDIDATES", "25")))
        max_tokens = int(getattr(self.env, "MAX_POTENTIAL_TOKENS", "10"))
        x_posts, x_error = await self._x_posts()
        candidates, discovery_errors = await self._discover_from_x(x_posts, max_candidates)
        source_used = "X discovery + DexScreener validation"

        # X is primary. Dex endpoints are only fallback discovery, so a 429 no longer means zero candidates.
        boost_error = None
        if not candidates:
            boost_data, boost_error = await self._json(DEX_BOOSTS_TOP_URL)
            if boost_data is None:
                boost_data, fallback_error = await self._json(DEX_BOOSTS_LATEST_URL)
                boost_error = f"top={boost_error}; latest={fallback_error}"
            for row in list(boost_data or [])[:max_candidates]:
                if isinstance(row, dict) and row.get("chainId") and row.get("tokenAddress"):
                    candidates[f"{str(row['chainId']).lower()}:{str(row['tokenAddress']).lower()}"] = row
            source_used = "DexScreener fallback" if candidates else "X unavailable + DexScreener fallback"

        min_liquidity = float(getattr(self.env, "MIN_LIQUIDITY_USD", "5000"))
        min_volume = float(getattr(self.env, "MIN_VOLUME_24H_USD", "1000"))
        pair_rows: dict[str, dict] = {}
        enrichment_errors: list[str] = []

        # Reuse search result pairs from X discovery, then batch-fetch anything missing.
        grouped: dict[str, list[str]] = {}
        for key, candidate in candidates.items():
            if isinstance(candidate.get("pair"), dict):
                pair_rows[key] = candidate["pair"]
            else:
                chain, address = key.split(":", 1)
                grouped.setdefault(chain, []).append(address)
        for chain, addresses in grouped.items():
            for start in range(0, len(addresses), 30):
                chunk = addresses[start:start + 30]
                url = f"{DEX_TOKENS_URL}/{quote(chain, safe='')}/{','.join(quote(a, safe='') for a in chunk)}"
                data, error = await self._json(url)
                if error:
                    enrichment_errors.append(f"{chain}: {error}")
                    continue
                for pair in data if isinstance(data, list) else []:
                    if not isinstance(pair, dict):
                        continue
                    base = pair.get("baseToken") or {}
                    addr = base.get("address")
                    if addr:
                        key = f"{str(pair.get('chainId') or chain).lower()}:{str(addr).lower()}"
                        current = pair_rows.get(key)
                        if current is None or safe_float(((pair.get("liquidity") or {}).get("usd"))) > safe_float(((current.get("liquidity") or {}).get("usd"))):
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
            buys, sells = int(safe_float(h24.get("buys"))), int(safe_float(h24.get("sells")))
            tx_count = buys + sells
            momentum = max(-50.0, min(100.0, safe_float(changes.get("h24"))))
            buy_ratio = buys / max(1, tx_count)
            x_mentions = int(candidate.get("x_mentions") or 0)
            score = math.log10(max(liquidity, 1.0))*.42 + math.log10(max(volume_24h, 1.0))*.26 + math.log10(max(tx_count, 1.0))*.12 + math.log10(max(x_mentions+1, 1.0))*.16 + (buy_ratio-.5)*.04
            info = pair.get("info") or {}
            by_token[key] = {"rank_score": round(score,4), "name": base.get("name") or "Unknown", "symbol": base.get("symbol") or "UNKNOWN", "chain": pair.get("chainId"), "dex": pair.get("dexId"), "price_usd": safe_float(pair.get("priceUsd")), "change_1h_pct": safe_float(changes.get("h1")), "change_6h_pct": safe_float(changes.get("h6")), "change_24h_pct": momentum, "liquidity_usd": liquidity, "volume_24h_usd": volume_24h, "market_cap_usd": safe_float(pair.get("marketCap")), "txns_24h": {"buys":buys,"sells":sells,"total":tx_count}, "buy_ratio_24h": round(buy_ratio,3), "x_mentions": x_mentions, "websites": [x.get("url") for x in list(info.get("websites") or [])[:2] if isinstance(x,dict) and x.get("url")], "socials": [x for x in list(info.get("socials") or [])[:2] if isinstance(x,dict)], "pair_url": pair.get("url"), "token_address": base.get("address"), "source": "X + DexScreener"}

        potential_tokens = sorted(by_token.values(), key=lambda x: x["rank_score"], reverse=True)[:max_tokens]
        ai_text = ""
        ai_error = None
        if self._has_gemini() and potential_tokens:
            try:
                from gemini import GeminiRotator
                client = GeminiRotator(self.env, "RESEARCH")
                packet = {"task":"Rank current meme-coin candidates using compact X evidence and validated market data.","tokens":potential_tokens,"x_posts":x_posts[:20],"rules":["X is the primary current-information source; distinguish hype from corroborated facts.","Do not invent facts.","Prioritize liquidity, volume, transaction quality, momentum, repeated independent X discussion and scam/risk signals.","Return concise plain text, not JSON or tables.","Give a 1-10 ranking, bullish/bearish facts, key risks and monitoring verdict.","Keep under 900 words."]}
                result = await client.research(json.dumps(packet, separators=(",", ":")))
                ai_text = str(result.get("text") or "").strip()
                await store.record_research("crypto", "meme-coin X-first top-10", result)
            except Exception as exc:
                ai_error = f"{type(exc).__name__}: {exc}"

        lines=["🔥 INFOTRADER MEME-COIN INTELLIGENCE", f"Top {len(potential_tokens)} candidates ranked from X activity + on-chain validation.", ""]
        for i,t in enumerate(potential_tokens,1):
            lines.append(f"{i}. {t['name']} (${t['symbol']}) — {t['chain']} | X mentions: {t['x_mentions']} | Liquidity ${t['liquidity_usd']:,.0f} | 24h vol ${t['volume_24h_usd']:,.0f} | 24h {t['change_24h_pct']:+.2f}% | buys/sells {t['txns_24h']['buys']:,}/{t['txns_24h']['sells']:,}")
        if ai_text: lines += ["", "🧠 GEMINI X + ON-CHAIN READ", ai_text]
        notes = ([f"X: {x_error}"] if x_error else []) + discovery_errors[:3] + ([boost_error] if boost_error else []) + enrichment_errors[:3]
        if notes: lines += ["", "⚠️ DATA NOTES"] + [f"• {x}" for x in notes]
        return {"source":"X + DexScreener + Gemini","candidate_source":source_used,"candidate_count":len(potential_tokens),"potential_tokens":potential_tokens,"top_10":potential_tokens,"x_posts":x_posts[:20],"x_error":x_error,"dex_errors":discovery_errors + ([boost_error] if boost_error else []) + enrichment_errors[:10],"ai_report":ai_text,"ai_error":ai_error,"text_report":"\n".join(lines)[:3900]}

    @staticmethod
    def format_alert(scan: str, payload: dict) -> str:
        if scan != "crypto":
            return ScheduledDefault.format_alert(scan, payload)
        return payload.get("text_report") or "InfoTrader crypto scan: no current candidates found."