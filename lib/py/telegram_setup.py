"""One-shot Telegram setup: store bot token + auto-discover chat id in Keychain.

Run:  python lib/py/telegram_setup.py
Reads the bot token hidden, asks you to message the bot, finds the chat id via
getUpdates, and writes TELEGRAM_TOKEN + TELEGRAM_CHAT_ID to Keychain. The token
is never printed.
"""
from __future__ import annotations

import getpass
import subprocess
import sys

import requests

import secrets_store


def _store(name: str, value: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", secrets_store.KEYCHAIN_SERVICE, "-a", name, "-w", value],
        check=True,
    )


def main() -> int:
    token = getpass.getpass("Paste Telegram bot token (hidden): ").strip()
    if not token:
        print("No token entered.", file=sys.stderr)
        return 1

    input("Now open Telegram, send any message to your bot, then press Enter… ")

    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates", timeout=20
    )
    if resp.status_code != 200:
        print(f"getUpdates failed {resp.status_code} (token wrong?).", file=sys.stderr)
        return 1

    updates = resp.json().get("result", [])
    chat_ids = []
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            chat_ids.append(chat["id"])
    if not chat_ids:
        print("No messages found. Message the bot first, then re-run.", file=sys.stderr)
        return 1

    chat_id = str(chat_ids[-1])  # most recent
    _store("TELEGRAM_TOKEN", token)
    _store("TELEGRAM_CHAT_ID", chat_id)
    print(f"Stored TELEGRAM_TOKEN + TELEGRAM_CHAT_ID in Keychain. chat id = {chat_id}")

    # Confirm with a test message.
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": "✅ my-automations connected."},
        timeout=20,
    )
    print("Sent a test message — check Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
