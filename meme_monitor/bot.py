from __future__ import annotations

import argparse
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.notifier import send_telegram
from meme_monitor.scraper import extract_tickers, get_nft_collection_stats, get_robinhood_news, get_trending_pairs, search_pairs

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
NEWS_LIQUIDITY_USD = float(os.getenv("NEWS_LIQUIDITY_USD", "2500"))
NFT_COLLECTIONS = [s.strip() for s in os.getenv("NFT_COLLECTIONS", "").split(",") if s.strip()]
WATCH_TOKENS = [s.strip().upper() for s in os.getenv("WATCH_TOKENS", "").split(",") if s.strip()]


def _format_liquidity(hit: dict) -> str:
    return f"{hit.get('symbol') or '?'} on {hit.get('chain')}/{hit.get('dex')}: liq ${hit['liquidity_usd']:,.0f}, vol24h ${hit['volume_24h_usd']:,.0f}, 24h {hit['price_change_24h_pct']:+.2f}%"


def run_once() -> None:
    lines = ["<b>Hourly Market Intelligence</b>"]
    news = get_robinhood_news()
    lines.append("\n<b>Robinhood / token news</b>")
    if news:
        for item in news[:10]:
            title = item["title"]
            lines.append(f"• {title}")
            tickers = extract_tickers(title)
            if not tickers:
                lines.append("   ↳ No explicit ticker detected; liquidity cross-check skipped.")
                continue
            for ticker in tickers[:3]:
                for hit in search_pairs(ticker, min_liquidity_usd=NEWS_LIQUIDITY_USD, limit=3)[:2]:
                    lines.append(f"   ↳ <code>${ticker}</code> — {_format_liquidity(hit)}\n      {hit.get('url')}")
        if len(news) > 10:
            lines.append(f"…and {len(news) - 10} more headlines.")
    else:
        lines.append("• No matching headlines found.")

    pairs = get_trending_pairs(min_liquidity_usd=MIN_LIQUIDITY_USD)
    for symbol in WATCH_TOKENS:
        pairs.extend(search_pairs(symbol, min_liquidity_usd=NEWS_LIQUIDITY_USD, limit=3))
    deduped = {(p.get("chain"), p.get("pair_address")): p for p in pairs}
    ranked = sorted(deduped.values(), key=lambda row: row["liquidity_usd"] * max(1.0, row["volume_24h_usd"]), reverse=True)
    lines.append(f"\n<b>Liquid meme / trending pairs ≥ ${MIN_LIQUIDITY_USD:,.0f}</b>")
    if ranked:
        for pair in ranked[:10]:
            lines.append(f"• {_format_liquidity(pair)}")
    else:
        lines.append("• None found.")

    if NFT_COLLECTIONS:
        lines.append("\n<b>NFT watchlist</b>")
        for slug in NFT_COLLECTIONS:
            stats = get_nft_collection_stats(slug)
            if not stats:
                lines.append(f"• {slug}: unavailable")
                continue
            floor = stats.get("floor_price_usd") or 0
            volume = stats.get("volume_24h_usd") or 0
            lines.append(f"• {stats.get('name') or slug}: floor ${floor:,.2f}, 24h vol ${volume:,.2f}")

    message = "\n".join(lines)
    print(message)
    send_telegram(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        run_once()
        return
    while True:
        run_once()
        time.sleep(3600)


if __name__ == "__main__":
    main()
