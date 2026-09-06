from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", ""))
STATE_DIR = Path(os.getenv("STATE_DIR", "/app/state"))
PAUSE_FILE = STATE_DIR / "PAUSED"
STOP_FILE = STATE_DIR / "STOP"
OFFSET_FILE = STATE_DIR / "telegram.offset"

COMMANDS = {
    "/status": "Status",
    "/pause": "Pause",
    "/resume": "Resume",
    "/kill": "Kill switch",
    "/signals": "Recent signals",
}


def api(method: str, payload: dict) -> dict:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    response = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data


def reply(text: str) -> None:
    api("sendMessage", {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True})


def set_command_menu() -> None:
    api(
        "setMyCommands",
        {"commands": [{"command": key[1:], "description": value} for key, value in COMMANDS.items()]},
    )


def recent_signals() -> str:
    path = STATE_DIR / "signals.log"
    if not path.exists():
        return "No signal log has been written yet."
    lines = path.read_text(encoding="utf-8").splitlines()[-10:]
    return "\n".join(lines) if lines else "No recent signals."


def handle(text: str) -> None:
    command = text.strip().split()[0].lower() if text.strip() else ""
    if command == "/status":
        reply(
            "InfoTrader status\n"
            f"paused={PAUSE_FILE.exists()}\n"
            f"kill_switch={STOP_FILE.exists()}\n"
            f"dry_run={os.getenv('DRY_RUN', 'true')}"
        )
    elif command == "/pause":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.touch()
        reply("Paused. Monitoring jobs may remain up, but trading remains blocked until /resume.")
    elif command == "/resume":
        PAUSE_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        reply("Resumed. Polymarket live execution still requires DRY_RUN=false and a verified executor.")
    elif command == "/kill":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STOP_FILE.touch()
        reply("KILL SWITCH ENABLED. Trading is blocked until /resume.")
    elif command == "/signals":
        reply(recent_signals())
    else:
        reply("Commands: /status /pause /resume /kill /signals")


def main() -> None:
    if not TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    set_command_menu()
    offset = int(OFFSET_FILE.read_text().strip()) if OFFSET_FILE.exists() else 0
    while True:
        try:
            data = api(
                "getUpdates",
                {"offset": offset, "timeout": 50, "allowed_updates": ["message"]},
            )
            for update in data.get("result", []):
                offset = int(update["update_id"]) + 1
                OFFSET_FILE.write_text(str(offset), encoding="utf-8")
                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                text = str(message.get("text") or "")
                if chat_id == CHAT_ID and text.startswith("/"):
                    handle(text)
        except Exception as exc:
            print(f"[telegram-control] {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
