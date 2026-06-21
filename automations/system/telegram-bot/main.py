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

# Freshness guard — on-demand reads sync ONLY when their data is older than this
# (hours), so the DB (synced on a daily/twice-daily cron) is never silently stale
# for a /finance read. Avoids re-syncing on every command. Bank is cheap HTTP;
# the market brief (news) is a Claude run, so its threshold is wider and it's
# only refreshed for the deep market reports.
FRESH_MAX_H = {"bank": 2.0, "prices": 12.0, "news": 8.0}
SOURCE_SYNC = {                      # source -> automation(s) that refresh it
    "bank": ["finance-sync", "splitwise-sync"],
    "prices": ["market-data-sync"],
    "news": ["market-watch"],
}
ACTION_NEEDS = {                     # finance action -> data sources it reads
    "summary": ["bank"],
    "weekly": ["bank"],
    "question": ["bank"],
    "expert": ["bank", "prices"],
    "analyze": ["bank", "prices", "news"],
    "council": ["bank", "prices", "news"],
}

HELP = (
    "🤖 my-automations — your personal automation platform.\n"
    "Everything runs locally on your Mac. Today it covers the Finance domain; "
    "more domains can be added.\n"
    "\n"
    "💬 TIP: just type a question — no command needed — e.g. \"how much did I "
    "spend on dining last month?\" You'll get an answer plus a chart, and the "
    "answer is remembered (shared with the dashboard).\n"
    "Or tap a button below.\n"
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
    "/finance new — start a fresh chat (clears the running conversation context)\n"
    "/finance chats — list & continue a past conversation (shared with the dashboard)\n"
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


def _send(token: str, chat_id: str, text: str, *, markdown: bool = False,
          reply_markup: dict | None = None) -> None:
    """Send a message. With markdown=True, try Telegram Markdown and fall back to
    plain text if it fails to parse (agent output can contain stray * _ ` )."""
    payload: dict = {"chat_id": chat_id, "text": text[:4096],
                     "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if markdown:
        payload["parse_mode"] = "Markdown"
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json=payload, timeout=25)
    if markdown:
        try:
            ok = r.json().get("ok", False)
        except ValueError:
            ok = False
        if not ok:                       # Markdown parse failed → resend as plain
            payload.pop("parse_mode", None)
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json=payload, timeout=25)


def _send_photo(token: str, chat_id: str, png: bytes, caption: str = "") -> None:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption[:1024]},
        files={"photo": ("chart.png", png, "image/png")}, timeout=30,
    )


# --- inline keyboards ---------------------------------------------------
def _main_menu() -> dict:
    """Tappable action menu (callback_data are short commands)."""
    return {"inline_keyboard": [
        [{"text": "📊 Summary", "callback_data": "/finance summary"},
         {"text": "🔄 Sync", "callback_data": "/finance sync"}],
        [{"text": "🧠 Analyze", "callback_data": "/finance analyze"},
         {"text": "🏛 Council", "callback_data": "/finance council"}],
        [{"text": "🌐 Market brief", "callback_data": "/finance brief"},
         {"text": "📅 Weekly", "callback_data": "/finance weekly"}],
        [{"text": "🆕 New chat", "callback_data": "/finance new"},
         {"text": "📚 Chats", "callback_data": "/finance chats"}],
        [{"text": "📋 Status", "callback_data": "/status"},
         {"text": "❓ Help", "callback_data": "/help"}],
    ]}


# Follow-up questions can exceed Telegram's 64-byte callback_data limit, so we
# stash them in memory and reference by short token (resets on restart — fine).
_FOLLOWUPS: dict[str, str] = {}


def _followups_message(followups: list[str]):
    """Telegram buttons don't wrap, so long follow-up questions get truncated.
    List the full questions in the message text and keep the buttons compact
    (just numbers); tapping resolves back to the full question via its token."""
    fus = [f for f in (followups or []) if f][:3]
    if not fus:
        return None, None
    lines, row = ["💬 Follow up — tap a number, or just type your own:"], []
    for i, fu in enumerate(fus, 1):
        tok = f"f{len(_FOLLOWUPS)}_{i}"
        _FOLLOWUPS[tok] = fu
        lines.append(f"{i}. {fu}")
        row.append({"text": f"💬 {i}", "callback_data": f"fu:{tok}"})
    return "\n".join(lines), {"inline_keyboard": [row]}


def _resolve_callback(data: str) -> str:
    """Map a callback payload back to the command/question text to handle."""
    if data.startswith("fu:"):
        return "/finance " + _FOLLOWUPS.get(data[3:], "")
    if data.startswith("conv:"):            # continue a picked past conversation
        return "/finance use " + data[5:]
    return data


# --- conversation tracking (so /finance new starts a fresh thread) ------
_CONV_FILE = config.DATA_DIR / "telegram_conv.json"


def _active_conv(chat_id: str) -> str:
    """The current Ask-AI conversation id for this chat (shared with the
    dashboard). Defaults to a stable id until the user starts a new chat."""
    import json
    try:
        m = json.loads(_CONV_FILE.read_text())
    except (FileNotFoundError, ValueError):
        m = {}
    return m.get(str(chat_id)) or f"tg-{chat_id}"


def _set_conv(chat_id: str, conv_id: str) -> None:
    """Point this chat at a specific conversation id (start fresh, or continue an
    existing one picked from /finance chats)."""
    import json
    try:
        m = json.loads(_CONV_FILE.read_text())
    except (FileNotFoundError, ValueError):
        m = {}
    m[str(chat_id)] = conv_id
    config.ensure_runtime_dirs()
    _CONV_FILE.write_text(json.dumps(m))


def _reset_conv(chat_id: str) -> str:
    """Begin a fresh conversation; the old one stays saved in history."""
    import time as _t
    new = f"tg-{chat_id}-{int(_t.time())}"
    _set_conv(chat_id, new)
    return new


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


def _age_hours(ts: str | None) -> float:
    """Hours since an ISO8601 timestamp (or date-only string). Huge if missing
    or unparseable, so an absent source always counts as stale."""
    if not ts:
        return 1e9
    import datetime as dt
    try:
        if len(ts) == 10:                       # date-only, e.g. market_prices.date
            d = dt.datetime.strptime(ts, "%Y-%m-%d").date()
            return (dt.date.today() - d).days * 24.0
        d = dt.datetime.fromisoformat(ts)
        return (dt.datetime.now(d.tzinfo) - d).total_seconds() / 3600.0
    except ValueError:
        return 1e9


def ensure_fresh(token: str, chat_id: str, sources: list[str]) -> None:
    """Refresh any of `sources` whose data is older than its threshold, before a
    read. Syncs run silently except for one heads-up so the user knows why a
    read paused. Failures are non-fatal — the read proceeds on existing data."""
    sys.path.insert(0, str(config.LIB_PY))
    import store
    with store.connect() as conn:
        fresh = store.data_freshness(conn)
    to_run, stale = [], []
    for s in sources:
        if _age_hours(fresh.get(s)) > FRESH_MAX_H[s]:
            stale.append(s)
            for aid in SOURCE_SYNC[s]:
                if aid not in to_run:
                    to_run.append(aid)
    if not to_run:
        return
    _send(token, chat_id, f"🔄 {', '.join(stale)} data was stale — refreshing first…")
    for aid in to_run:
        _run(aid)


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
        _send(token, chat_id, HELP, reply_markup=_main_menu())
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
        if first in ("new", "reset", "newchat"):
            _reset_conv(chat_id)
            _send(token, chat_id, "🆕 Started a fresh chat. (Your previous conversation is "
                  "saved — it stays in the dashboard's Ask AI history.)", reply_markup=_main_menu())
            return
        if first in ("chats", "history"):
            sys.path.insert(0, str(config.LIB_PY))
            import store
            store.init_db()
            with store.connect() as conn:
                chats = store.list_chats(conn, 8)
            if not chats:
                _send(token, chat_id, "No saved chats yet — just ask a question to start one.")
                return
            active = _active_conv(chat_id)
            lines = ["📚 Pick a conversation to continue (tap a number; ▶ = current):"]
            btns = []
            for i, c in enumerate(chats, 1):
                title = (c.get("title") or "Chat").strip().replace("\n", " ")[:60]
                mark = "▶ " if c["conv_id"] == active else ""
                when = (c.get("started", "") or "")[:10]
                lines.append(f"{i}. {mark}{title}  ·  {when}")
                btns.append({"text": str(i), "callback_data": f"conv:{c['conv_id']}"})
            kb = [btns[j:j + 4] for j in range(0, len(btns), 4)]   # numbers, 4 per row
            _send(token, chat_id, "\n".join(lines), reply_markup={"inline_keyboard": kb})
            return
        if first == "use":
            conv_id = rest.split(maxsplit=1)[1].strip() if len(rest.split()) > 1 else ""
            if not conv_id:
                _send(token, chat_id, "Usage: /finance use <conversation-id> (or tap one in /finance chats)")
                return
            _set_conv(chat_id, conv_id)
            sys.path.insert(0, str(config.LIB_PY))
            import store
            with store.connect() as conn:
                hist = store.load_chat(conn, conv_id)
            last = next((m["content"] for m in reversed(hist) if m["role"] == "assistant"), "")
            tail = ("\n\n_Last reply:_ " + last[:300]) if last else ""
            _send(token, chat_id, f"↩️ Continuing this chat ({len(hist)} messages). Ask away.{tail}",
                  markdown=bool(tail))
            return
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
            ensure_fresh(token, chat_id, ACTION_NEEDS["expert"])
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
            ensure_fresh(token, chat_id, ACTION_NEEDS.get(first, []))
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
            _send(token, chat_id, f"{head}{extra}", reply_markup=_main_menu())
        else:                              # NL question → smart chat agent (UI parity)
            _send(token, chat_id, "💭 thinking…")
            ensure_fresh(token, chat_id, ACTION_NEEDS["question"])
            sys.path.insert(0, str(config.LIB_PY))
            import finance_chat
            import store
            conv_id = _active_conv(chat_id)  # current thread (reset via /finance new); shared w/ UI
            store.init_db()
            with store.connect() as conn:
                history = store.load_chat(conn, conv_id)
            history.append({"role": "user", "content": rest})
            res = finance_chat.chat(history)
            with store.connect() as conn:
                store.save_chat_message(conn, conv_id=conv_id, role="user", content=rest)
                store.save_chat_message(conn, conv_id=conv_id, role="assistant",
                                        content=res["answer"], charts=res.get("charts", []),
                                        followups=res.get("followups", []))
            _send(token, chat_id, res["answer"], markdown=True)
            for spec in (res.get("charts") or [])[:1]:   # one chart keeps it snappy
                try:
                    import finance_viz
                    png = finance_viz.render(spec)
                    if png:
                        _send_photo(token, chat_id, png, caption=spec.get("title", ""))
                except Exception as e:
                    print(f"[telegram-bot] chart render failed: {e}", flush=True)
            ftext, fkb = _followups_message(res.get("followups"))
            if fkb:
                _send(token, chat_id, ftext, reply_markup=fkb)
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

    sys.path.insert(0, str(config.LIB_PY))
    import store
    store.init_db()                         # ensure chat history table exists
    _send(token, chat_id,
          "🤖 *my-automations* online. Tap an action, or just type a question "
          "like _\"how much did I spend on dining last month?\"_",
          markdown=True, reply_markup=_main_menu())
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

            # Tapped inline button → resolve to a command/question and dispatch.
            cb = upd.get("callback_query")
            if cb:
                cb_chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
                data = cb.get("data", "")
                try:
                    _api(token, "answerCallbackQuery", callback_query_id=cb["id"])
                except requests.RequestException:
                    pass
                if cb_chat != chat_id:
                    continue
                resolved = _resolve_callback(data)
                cmd = root_command(resolved)
                print(f"[telegram-bot] callback {data!r} -> {resolved!r}", flush=True)
                if cmd:
                    try:
                        handle(token, chat_id, cmd, resolved)
                    except Exception as e:
                        print(f"[telegram-bot] callback error: {e}", flush=True)
                        _send(token, chat_id, f"⚠️ error: {e}")
                continue

            msg = upd.get("message") or upd.get("edited_message") or {}
            from_chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text", "")
            print(f"[telegram-bot] update {upd['update_id']} chat={from_chat} text={text!r}", flush=True)
            if from_chat != chat_id:        # security: ignore everyone else
                print(f"[telegram-bot] ignored (chat {from_chat} != {chat_id})", flush=True)
                continue
            cmd = root_command(text)
            try:
                if cmd:
                    print(f"[telegram-bot] handling {cmd}", flush=True)
                    handle(token, chat_id, cmd, text)
                elif text and not text.startswith("/"):
                    # Free text → treat as a finance question (no command needed).
                    handle(token, chat_id, "/finance", "/finance " + text)
                else:
                    _send(token, chat_id, "Unknown command.", reply_markup=_main_menu())
            except Exception as e:
                print(f"[telegram-bot] handler error: {e}", flush=True)
                _send(token, chat_id, f"⚠️ error: {e}")


if __name__ == "__main__":
    main()
