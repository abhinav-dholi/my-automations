"""council_cli — run the full investment council or a single expert.

  council                 full 8-persona debate → store insight + Telegram
  expert <key> [--q "…"]  one persona's take (prints; used by the bot)

Personas: value, index, macro, growth, cycles, quant, tailrisk, disruptive.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import config
import council
import notify as notify_mod
import store


def _store(res: dict) -> None:
    store.init_db()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO insights (agent,generated_at,model,input_hash,output_json)
               VALUES (?,?,?,?,?)""",
            ("finance-council", time.strftime("%Y-%m-%dT%H:%M:%S%z"), "claude", "",
             json.dumps(res)),
        )
    config.ensure_runtime_dirs()
    (config.DATA_DIR / "finance-council.json").write_text(json.dumps(res, indent=2))


def _notify_summary(res: dict) -> None:
    lines = ["🏛 Investment Council", res.get("summary", "")]
    if res.get("whats_changed"):
        lines.append(f"\n🔄 Since last time: {res['whats_changed']}")
    actions = res.get("actions", [])[:3]
    if actions:
        lines.append("\nTop actions (balanced synthesis):")
        for a in actions:
            sup = ",".join(a.get("supported_by", []) or [])
            lines.append(f"{a.get('priority','?')}. {a.get('action','')} "
                         f"[{a.get('confidence','')}{'; backs: '+sup if sup else ''}]")
    tradeoffs = res.get("tradeoffs", [])
    if tradeoffs:
        lines.append(f"\n⚖️ Key tradeoff: {tradeoffs[0].get('issue','')}")
    lines.append("\nBalanced & informational — not licensed financial advice. "
                 "Full detail: dashboard Council page.")
    notify_mod.send("\n".join(lines))


def cmd_council(_a) -> int:
    res = council.run_council()
    _store(res)
    _notify_summary(res)
    print(f"[council] {len(res['personas'])} personas, {len(res['actions'])} actions. "
          f"Verdict: {res.get('verdict','')[:100]}")
    return 0


def cmd_expert(args) -> int:
    try:
        res = council.run_expert(args.key, args.q)
    except KeyError:
        print(f"Unknown expert '{args.key}'. Options: {', '.join(council.PERSONAS)}")
        return 1
    print(f"🧠 {res['persona']}\n\n{res['answer']}")
    if args.notify:
        notify_mod.send(f"🧠 {res['persona']}\n\n{res['answer']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="council_cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("council").set_defaults(func=cmd_council)
    pe = sub.add_parser("expert")
    pe.add_argument("key")
    pe.add_argument("--q", default=None, help="optional specific question")
    pe.add_argument("--notify", action="store_true", help="also send to Telegram")
    pe.set_defaults(func=cmd_expert)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
