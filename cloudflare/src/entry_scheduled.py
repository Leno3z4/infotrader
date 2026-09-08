"""InfoTrader Worker scheduled dispatcher.

One Cloudflare Cron Trigger fires every 15 minutes. Polymarket runs on every
fire; the crypto caller runs on the top of each UTC hour. Using one trigger
removes ambiguity around multiple cron expressions while preserving both
cadences.
"""

from datetime import datetime, timezone

from workers import env as worker_env

from entry import Default as BaseDefault
from storage import StateStore

POLYMARKET_CRON = "*/15 * * * *"


class Default(BaseDefault):
    async def scheduled(self, controller, env, ctx):
        print("CRON HANDLER ENTERED")
        cron = str(getattr(controller, "cron", "") or "")
        scheduled_ms = getattr(controller, "scheduledTime", None)
        print(f"CRON CONTROLLER: {cron!r} scheduledTime={scheduled_ms!r}")

        binding_env = self.env if getattr(self, "env", None) is not None else env
        if getattr(binding_env, "INFOTRADER_STATE", None) is None:
            binding_env = worker_env
        store = StateStore(binding_env)
        print(f"CRON STORAGE AVAILABLE: {store.available}")

        if cron != POLYMARKET_CRON:
            print(f"CRON UNKNOWN: expected={POLYMARKET_CRON!r} got={cron!r}")
            return

        scan = "polymarket"
        run_crypto = False
        if scheduled_ms is not None:
            try:
                dt = datetime.fromtimestamp(float(scheduled_ms) / 1000.0, tz=timezone.utc)
                run_crypto = dt.minute == 0
            except (TypeError, ValueError, OverflowError):
                run_crypto = False
        print(f"CRON DISPATCH: scan={scan!r} run_crypto={run_crypto}")

        if not store.available:
            print("CRON STORAGE MISSING: INFOTRADER_STATE is not configured; scheduled scan skipped")
            return

        try:
            await store.record_cron(cron, scan)
            print("CRON HEARTBEAT RECORDED")
        except Exception as exc:
            print(f"CRON HEARTBEAT ERROR: {type(exc).__name__}: {exc}")

        try:
            result = await self.run_scan(scan, store)
            print(f"CRON COMPLETE: scan={scan} status={result.get('status')}")
        except Exception as exc:
            print(f"SCHEDULE ERROR: scan={scan}: {type(exc).__name__}: {exc}")
            try:
                from entry import send_telegram
                await send_telegram(env, f"InfoTrader scheduled scan error\nType: {type(exc).__name__}\nError: {exc}")
            except Exception as alert_exc:
                print(f"SCHEDULE ALERT ERROR: {type(alert_exc).__name__}: {alert_exc}")

        if run_crypto:
            try:
                result = await self.run_scan("crypto", store)
                print(f"CRON COMPLETE: scan=crypto status={result.get('status')}")
            except Exception as exc:
                print(f"SCHEDULE ERROR: scan=crypto: {type(exc).__name__}: {exc}")
                try:
                    from entry import send_telegram
                    await send_telegram(env, f"InfoTrader scheduled scan error\nType: {type(exc).__name__}\nError: {exc}")
                except Exception as alert_exc:
                    print(f"SCHEDULE ALERT ERROR: {type(alert_exc).__name__}: {alert_exc}")
