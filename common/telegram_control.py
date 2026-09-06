from __future__ import annotations

import os
import time
from pathlib import Path

from common.gemini_rotator import GeminiRotator
from common.notifier import TELEGRAM_MAX_CHARS

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
    "/ask": "Ask Gemini about current activity",
}


def api(method: str, payload: dict) -> dict:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    response = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data


def reply(text: str) -> None:
    # Telegram accepts up to 4096 characters per message.
    for start in range(0, len(text), TELEGRAM_MAX_CHARS):
        api("sendMessage", {"chat_id": CHAT_ID, "text": text[start:start + TELEGRAM_MAX_CHARS], "disable_web_page_preview": True})


def set_command_menu() -> None:
    api("setMyCommands", {"commands": [{"command": key[1:], "description": value} for key, value in COMMANDS.items()]})


def recent_signals() -> str:
    path = STATE_DIR / "signals.log"
    if not path.exists():
        return "No signal log has been written yet."
    lines = path.read_text(encoding="utf-8").splitlines()[-20:]
    return "\n".join(lines) if lines else "No recent signals."


def trading_context() -> str:
    parts = []
    state_file = Path(os.getenv("TRADING_STATE_FILE", STATE_DIR / "daily_state.json"))
    if state_file.exists():
        parts.append("TRADING STATE:\n" + state_file.read_text(encoding="utf-8")[-4000:])
    parts.append("RECENT SIGNAL LOG:\n" + recent_signals())
    parts.append(
        "SYSTEM FLAGS:\n"
        f"paused={PAUSE_FILE.exists()}\n"
        f"kill_switch={STOP_FILE.exists()}\n"
        f"dry_run={os.getenv('DRY_RUN', 'true')}"
    )
    return "\n\n".join(parts)


def ask_gemini(question: str) -> str:
    prompt = f"""You are the private InfoTrader assistant. Answer the user's question using the supplied live bot state and recent signal history. Do not invent trades, positions, prices, P&L, or facts that are not in the context. Be explicit when the context does not contain enough information. You are explaining system activity, not placing orders.\n\nCURRENT INFO TRADER CONTEXT:\n{trading_context()}\n\nUSER QUESTION:\n{question}\n"""
    rotator = GeminiRotator(role="chat")
    try:
        return rotator.generate(prompt)
    except Exception as exc:
        return f"Gemini chat unavailable: {type(exc).__name__}: {exc}"


def handle(text: str) -> None:
    stripped = text.strip()
    command = stripped.split()[0].lower() if stripped else ""
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
        reply("Paused. Monitoring jobs may remain up, but trading is blocked until /resume.")
    elif command == "/resume":
        PAUSE_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        reply("Resumed. Live Polymarket execution still requires a verified executor and DRY_RUN=false.")
    elif command == "/kill":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STOP_FILE.touch()
        reply("KILL SWITCH ENABLED. Trading is blocked until /resume.")
    elif command == "/signals":
        reply(recent_signals())
    elif command == "/ask":
        question = stripped[len("/ask"):].strip()
        if not question:
            reply("Usage: /ask what has happened in the trading so far?")
            return
        reply(ask_gemini(question))
    else:
        reply("Commands: /status /pause /resume /kill /signals /ask <question>")


def main() -> None:
    if not TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    set_command_menu()
    offset = int(OFFSET_FILE.read_text().strip()) if OFFSET_FILE.exists() else 0
    while True:
        try:
            data = api("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message"]})
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
