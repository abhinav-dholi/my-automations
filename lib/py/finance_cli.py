"""finance_cli — the scoped toolset a Claude-orchestrated finance run may call.

Subcommands (whitelisted per automation via allowed_tools):
  features        Print anonymized feature packs (JSON) — the ONLY data egress.
  store-insight   Persist an agent insight/report (JSON via stdin) to `insights`.
  notify          Send a message to Telegram (text via arg or stdin).

Raw DB/SQL access is deliberately NOT exposed here, so a Claude run scoped to
these subcommands can never pull raw transactions into its context (§15.3).
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

import config
import features as features_mod
import notify as notify_mod
import secrets_store
import simplefin
import store


def _load_profile() -> dict:
    # Real profile.yaml is gitignored; profile.example.yaml is the committed fallback.
    real = config.DATA_DIR / "profile.yaml"
    example = config.REPO_ROOT / "profile.example.yaml"
    path = real if real.exists() else example
    if path.exists():
        return yaml.safe_load(path.read_text()) or {}
    return {}


def cmd_features(args) -> int:
    feats = features_mod.build(profile=_load_profile(), lookback_days=args.lookback)
    print(json.dumps(feats, indent=2))
    return 0


def cmd_window(args) -> int:
    print(json.dumps(features_mod.window_summary(args.days), indent=2))
    return 0


def cmd_accounts(_args) -> int:
    """Per-account snapshot: balance, type/subtype, institution, holdings. No
    full account numbers (names carry only the bank's masked last-4)."""
    with store.connect() as conn:
        bals = [dict(r) for r in store.latest_balances(conn)]
        holds = [dict(r) for r in store.latest_holdings(conn)]
    by_acct: dict = {}
    for h in holds:
        by_acct.setdefault(h["account_id"], []).append(
            {"symbol": h["symbol"], "name": h["name"],
             "value": round(h.get("market_value") or 0, 2)})
    out = [{
        "name": b["name"], "type": b["type"], "subtype": b.get("subtype") or "",
        "institution": b.get("institution") or "", "balance": round(b["balance"], 2),
        "holdings": sorted(by_acct.get(b["id"], []), key=lambda x: -x["value"])[:25],
    } for b in bals]
    print(json.dumps(out, indent=2))
    return 0


def cmd_transactions(args) -> int:
    """Raw transactions (date, amount, DESCRIPTION, category) over a window, with
    optional category/text filters. Exposes merchant descriptions to the caller
    by design (chat-agent scope) — never account numbers."""
    since = int(time.time()) - args.days * 86400
    with store.connect() as conn:
        rows = [dict(r) for r in store.transactions_since(conn, since)]
    s = (args.search or "").lower().strip()
    cat = (args.category or "").lower().strip()
    out = []
    for r in rows:
        if cat and (r.get("category") or "").lower() != cat:
            continue
        if s and s not in (r.get("description") or "").lower():
            continue
        out.append({
            "date": time.strftime("%Y-%m-%d", time.localtime(r["posted"])),
            "amount": round(r["amount"], 2),
            "description": r.get("description") or "",
            "category": r.get("category") or "",
        })
        if len(out) >= args.limit:
            break
    print(json.dumps({"count": len(out), "days": args.days, "transactions": out}, indent=2))
    return 0


def cmd_store_insight(args) -> int:
    # Prefer --json (single-line, permission-friendly); fall back to stdin.
    payload = args.json if args.json is not None else sys.stdin.read()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {"report": payload}
    store.init_db()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO insights (agent,generated_at,model,input_hash,output_json)
               VALUES (?,?,?,?,?)""",
            (args.agent, time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             args.model, args.input_hash, json.dumps(data)),
        )
    # Per-agent copy for the UI (e.g. finance-analyze.json, finance-council.json).
    config.ensure_runtime_dirs()
    (config.DATA_DIR / f"{args.agent}.json").write_text(json.dumps(data, indent=2))
    print("stored")
    return 0


def cmd_notify(args) -> int:
    text = args.text if args.text is not None else sys.stdin.read()
    # Allow single-line invocation with literal \n escapes (real newlines in a
    # CLI arg would be split into separate commands by the permission parser).
    text = text.replace("\\n", "\n")
    notify_mod.send(text)
    print("sent")
    return 0


def cmd_setup_simplefin(_args) -> int:
    """Claim a SimpleFIN setup token and store the Access URL in Keychain.

    The token is read hidden (no echo); the resulting Access URL — which embeds
    credentials — is written straight to Keychain and never printed.
    """
    token = getpass.getpass("Paste SimpleFIN setup token (hidden): ").strip()
    if not token:
        print("No token entered.", file=sys.stderr)
        return 1
    access_url = simplefin.claim_access_url(token)
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", secrets_store.KEYCHAIN_SERVICE, "-a", "SIMPLEFIN_ACCESS_URL",
         "-w", access_url],
        check=True,
    )
    print(f"Stored SIMPLEFIN_ACCESS_URL in Keychain (url length {len(access_url)}).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="finance_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("features")
    pf.add_argument("--lookback", type=int, default=400)
    pf.set_defaults(func=cmd_features)

    pw = sub.add_parser("window")
    pw.add_argument("--days", type=int, default=30)
    pw.set_defaults(func=cmd_window)

    sub.add_parser("accounts").set_defaults(func=cmd_accounts)

    pt = sub.add_parser("transactions")
    pt.add_argument("--days", type=int, default=90)
    pt.add_argument("--category", default=None)
    pt.add_argument("--search", default=None)
    pt.add_argument("--limit", type=int, default=200)
    pt.set_defaults(func=cmd_transactions)

    ps = sub.add_parser("store-insight")
    ps.add_argument("--agent", default="finance-analyze")
    ps.add_argument("--model", default="")
    ps.add_argument("--input-hash", dest="input_hash", default="")
    ps.add_argument("--json", default=None, help="Report JSON as a single-line string (else read stdin)")
    ps.set_defaults(func=cmd_store_insight)

    pn = sub.add_parser("notify")
    pn.add_argument("text", nargs="?", default=None)
    pn.set_defaults(func=cmd_notify)

    sub.add_parser("setup-simplefin").set_defaults(func=cmd_setup_simplefin)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
