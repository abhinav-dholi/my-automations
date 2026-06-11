"""Notification channel of record: Telegram.

Sends a message via the Telegram Bot API. All text is scrubbed first so a
secret can never ride out in a notification. Slack/Discord can be added later
as additional backends behind the same `send()` signature.
"""
from __future__ import annotations

import requests

import scrub
import secrets_store

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, *, parse_mode: str | None = None) -> None:
    """Send `text` to the configured Telegram chat.

    Requires secrets TELEGRAM_TOKEN and TELEGRAM_CHAT_ID. No-ops with a
    printed warning if they're absent, so a missing channel never crashes a
    run (the run's own status still reflects success/failure).
    """
    token = secrets_store.get("TELEGRAM_TOKEN", required=False)
    chat_id = secrets_store.get("TELEGRAM_CHAT_ID", required=False)
    body = scrub.clean(text)

    if not token or not chat_id:
        print("[notify] Telegram not configured; message:\n" + body)
        return

    payload = {"chat_id": chat_id, "text": body}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    resp = requests.post(_API.format(token=token), json=payload, timeout=20)
    if resp.status_code != 200:
        # Scrub the response too — it may echo the token/URL.
        raise RuntimeError(
            scrub.clean(f"Telegram send failed {resp.status_code}: {resp.text}")
        )
