import os
from typing import Iterable

import requests

TELEGRAM_MAX_CHARS = 4096


def _chunks(message: str, limit: int = TELEGRAM_MAX_CHARS) -> Iterable[str]:
    current = []
    length = 0
    for line in message.splitlines():
        extra = len(line) + (1 if current else 0)
        if current and length + extra > limit:
            yield "\n".join(current)
            current = [line]
            length = len(line)
        else:
            current.append(line)
            length += extra
    if current:
        yield "\n".join(current)


def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[notifier] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID; printing instead.")
        print(message)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in _chunks(message):
        try:
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            ok = False
            print(f"[notifier] Telegram send failed: {exc}")
    return ok
