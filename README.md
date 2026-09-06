# InfoTrader

Two hourly market-intelligence jobs:

- **Meme / mint / NFT monitor**: Robinhood-related crypto news, explicit ticker extraction, DEX liquidity/volume cross-checks, liquid trending pairs, and configured NFT collection stats.
- **Polymarket analyst**: open markets + recent news + optional current positions → Gemini proposal → Python risk gate → dry-run execution notification.

## What changed from the initial ZIP

The initial skeleton was a good start, but it was not accurate enough to call production-ready. This version:

- stops guessing that the first Polymarket outcome is the trade target;
- only permits an outcome Gemini actually named and that exists in the market;
- optionally loads the configured profile's current positions from Polymarket's public Data API;
- improves news/ticker extraction and DEX liquidity cross-checking;
- adds Telegram message chunking;
- adds five-key-capable Gemini failover with cooldowns;
- adds hard daily trade limits alongside size/confidence/loss limits;
- adds tests and a GitHub Actions hourly workflow;
- keeps live Polymarket execution disabled until the current wallet/signing adapter is separately verified.

## Gemini fallback

Configure `GEMINI_API_KEY_1` through `_5`. The rotator is intended for resilience across credentials/projects you are legitimately allowed to use. Gemini documents rate limits at the **project** level, so multiple keys inside one project do not multiply its quota.

## Setup

```bash
python -m venv .venv
pip install -r requirements.txt
cp .env.example .env
```

Run one pass:

```bash
python meme_monitor/bot.py --once
python polymarket_trader/bot.py --once
```

Run tests:

```bash
pytest -q
```

## Bot A configuration

```text
MIN_LIQUIDITY_USD=5000
NEWS_LIQUIDITY_USD=2500
WATCH_TOKENS=PEPE,DOGE
NFT_COLLECTIONS=collection-slug-a,collection-slug-b
RESERVOIR_API_KEY=
```

## Bot B configuration

```text
POLYMARKET_KEYWORD=
MAX_MARKETS_PER_RUN=5
POLYMARKET_PROFILE_ADDRESS=
MAX_POSITION_USD=5
MAX_DAILY_LOSS_USD=15
MAX_TRADES_PER_DAY=5
MIN_CONFIDENCE=0.65
DRY_RUN=true
```

Create a local kill switch with `touch polymarket_trader/STOP` and remove the file to resume.

## Polymarket live trading status

The code intentionally does **not** pretend the live adapter is ready. Polymarket's older `py-clob-client` is archived, and Polymarket now recommends the newer unified Python SDK. Current 2026 production issues have also been reported around the deposit-wallet / Poly1271 order-signing flow. The repository therefore separates analysis from execution and keeps the executor in dry-run mode until a tested adapter is chosen.

For a persistent live deployment, do not rely on an hourly GitHub Actions job as the source of truth for mutable risk state. Use a persistent worker or external state store.

## GitHub Actions

`.github/workflows/hourly.yml` runs at minute 7 of every hour and can also be started manually. Credentials go in Actions **Secrets**; non-sensitive tuning values go in Actions **Variables**.

The included workflow always sets `DRY_RUN=true` for the Polymarket job.
