"""InfoTrader Worker entrypoint with corrected cron-to-scan dispatch."""

from workers import env as worker_env

from entry import Default as BaseDefault
from storage import StateStore


POLYMARKET_CRON = "*/15 * * * *"
CRYPTO_CRON = "0 * * * *"


class Default(BaseDefault):
    async def scheduled(self, controller, env, ctx):
        """Run the scan that corresponds to the configured cron expression."""
        print("CRON HANDLER ENTERED")
        cron = getattr(controller, "cron", None)
        print(f"CRON CONTROLLER: {cron!r}")

        # Prefer the entrypoint's binding context; StateStore also has a
        # workers.env fallback for runtimes where the scheduled env object is
        # incomplete.
        binding_env = self.env if getattr(self, "env", None) is not None else env
        if getattr(binding_env, "INFOTRADER_STATE", None) is None:
            binding_env = worker_env
        store = StateStore(binding_env)
        print(f"CRON STORAGE AVAILABLE: {store.available}")

        scan = (
            "polymarket"
            if cron == POLYMARKET_CRON
            else "crypto"
            if cron == CRYPTO_CRON
            else None
        )
        print(f"CRON DISPATCH: scan={scan!r}")

        if not store.available:
            print("CRON STORAGE MISSING: INFOTRADER_STATE is not configured; scheduled scan skipped")
            return

        try:
            await store.record_cron(cron or "unknown", scan)
            print("CRON HEARTBEAT RECORDED")
        except Exception as exc:
            print(f"CRON HEARTBEAT ERROR: {type(exc).__name__}: {exc}")

        if not scan:
            print(f"CRON UNKNOWN: no scan mapped for expression={cron!r}")
            return

        try:
            result = await self.run_scan(scan, store)
            print(f"CRON COMPLETE: scan={scan} status={result.get('status')}")
        except Exception as exc:
            print(f"SCHEDULE ERROR: scan={scan}: {type(exc).__name__}: {exc}")
            try:
                from entry import send_telegram
                await send_telegram(
                    env,
                    f"InfoTrader scheduled scan error\nType: {type(exc).__name__}\nError: {exc}",
                )
            except Exception as alert_exc:
                print(f"SCHEDULE ALERT ERROR: {type(alert_exc).__name__}: {alert_exc}")
