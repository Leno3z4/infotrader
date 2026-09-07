# InfoTrader

Two production paths live in this repository: the original OCI/Docker services and a Cloudflare Worker-native scheduled intelligence service. Both are dry-run only for trading.

## What it does

**Crypto intelligence**
- Robinhood-related crypto news
- meme/mint discovery
- DEX liquidity and volume cross-checks
- NFT collection monitoring through OpenSea
- Telegram alerts

**Polymarket AI pipeline**
- Discovers active markets
- Gives **Premier League first-class priority** while keeping other markets eligible
- Gemini research with Google Search grounding
- Gemini decision analysis for fair probability / edge / action
- Deterministic risk gates remain outside the model
- Live Polymarket order placement remains disabled
- Persists scans, research and decisions in Cloudflare KV when using the Worker path

## Gemini roles

```text
GEMINI_RESEARCH_KEYS=GEMINI_API_KEY_1,GEMINI_API_KEY_4
GEMINI_DECISION_KEYS=GEMINI_API_KEY_2,GEMINI_API_KEY_5
GEMINI_CHAT_KEYS=GEMINI_API_KEY_3,GEMINI_API_KEY_1
```

The Cloudflare Worker uses the Gemini REST API directly. Research calls enable Gemini Google Search grounding; decision calls evaluate the returned market/research packet. Role pools fail over across configured credentials when requests are rejected. Multiple keys in one Gemini project do not create independent quota pools; use credentials/projects you legitimately control.

## Telegram AI chat

The existing OCI Telegram service supports `/ask`, `/status`, `/pause`, `/resume`, `/kill`, and `/signals`. The Cloudflare Worker also supports authenticated HTTP control routes and sends scan alerts with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Cloudflare Worker

The `cloudflare/` project is a Worker-native, **dry-run-only** scanner. It uses Workers async `fetch`, does not import the OCI Docker services, and does not execute trades.

| Schedule (UTC) | Scanner | Main work |
|---|---|---|
| `*/15 * * * *` | Polymarket | Rank active markets with Premier League priority, research the top candidates, and ask Gemini for a decision packet |
| `0 * * * *` | Crypto | Gather Robinhood news, DexScreener liquidity, optional OpenSea collection stats, and Gemini research |

### Cloudflare configuration

Create a Workers KV namespace called `infotrader-state` and bind it to the Worker as **`INFOTRADER_STATE`**. Cloudflare supports KV bindings either in Wrangler or through Workers & Pages → Settings → Bindings. See `cloudflare/wrangler.state.example.jsonc` for the expected binding shape.

Set these Worker **secrets** in the Cloudflare dashboard:

```text
GEMINI_API_KEY_1
GEMINI_API_KEY_2
GEMINI_API_KEY_3
GEMINI_API_KEY_4
GEMINI_API_KEY_5
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
CONTROL_TOKEN
OPENSEA_API_KEY
```

Set these Worker **variables** when needed:

```text
GEMINI_RESEARCH_KEYS=GEMINI_API_KEY_1,GEMINI_API_KEY_4
GEMINI_DECISION_KEYS=GEMINI_API_KEY_2,GEMINI_API_KEY_5
GEMINI_CHAT_KEYS=GEMINI_API_KEY_3,GEMINI_API_KEY_1
GEMINI_MODEL=gemini-3.8-flash
MAX_MARKETS_PER_RUN=8
MIN_LIQUIDITY_USD=5000
NFT_COLLECTIONS=azuki,pudgypenguins
DRY_RUN=true
```

The Worker stores the latest scan for each vertical, recent scan history, recent Gemini research, recent decisions, and persistent `PAUSED` / `STOP` flags in KV. Scheduled runs fail closed when the KV binding is missing.

`/health` and `/status` are read-only. These `POST` routes require `Authorization: Bearer <CONTROL_TOKEN>`: `/control/pause`, `/control/resume`, `/control/stop`, `/scan/polymarket`, `/scan/crypto`.

### OpenSea

NFT monitoring now uses OpenSea REST API v2. The Worker calls the collection stats endpoint with the `X-API-KEY` header, and the OCI crypto monitor uses the same read-only API for configured collection slugs. No NFT buying/selling functionality is enabled. OpenSea documents collection stats for floor price, volume, sales and related metrics.

## OCI deployment

The existing OCI/Docker deployment remains available:

```bash
cd /home/ubuntu/infotrader
cp .env.example .env
nano .env
bash ops/deploy.sh
```

Keep `DRY_RUN=true` initially.

## Local tests

```bash
pip install -r requirements.txt
pytest -q
```

## Safety boundary

`DRY_RUN=true` is the safe default. Gemini can research and propose, but it cannot bypass the deterministic risk layer. The live Polymarket signing/execution adapter remains intentionally disabled until the wallet and order flow are independently verified.
