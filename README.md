# InfoTrader

Two 24/7 services plus Telegram control, designed for one small Oracle Cloud Infrastructure (OCI) Compute VM.

## What it does

**Bot A — crypto intelligence**
- Robinhood-related crypto news
- meme/mint discovery
- DEX liquidity and volume cross-checks
- NFT collection monitoring
- Telegram alerts

**Bot B — Polymarket AI pipeline**
- Discovers active markets
- Gives **Premier League first-class priority** and keeps other markets eligible
- Uses Gemini Research Agent for live web research
- Uses Gemini Decision Agent for fair probability / edge / action
- Uses deterministic Python risk gates
- Uses a deterministic execution layer; live order placement remains disabled until a verified adapter is installed
- Persists research/signal history for later Telegram questions

Premier League priority uses team/league detection plus event proximity and market liquidity/volume. Fixtures are treated as mutable because official fixtures can change.

## Gemini roles

```text
GEMINI_RESEARCH_KEYS=GEMINI_API_KEY_1,GEMINI_API_KEY_4
GEMINI_DECISION_KEYS=GEMINI_API_KEY_2,GEMINI_API_KEY_5
GEMINI_CHAT_KEYS=GEMINI_API_KEY_3,GEMINI_API_KEY_1
```

The research agent uses Gemini web search grounding. Role pools fail over across configured credentials when quota/auth errors occur. Multiple keys in one Gemini project do not create independent quota pools; use credentials/projects you legitimately control.

## Telegram AI chat

You can now ask the bot about what has happened so far:

```text
/ask what has happened in the trading so far?
/ask what Premier League markets are we most interested in?
/ask why did we skip the last trade?
/ask what signals have appeared today?
```

The Telegram assistant reads the persistent trading state, recent signal log, research history, and system flags. It is an analysis/chat interface only; `/ask` cannot place or modify trades.

Other commands:

```text
/status
/pause
/resume
/kill
/signals
```

`/kill` creates a persistent stop marker in the shared state volume. `/resume` removes it. Live Polymarket execution still requires a separately verified executor and explicit configuration.

## Architecture

```text
                         GitHub
                            │
                            ▼
                      OCI deployment
                            │
                  ┌─────────┴─────────┐
                  │                   │
             Crypto Bot         Polymarket Bot
                  │                   │
              Telegram         ┌─────┴─────┐
                               ▼           ▼
                         Research      Market data
                         Gemini 1/4         │
                               │             │
                               └──────┬──────┘
                                      ▼
                                  Decision
                                  Gemini 2/5
                                      │
                                      ▼
                                  Risk Gate
                                      │
                                      ▼
                                  Executor #3
                                      │
                                      ▼
                                  Polymarket
```

## Crypto / Robinhood / NFT status

The original crypto monitor remains in the repository and is still used by the `meme-monitor` service. It fetches Robinhood-specific crypto news, extracts explicit token symbols, cross-checks those symbols against DexScreener liquidity/volume, scans liquid/trending pairs, and optionally queries Reservoir for configured NFT collections. Signals are persisted to the shared state volume and sent to Telegram.

## Cloudflare Workers scheduled scanner

The `cloudflare/` project is a Worker-native, **dry-run-only** scanner. It does not import the Docker services, use local files, or execute trades. It uses Cloudflare's asynchronous `fetch` API and has two UTC Cron Triggers:

| Schedule | Scanner | What it records |
|---|---|---|
| `*/15 * * * *` | Polymarket / Premier League | Active Premier League markets plus liquidity, volume, outcomes, and the selected priority list |
| `0 * * * *` | Crypto intelligence | Robinhood-related headlines, extracted tickers, liquid DexScreener boosted pairs, and the optional Reservoir NFT watchlist |

The Worker needs an `INFOTRADER_STATE` Cloudflare KV binding before scheduled scans are allowed to run. This intentionally fails closed: without persistence, the Worker will not send unrecorded alerts or risk duplicate scans. KV stores the most recent scan timestamp/payload, a bounded history of 30 signals, and persistent `PAUSED` / `STOP` flags.

Create a KV namespace named `infotrader-state` in the Cloudflare dashboard. Copy the `kv_namespaces` entry from [`cloudflare/wrangler.state.example.jsonc`](cloudflare/wrangler.state.example.jsonc) into `cloudflare/wrangler.jsonc`, replacing the placeholder with the namespace ID, then redeploy. Alternatively add the same binding in the Worker dashboard with binding name exactly `INFOTRADER_STATE`.

Set these Worker **secrets** in the Cloudflare dashboard (never commit them):

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
CONTROL_TOKEN
RESERVOIR_API_KEY              # only for NFT collection scans
```

Set these non-secret Worker variables if wanted:

```text
MIN_LIQUIDITY_USD=5000
NFT_COLLECTIONS=azuki,pudgypenguins
```

`/health` and `/status` are safe read-only endpoints. The following `POST` routes need an `Authorization: Bearer <CONTROL_TOKEN>` header: `/control/pause`, `/control/resume`, `/control/stop`, `/scan/polymarket`, and `/scan/crypto`. `STOP` and `PAUSED` both suppress scheduled scans; `/resume` clears both. These controls never enable order placement.

After a deployment, Cron Trigger changes can take several minutes to propagate. All schedules use UTC.

## OCI deployment

Create an Ubuntu Ampere A1 Flex VM in your OCI home region and use `ops/cloud-init.yaml`.

On the VM:

```bash
cd /home/ubuntu/infotrader
cp .env.example .env
nano .env
bash ops/deploy.sh
```

Keep `DRY_RUN=true` initially.

## GitHub → OCI auto-deploy

`.github/workflows/deploy-oci.yml` tests the repository and deploys over SSH after pushes to `main`.

Add GitHub Actions secrets:

```text
OCI_HOST
OCI_USER
OCI_SSH_PRIVATE_KEY
```

Optional:

```text
OCI_SSH_PORT
```

## Local tests

```bash
pip install -r requirements.txt
pytest -q
```

## Safety boundary

`DRY_RUN=true` is the safe default. The AI can research and propose, but it cannot bypass the Python risk engine. The live Polymarket signing/execution adapter remains intentionally disabled until the current wallet/order flow is independently verified.
