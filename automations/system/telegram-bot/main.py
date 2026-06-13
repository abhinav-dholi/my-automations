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

# Commands are namespaced by domain so the bot scales beyond finance.
# Global commands work across all domains; /finance is the finance namespace.
GLOBAL = {"/help", "/start", "/status", "/list", "/run", "/finance"}

# Finance subcommand -> automation id(s) to run in order.
FINANCE_ACTIONS = {
    "summary": ["finance-summary"],
    "sync": ["finance-sync", "splitwise-sync", "finance-categorize"],
    "analyze": ["finance-analyze"],
    "council": ["finance-council"],
    "categorize": ["finance-categorize"],
    "weekly": ["finance-weekly"],
    "brief": ["market-watch"],
}
# Friendly progress labels + which actions deliver their own rich message.
ACTION_LABELS = {
    "summary": "📊 Building your snapshot", "sync": "🔄 Syncing accounts + Splitwise",
    "analyze": "🧠 Running full analysis (~2 min)", "council": "🏛 Convening the council (~2–3 min)",
    "categorize": "🏷 AI-categorizing transactions", "weekly": "📅 Building weekly digest",
    "brief": "🌐 Refreshing market brief",
}
SELF_NOTIFY = {"finance-summary", "finance-weekly", "finance-analyze", "finance-council"}

HELP = (
    "🤖 my-automations — your personal automation platform.\n"
    "Everything runs locally on your Mac. Your financial data never leaves the "
    "machine except anonymized aggregates (and public market data) sent to Claude "
    "for analysis. Today it covers the Finance domain; more domains can be added.\n"
    "\n"
    "━━ GLOBAL ━━\n"
    "/help — this overview\n"
    "/list — list every automation, grouped by category (with their ids)\n"
    "/status — which scheduled services are running + each one's last run\n"
    "/run <id> — run any automation by its id (works for every domain)\n"
    "\n"
    "━━ FINANCE · keep data fresh ━━\n"
    "/finance sync — pull latest bank/card/investment data (SimpleFIN) + Splitwise, "
    "detect internal transfers, and AI-categorize transactions\n"
    "/finance brief — scout the web for market-moving news + macro and refresh the "
    "market brief\n"
    "\n"
    "━━ FINANCE · understand your money ━━\n"
    "/finance summary — instant status: net worth, account balances, portfolio, "
    "Splitwise (who owes whom), and your typical-month cashflow\n"
    "/finance weekly — last-7-days spend + balances digest\n"
    "/finance analyze — full AI analysis: cashflow, emergency fund, allocation vs "
    "target, concentration, rebalancing math\n"
    "/finance <question> — ask anything in plain English, e.g. "
    "'how much did I spend on travel in the last 45 days?'\n"
    "\n"
    "━━ FINANCE · investment council ━━\n"
    "/finance council — 8 legendary investors debate YOUR portfolio over live market "
    "data, critique each other, then a fiduciary mediator gives ranked, prioritized advice\n"
    "/finance expert <name> [question] — get one investor's take. Names:\n"
    "   • value (Buffett) • index (Bogle) • macro (Dalio) • growth (Lynch)\n"
    "   • cycles (Marks) • quant (Fama-French) • tailrisk (Taleb) • disruptive (Wood)\n"
    "   e.g. /finance expert buffett Is my META position too big?\n"
    "\n"
    "Informational only — not licensed financial advice."
)
FINANCE_HELP = (
    "Finance:\n"
    "/finance <question> — natural-language Q&A\n"
    "/finance summary — current status (net worth, accounts, Splitwise)\n"
    "/finance sync — pull bank + Splitwise + AI-categorize\n"
    "/finance analyze — full AI analysis report\n"
    "/finance council — 8-investor panel debate → mediator advice\n"
    "/finance expert <name> [question] — one investor's take (value, index, macro,\n"
    "    growth, cycles, quant, tailrisk, disruptive)\n"
    "/finance brief — refresh the market news brief\n"
    "/finance weekly — weekly spend/balance digest"
)


def root_command(text: str) -> str | None:
    """Return the namespaced root command (e.g. '/finance', '/run') or None."""
    if not text:
        return None
    tok = text.strip().split()[0].lower().split("@")[0]  # strip /cmd@botname
    return tok if tok in GLOBAL else None


def _rest(text: str) -> str:
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
        return "No runs yet. Try /finance sync to get started."
    icon = {"ok": "🟢", "error": "🔴"}
    lines = ["📋 Last run of each automation:"]
    for i, r in sorted(latest.items()):
        when = (r.get("end", "") or "")[:16].replace("T", " ")
        lines.append(f"{icon.get(r.get('status'), '⚪️')} {i} — {when}")
    return "\n".join(lines)


def _list_text() -> str:
    sys.path.insert(0, str(config.LIB_PY))
    import manifest
    cats: dict[str, list[str]] = {}
    for m in manifest.discover():
        cats.setdefault(m.category, []).append(m.id)
    out = ["Automations:"]
    for cat, ids in sorted(cats.items()):
        out.append(f"\n▸ {cat}")
        out += [f"  {i}" for i in sorted(ids)]
    out.append("\nRun any with /run <id>.")
    return "\n".join(out)


def _run(automation_id: str) -> tuple[bool, str]:
    res = subprocess.run(
        [sys.executable, "cli/auto", "run", automation_id],
        cwd=str(config.REPO_ROOT), capture_output=True, text=True,
    )
    ok = res.returncode == 0
    tail = (res.stdout or res.stderr or "").strip().splitlines()
    return ok, (tail[-1] if tail else f"exit {res.returncode}")


def _run_and_report(token: str, chat_id: str, aid: str) -> None:
    _send(token, chat_id, f"▶ running {aid}…")
    ok, msg = _run(aid)
    _send(token, chat_id, f"{'✅' if ok else '❌'} {aid}: {msg}")


def _known_ids() -> set[str]:
    sys.path.insert(0, str(config.LIB_PY))
    import manifest
    return {m.id for m in manifest.discover()}


def handle(token: str, chat_id: str, cmd: str, text: str = "") -> None:
    if cmd in ("/help", "/start"):
        _send(token, chat_id, HELP)
        return
    if cmd == "/status":
        _send(token, chat_id, _status_text())
        return
    if cmd == "/list":
        _send(token, chat_id, _list_text())
        return
    if cmd == "/run":
        rest = _rest(text)
        aid = rest.split()[0] if rest else ""
        if not aid:
            _send(token, chat_id, "Usage: /run <automation-id>. See /status for ids.")
        elif aid not in _known_ids():
            _send(token, chat_id, f"Unknown automation '{aid}'. See /status for ids.")
        else:
            _run_and_report(token, chat_id, aid)
        return
    if cmd == "/finance":
        rest = _rest(text)
        if not rest:
            _send(token, chat_id, FINANCE_HELP)
            return
        first = rest.split()[0].lower()
        if first == "expert":
            parts = rest.split(maxsplit=2)
            if len(parts) < 2:
                sys.path.insert(0, str(config.LIB_PY))
                import council
                _send(token, chat_id, "Usage: /finance expert <name> [question]\nNames: "
                      + ", ".join(council.PERSONAS))
                return
            key = parts[1].lower()
            q = parts[2] if len(parts) > 2 else None
            _send(token, chat_id, f"🧠 consulting {key}…")
            cmd_list = [sys.executable, "lib/py/council_cli.py", "expert", key]
            if q:
                cmd_list += ["--q", q]
            res = subprocess.run(cmd_list, cwd=str(config.REPO_ROOT),
                                 capture_output=True, text=True)
            _send(token, chat_id, (res.stdout or res.stderr or "no output").strip()[:3500])
            return
        if first in FINANCE_ACTIONS:
            ids = FINANCE_ACTIONS[first]
            label = ACTION_LABELS.get(first, first)
            _send(token, chat_id, f"{label}…")
            all_ok, tails, self_notified = True, [], False
            for aid in ids:
                ok, msg = _run(aid)
                all_ok = all_ok and ok
                if aid in SELF_NOTIFY:
                    self_notified = True
                else:
                    tails.append(f"  {'✓' if ok else '✗'} {msg}")
            head = "✅ Done" if all_ok else "⚠️ Finished with errors"
            extra = ("\n" + "\n".join(tails)) if tails else ""
            if self_notified and not tails:
                extra = " — full result sent above 👆"
            _send(token, chat_id, f"{head}{extra}")
        else:                              # treat the whole thing as a question
            _send(token, chat_id, "💭 thinking…")
            sys.path.insert(0, str(config.LIB_PY))
            import finance_ask
            _send(token, chat_id, finance_ask.answer(rest))
        return


OFFSET_FILE = config.DATA_DIR / "telegram_offset"


def _load_offset():
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _save_offset(offset: int) -> None:
    try:
        config.ensure_runtime_dirs()
        OFFSET_FILE.write_text(str(offset))
    except OSError:
        pass


def main() -> None:
    token = secrets_store.get("TELEGRAM_TOKEN")
    chat_id = str(secrets_store.get("TELEGRAM_CHAT_ID"))

    # Resume from the saved position so commands sent while the Mac was asleep
    # are processed on wake (no loss, no replay). Only on a true first run do we
    # skip backlog, to avoid replaying ancient history.
    offset = _load_offset()
    if offset is None:
        init = _api(token, "getUpdates", timeout=0)
        if init.get("ok") and init.get("result"):
            offset = init["result"][-1]["update_id"] + 1
            _save_offset(offset)

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
            _save_offset(offset)
            msg = upd.get("message") or upd.get("edited_message") or {}
            from_chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text", "")
            print(f"[telegram-bot] update {upd['update_id']} chat={from_chat} text={text!r}", flush=True)
            if from_chat != chat_id:        # security: ignore everyone else
                print(f"[telegram-bot] ignored (chat {from_chat} != {chat_id})", flush=True)
                continue
            cmd = root_command(text)
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
