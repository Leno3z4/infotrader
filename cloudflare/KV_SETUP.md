# Cloudflare KV setup for InfoTrader

The Worker expects one KV namespace bound as `INFOTRADER_STATE`.

## Dashboard method

1. Cloudflare Dashboard → Workers & Pages → KV → Create namespace.
2. Name it `infotrader-state`.
3. Open Workers & Pages → `infotrader` → Settings → Bindings.
4. Add binding → KV Namespace.
5. Set the variable/binding name to exactly `INFOTRADER_STATE`.
6. Select `infotrader-state` and deploy.

The Worker then persists:

- `scan:polymarket:last`
- `scan:crypto:last`
- `recent:signals`
- `recent:research`
- `recent:decisions`
- `flag:PAUSED`
- `flag:STOP`

## Wrangler method

Create the namespace:

```bash
npx wrangler kv namespace create INFOTRADER_STATE
```

Wrangler prints a namespace ID. Add the resulting `kv_namespaces` block to `cloudflare/wrangler.jsonc` using the structure in `wrangler.state.example.jsonc`, then deploy with:

```bash
uv run pywrangler deploy
```

Do not commit API tokens or other secrets. The namespace ID is not a credential, but keeping the dashboard binding as the source of truth is simplest for this Git-integrated Worker.

## Variables / secrets

Required secrets for the full pipeline:

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

Useful non-secret variables:

```text
GEMINI_RESEARCH_KEYS=GEMINI_API_KEY_1,GEMINI_API_KEY_4
GEMINI_DECISION_KEYS=GEMINI_API_KEY_2,GEMINI_API_KEY_5
GEMINI_CHAT_KEYS=GEMINI_API_KEY_3,GEMINI_API_KEY_1
GEMINI_MODEL=gemini-3.8-flash
MAX_MARKETS_PER_RUN=8
MIN_LIQUIDITY_USD=5000
NFT_COLLECTIONS=
DRY_RUN=true
```

Live trading is intentionally disabled. KV only provides application state; it does not grant permission to place orders.
