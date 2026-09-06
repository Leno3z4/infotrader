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
- Telegram reports the research, decision and execution state

Premier League priority uses team/league detection plus event proximity and market liquidity/volume. Fixtures are treated as mutable because the official Premier League schedule is subject to broadcast and UEFA-related changes; the league has published further 2026/27 amendments in September 2026. citeturn726160search2turn726160search0turn726160search10

## Gemini roles

Configure your five credentials as two research fallbacks, two decision fallbacks and one spare execution credential:

```text
GEMINI_RESEARCH_KEYS=GEMINI_API_KEY_1,GEMINI_API_KEY_4
GEMINI_DECISION_KEYS=GEMINI_API_KEY_2,GEMINI_API_KEY_5
```

The code falls back across the configured credentials when a credential returns quota/auth errors. **Five keys in one Gemini project do not create five independent quota pools**; use legitimately separate project/credential allocations where applicable.

The research agent uses Gemini's current Google Search grounding tool, which lets the model retrieve current public web information and ground answers in search results. citeturn416786search4turn416786search5

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

## OCI deployment

Create an Ubuntu Ampere A1 Flex VM in your OCI home region and use `ops/cloud-init.yaml`. Oracle documents the current Always Free A1 compute allocation and OCI Container Instances as alternatives. citeturn726160search11

On the VM:

```bash
cd /home/ubuntu/infotrader
cp .env.example .env
nano .env
bash ops/deploy.sh
```

Keep `DRY_RUN=true` initially.

## Telegram

The control service uses long polling. Commands:

```text
/status
/pause
/resume
/kill
/signals
```

`/kill` creates a persistent stop marker in the shared state volume. `/resume` removes it.

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
