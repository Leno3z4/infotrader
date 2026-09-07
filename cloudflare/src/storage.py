"""Small persistence abstraction over the optional Cloudflare KV binding."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class StateStore:
    """Persist control flags and bounded scanner history in ``INFOTRADER_STATE``.

    The binding is optional at deploy time so the health Worker stays deployable
    before the user creates the KV namespace. Scheduled scans fail closed until
    it is attached, avoiding unrecorded duplicate alert runs.
    """

    def __init__(self, env: Any):
        self._kv = getattr(env, "INFOTRADER_STATE", None)

    @property
    def available(self) -> bool:
        return self._kv is not None

    async def get_json(self, key: str, default: Any = None) -> Any:
        if not self.available:
            return default
        raw = await self._kv.get(key)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return default

    async def put_json(self, key: str, value: Any) -> None:
        if not self.available:
            raise RuntimeError("INFOTRADER_STATE KV binding is not configured")
        await self._kv.put(key, json.dumps(value, separators=(",", ":"), default=str))

    async def flag(self, name: str) -> bool:
        if not self.available:
            return False
        return (await self._kv.get(f"flag:{name}")) == "true"

    async def set_flag(self, name: str, enabled: bool) -> None:
        if not self.available:
            raise RuntimeError("INFOTRADER_STATE KV binding is not configured")
        key = f"flag:{name}"
        if enabled:
            await self._kv.put(key, "true")
        else:
            await self._kv.delete(key)

    async def record_scan(self, scan: str, status: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "scan": scan,
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        await self.put_json(f"scan:{scan}:last", event)
        recent = await self.get_json("recent:signals", [])
        recent = [event, *recent][:30]
        await self.put_json("recent:signals", recent)
        return event

    async def status(self) -> dict[str, Any]:
        return {
            "storage_bound": self.available,
            "paused": await self.flag("PAUSED"),
            "stopped": await self.flag("STOP"),
            "polymarket": await self.get_json("scan:polymarket:last"),
            "crypto": await self.get_json("scan:crypto:last"),
        }
