# InfoTrader

Two 24/7 market-intelligence services plus a Telegram control service, designed to run on one small OCI Compute VM.

- **Meme / mint / NFT monitor**: Robinhood-related crypto news, explicit ticker extraction, DEX liquidity/volume cross-checks, liquid trending pairs, and configured NFT collection stats.
- **Polymarket analyst**: open markets + recent news + optional current positions → Gemini proposal → Python risk gate → dry-run execution notification.
- **Telegram control**: `/status`, `/pause`, `/resume`, `/kill`, `/signals`.

## Architecture

```text
GitHub main
   │  push
   ▼
GitHub Actions ──SSH──> OCI Compute VM
                          │
                          ├── meme-monitor
                          ├── polymarket-trader
                          └── telegram-control
                                 │
                                 ▼
                              Telegram
```

Oracle documents Always Free Ampere A1 compute in the home region; an Always Free tenancy gets the equivalent of 2 OCPUs and 12 GB RAM total across A1 instances. OCI Container Instances and OCI Container Registry are also supported alternatives. See Oracle's current Always Free and Container Instance documentation before provisioning. citeturn552222search0turn552222search8

## 1. Create the OCI VM

Use an Ubuntu A1 Flex VM in your **home region**. Keep the total Always Free A1 allocation within Oracle's current published limits. Oracle's Ubuntu login user is `ubuntu`. citeturn552222search0turn552222search4

When creating the instance, paste the contents of `ops/cloud-init.yaml` into cloud-init. This installs Docker, clones the public repository, and builds the image.

## 2. Configure secrets on the VM

SSH into the VM and edit:

```bash
nano /home/ubuntu/infotrader/.env
```

At minimum:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
GEMINI_API_KEY_1=...
```

Add `_2` through `_5` only for separate credentials/projects you legitimately control. Keys in one Gemini project do not multiply that project's quota.

For the initial deployment keep:

```text
DRY_RUN=true
```

## 3. Start the services

```bash
cd /home/ubuntu/infotrader
bash ops/deploy.sh
```

Check:

```bash
docker compose ps
docker compose logs -f
```

The Compose services use `restart: unless-stopped`, and the risk/command state is stored in the named Docker volume `bot-state`, so ordinary container recreation does not erase it.

## 4. Telegram

The control service uses Telegram long polling, so the VM does not need a public HTTPS endpoint just to receive commands. Telegram also supports HTTPS webhooks if you later decide to put an ingress layer in front of it. citeturn552222search2

Commands:

```text
/status
/pause
/resume
/kill
/signals
```

`/kill` creates a persistent stop marker in the shared state volume. `/resume` removes it. Live Polymarket execution is still separately disabled by `DRY_RUN=true`.

## 5. GitHub → OCI auto-deploy

The workflow `.github/workflows/deploy-oci.yml` runs tests on every push to `main`, then SSHes to OCI and runs the deployment commands.

Add these GitHub Actions secrets:

```text
OCI_HOST
OCI_USER
OCI_SSH_PRIVATE_KEY
```

`OCI_SSH_PORT` is optional and defaults to 22.

The SSH user must have Docker access. The standard Ubuntu account on OCI is `ubuntu`. citeturn552222search4

## 6. Local development

```bash
python -m venv .venv
pip install -r requirements.txt
pytest -q
python meme_monitor/bot.py --once
python polymarket_trader/bot.py --once
```

## Polymarket live trading status

The repository deliberately separates analysis/risk from execution. `DRY_RUN=true` is the safe default. Do not switch it off until the current Polymarket signing/wallet adapter has been independently verified, position reconciliation is in place, and the system has been observed in dry-run mode.

For a persistent live trading service, OCI is a better host than an hourly GitHub Actions job because mutable state remains on the VM and the containers can restart automatically.
