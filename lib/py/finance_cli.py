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
import json
import sys
import time
from pathlib import Path

import yaml

import config
import features as features_mod
import notify as notify_mod
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


def cmd_store_insight(args) -> int:
    payload = sys.stdin.read()
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
    # Also drop a human-readable copy for the UI.
    config.ensure_runtime_dirs()
    (config.DATA_DIR / "finance-analysis.json").write_text(json.dumps(data, indent=2))
    print("stored")
    return 0


def cmd_notify(args) -> int:
    text = args.text or sys.stdin.read()
    notify_mod.send(text)
    print("sent")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="finance_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("features")
    pf.add_argument("--lookback", type=int, default=90)
    pf.set_defaults(func=cmd_features)

    ps = sub.add_parser("store-insight")
    ps.add_argument("--agent", default="finance-analyze")
    ps.add_argument("--model", default="")
    ps.add_argument("--input-hash", dest="input_hash", default="")
    ps.set_defaults(func=cmd_store_insight)

    pn = sub.add_parser("notify")
    pn.add_argument("text", nargs="?", default=None)
    pn.set_defaults(func=cmd_notify)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
