"""Cloudflare production Worker entrypoint with a concrete scheduled handler.

The production trading implementation is inherited from entry_trading.Default.
The scheduled handler is defined directly on this final WorkerEntrypoint class so
Cloudflare's Python Workers runtime can discover it without relying on inherited
handler dispatch.
"""

from entry import send_telegram
from entry_scheduled import POLYMARKET_CRON, _is_top_of_utc_hour
from entry_trading import Default as TradingDefault
from storage import StateStore


class Default(TradingDefault):
    async def scheduled(self, controller, env, ctx):
        """Run Polymarket every 15 minutes and crypto at the top of each UTC hour."""
        print("CRON HANDLER ENTERED")
        cron = getattr(controller, "cron", None)
        scheduled_time = getattr(controller, "scheduledTime", None)
        print(f"CRON CONTROLLER: {cron!r} scheduledTime={scheduled_time!r}")

        if cron != POLYMARKET_CRON:
            print(f"CRON UNKNOWN: expected={POLYMARKET_CRON!r} got={cron!r}")
            return

        store = StateStore(env)
        print(f"CRON STORAGE AVAILABLE: {store.available}")
        if not store.available:
            print("CRON STORAGE MISSING: INFOTRADER_STATE is not configured; scheduled scan skipped")
            return

        run_crypto = _is_top_of_utc_hour(scheduled_time)
        print(f"CRON DISPATCH: scan='polymarket' run_crypto={run_crypto}")

        try:
            await store.record_cron(cron, "polymarket")
            print("CRON HEARTBEAT RECORDED")
        except Exception as exc:
            print(f"CRON HEARTBEAT ERROR: {type(exc).__name__}: {exc}")

        try:
            result = await self.run_scan("polymarket", store)
            print(f"CRON COMPLETE: scan=polymarket status={result.get('status')}")
        except Exception as exc:
            print(f"SCHEDULE ERROR: scan=polymarket: {type(exc).__name__}: {exc}")
            try:
                await send_telegram(
                    env,
                    f"InfoTrader scheduled scan error\nType: {type(exc).__name__}\nError: {exc}",
                )
            except Exception as alert_exc:
                print(f"SCHEDULE ALERT ERROR: {type(alert_exc).__name__}: {alert_exc}")

        if run_crypto:
            try:
                result = await self.run_scan("crypto", store)
                print(f"CRON COMPLETE: scan=crypto status={result.get('status')}")
            except Exception as exc:
                print(f"SCHEDULE ERROR: scan=crypto: {type(exc).__name__}: {exc}")
                try:
                    await send_telegram(
                        env,
                        f"InfoTrader scheduled scan error\nType: {type(exc).__name__}\nError: {exc}",
                    )
                except Exception as alert_exc:
                    print(f"SCHEDULE ALERT ERROR: {type(alert_exc).__name__}: {alert_exc}")
