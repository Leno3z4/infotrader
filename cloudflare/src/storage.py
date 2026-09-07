"""Persistence abstraction over the Cloudflare KV binding."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class StateStore:
    """Persist control flags, scan state, research and decision history.

    The binding is optional for deployment, but scheduled scans fail closed until
    it is attached so the Worker never produces untracked runs.
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

    async def put_json(self, key: str, value: Any, *, expiration_ttl: int | None = None) -> None:
        if not self.available:
            raise RuntimeError("INFOTRADER_STATE KV binding is not configured")
        options = {"expiration_ttl": expiration_ttl} if expiration_ttl else None
        encoded = json.dumps(value, separators=(",", ":"), default=str)
        if options:
            await self._kv.put(key, encoded, options)
        else:
            await self._kv.put(key, encoded)

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

    async def _append(self, key: str, event: dict[str, Any], limit: int = 30) -> None:
        recent = await self.get_json(key, [])
        recent = [event, *recent][:limit]
        await self.put_json(key, recent)

    async def record_scan(self, scan: str, status: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "scan": scan,
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        await self.put_json(f"scan:{scan}:last", event)
        await self.put_json("recent:signals", [event, *(await self.get_json("recent:signals", []))][:30])
        return event

    async def record_research(self, scan: str, subject: str, result: dict[str, Any]) -> dict[str, Any]:
        event = {
            "scan": scan,
            "subject": subject,
            "at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        await self._append("recent:research", event, 30)
        return event

    async def record_decision(self, scan: str, subject: str, result: dict[str, Any]) -> dict[str, Any]:
        event = {
            "scan": scan,
            "subject": subject,
            "at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        await self._append("recent:decisions", event, 30)
        return event

    async def recent(self, key: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.get_json(key, [])
        return rows[:limit] if isinstance(rows, list) else []

    async def status(self) -> dict[str, Any]:
        return {
            "storage_bound": self.available,
            "paused": await self.flag("PAUSED"),
            "stopped": await self.flag("STOP"),
            "polymarket": await self.get_json("scan:polymarket:last"),
            "crypto": await self.get_json("scan:crypto:last"),
            "recent_research": await self.recent("recent:research", 5),
            "recent_decisions": await self.recent("recent:decisions", 5),
        }
