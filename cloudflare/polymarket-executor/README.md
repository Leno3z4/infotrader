# InfoTrader Polymarket executor

This Worker is intentionally separate from the Python InfoTrader Worker. Cloudflare Python Workers run on Pyodide/WASM, so the Polymarket CLOB SDK belongs in a normal JavaScript Worker instead of the Python dependency graph.

## Required Worker configuration

Set these on `infotrader-polymarket-executor`:

- `POLYMARKET_PRIVATE_KEY` — the EOA/session signer private key.
- `POLYMARKET_EXECUTOR_TOKEN` — long random token shared with the Python Worker.
- `POLYMARKET_WALLET_ADDRESS` — funder/profile wallet when applicable.
- `POLYMARKET_FUNDER_ADDRESS` — optional explicit funder override.
- `POLYMARKET_SIGNATURE_TYPE` — `0`, `1`, `2`, or `3` as appropriate for the account.
- `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE` — recommended for live use; required for signature type `3` because current CLOB API-key authentication for deposit-wallet accounts is not reliably creatable from the published TypeScript client.
- `POLYMARKET_MAX_ORDER_USD`, `POLYMARKET_MIN_EDGE`, `POLYMARKET_MIN_CONFIDENCE`, `POLYMARKET_MIN_ENTRY_PRICE`, `POLYMARKET_MAX_ENTRY_PRICE` — execution limits.

The Python Worker needs:

- `POLYMARKET_EXECUTOR_URL` — deployed executor URL.
- `POLYMARKET_EXECUTOR_TOKEN` — exactly the same token.
- `POLYMARKET_LIVE_TRADING=true` only after the executor has been tested.

## Deployment

The repository includes `.github/workflows/deploy-polymarket-executor.yml`. It expects `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` as GitHub Actions secrets.

Polymarket's CLOB V2 is the production trading API at `https://clob.polymarket.com`. The executor uses `@polymarket/clob-client-v2` 1.1.0 and `viem` rather than the incompatible Python client dependency.
