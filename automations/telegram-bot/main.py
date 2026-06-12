"""telegram-bot — long-poll Telegram, run finance automations on demand.

Security: only messages from the configured TELEGRAM_CHAT_ID are honored;
everything else is ignored (a bot can be messaged by anyone). Runs as an
always-on service (launchd KeepAlive). Triggered automations send their own
output to Telegram; this bot replies with start/finish status.
"""
from __future__ import annotations

import subprocess
import sys
import time

import requests

import config
import secrets_store

# command -> automation id(s) to run in order
COMMANDS = {
    "/analyze": ["finance-analyze"],
    "/sync": ["finance-sync", "splitwise-sync"],
    "/weekly": ["finance-weekly"],
}
HELP = (
    "Commands:\n"
    "/analyze — run the finance analysis (sends a report here)\n"
    "/finance <question> — ask anything about your finances in plain English\n"
    "/sync — pull latest bank + Splitwise data\n"
    "/weekly — weekly spend/balance digest\n"
    "/status — last run of each automation\n"
    "/help — this message"
)

# Commands that take a free-text question after the token.
ASK_COMMANDS = {"/finance", "/ask"}


def parse_command(text: str) -> str | None:
    """Return the normalized command token (e.g. '/analyze') or None."""
    if not text:
        return None
    tok = text.strip().split()[0].lower()
    tok = tok.split("@")[0]  # strip /cmd@botname
    known = set(COMMANDS) | ASK_COMMANDS | {"/status", "/help", "/start"}
    return tok if tok in known else None


def _question_after(text: str) -> str:
    """Strip the leading command token, return the rest (the question)."""
    parts = text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _api(token: str, method: str, **params):
    return requests.get(
        f"https://api.telegram.org/bot{token}/{method}", params=params, timeout=70
    ).json()


def _send(token: str, chat_id: str, text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=20,
    )


def _status_text() -> str:
    sys.path.insert(0, str(config.LIB_PY))
    import runlog
    latest = {}
    for rec in runlog.read_recent(500):
        latest[rec["id"]] = rec
    if not latest:
        return "No runs logged yet."
    return "\n".join(
        f"{i}: {r.get('status','?')} @ {r.get('end','?')}"
        for i, r in sorted(latest.items())
    )


def _run(automation_id: str) -> tuple[bool, str]:
    res = subprocess.run(
        [sys.executable, "cli/auto", "run", automation_id],
        cwd=str(config.REPO_ROOT), capture_output=True, text=True,
    )
    ok = res.returncode == 0
    tail = (res.stdout or res.stderr or "").strip().splitlines()
    return ok, (tail[-1] if tail else f"exit {res.returncode}")


def handle(token: str, chat_id: str, cmd: str, text: str = "") -> None:
    if cmd in ("/help", "/start"):
        _send(token, chat_id, HELP)
        return
    if cmd == "/status":
        _send(token, chat_id, _status_text())
        return
    if cmd in ASK_COMMANDS:
        question = _question_after(text)
        if not question:
            _send(token, chat_id, "Ask a question, e.g. /finance how much did I spend on dining?")
            return
        _send(token, chat_id, "💭 thinking…")
        sys.path.insert(0, str(config.LIB_PY))
        import finance_ask
        _send(token, chat_id, finance_ask.answer(question))
        return
    for aid in COMMANDS[cmd]:
        _send(token, chat_id, f"▶ running {aid}…")
        ok, msg = _run(aid)
        _send(token, chat_id, f"{'✅' if ok else '❌'} {aid}: {msg}")


def main() -> None:
    token = secrets_store.get("TELEGRAM_TOKEN")
    chat_id = str(secrets_store.get("TELEGRAM_CHAT_ID"))

    # Skip backlog: start from the latest update id.
    offset = None
    init = _api(token, "getUpdates", timeout=0)
    if init.get("ok") and init.get("result"):
        offset = init["result"][-1]["update_id"] + 1

    _send(token, chat_id, "🤖 my-automations bot online. /help for commands.")
    print("[telegram-bot] online, polling…")

    while True:
        try:
            resp = _api(token, "getUpdates", offset=offset, timeout=50)
        except requests.RequestException as e:
            print(f"[telegram-bot] poll error: {e}"); time.sleep(5); continue
        if not resp.get("ok"):
            time.sleep(5); continue

        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            from_chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text", "")
            print(f"[telegram-bot] update {upd['update_id']} chat={from_chat} text={text!r}", flush=True)
            if from_chat != chat_id:        # security: ignore everyone else
                print(f"[telegram-bot] ignored (chat {from_chat} != {chat_id})", flush=True)
                continue
            cmd = parse_command(text)
            if cmd:
                print(f"[telegram-bot] handling {cmd}", flush=True)
                try:
                    handle(token, chat_id, cmd, text)
                except Exception as e:
                    print(f"[telegram-bot] handler error: {e}", flush=True)
                    _send(token, chat_id, f"⚠️ error handling {cmd}: {e}")
            else:
                _send(token, chat_id, "Unknown command. /help")


if __name__ == "__main__":
    main()
