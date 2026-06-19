"""finance_chat — a conversational finance agent that returns prose AND charts.

Claude answers over the user's own data via the scoped finance_cli tools, then
emits a strict JSON contract {answer, charts, followups}. Charts are either
references into a UI-owned catalog (the app draws them from its own data, so no
numbers round-trip through the model) or small custom specs. The UI renders;
Claude never executes plotting code.

Privacy scope (user-chosen): aggregates + per-account detail + transaction
DESCRIPTIONS (never account numbers). Requires CLAUDE_CODE_OAUTH_TOKEN.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import config

# Claude may call any finance_cli subcommand (read-only data + the descriptions
# the user opted into). It cannot touch the DB directly or run other commands.
ALLOWED_TOOLS = "Bash(python3 lib/py/finance_cli.py:*)"

# UI-owned chart catalog: id -> what it shows. The app renders these from the
# data it already loaded, so Claude only needs to pick which ones fit.
CATALOG = {
    "spend_by_category": "donut — typical-month spend by category",
    "monthly_income_vs_spend": "grouped bars — income vs spend per month",
    "allocation_vs_target": "bars — current vs target allocation (equity/bonds/cash)",
    "net_worth_composition": "donut — liquid cash / taxable / retirement",
    "holdings_mix": "donut — holdings by symbol",
    "top_merchants": "bar — top merchants by spend (last 90d)",
    "performance_vs_benchmark": "line — your holdings vs market benchmarks (rebased)",
    "accounts_by_kind": "donut — asset value by account kind",
    "splitwise_by_friend": "bar — Splitwise balance by friend",
}

PROMPT_TEMPLATE = """\
You are a sharp, concise personal-finance assistant answering questions about the
USER'S OWN money. Use the conversation for context; answer the LATEST user message.

Data tools (run via Bash; use ONLY numbers they return — never invent figures):
- python3 lib/py/finance_cli.py features
    typical-month aggregates: income/spend, spend_by_category, allocation, net
    worth breakdown, monthly_series, resilience, splitwise.
- python3 lib/py/finance_cli.py window --days N
    EXACT totals over the last N days (use for "last N days/weeks" questions).
- python3 lib/py/finance_cli.py accounts
    per-account balance, type/subtype (hysa/brokerage/retirement/equity), institution, holdings.
- python3 lib/py/finance_cli.py transactions --days N [--category C] [--search TEXT] [--limit N]
    raw transactions WITH descriptions (merchant-level). Spend is negative.

Notes: spend already excludes transfers, card payments, and investment moves.

CASH MODEL — be precise, don't conflate (the user cares about this):
- "Cash surplus" / "savings" = cashflow.operating_cash_surplus (take-home salary −
  spending). That is the real recurring cash kept. Use THIS for cash-surplus questions.
- cashflow.monthly_pretax_retirement is 401(k)/HSA — wealth, NOT spendable cash.
- cashflow.one_time_inflows_window is refunds/bonuses over the window — LUMPY, not recurring.
- cashflow.monthly_moved_to_savings is transfers INTO savings — money relocation, NOT income.
- Never invent "other income"; never call transfers or 401(k) "cash surplus".
- net_worth uses holdings value for investment accounts (vested equity with $0 cash counts).
Be direct, cite the numbers, and stay informational (not licensed advice).

After gathering data, RESPOND WITH ONLY ONE JSON OBJECT — no prose, no code fences:
{{
  "answer": "<concise markdown answer that cites the numbers>",
  "charts": [ <0-3 charts that genuinely illustrate the answer> ],
  "followups": [ "<up to 3 short, specific follow-up questions>" ]
}}

Each chart is EITHER a catalog reference (PREFERRED — the app draws it from its
own data): {{"id": "<one of the catalog ids>"}}
or a custom chart built from tool numbers:
  {{"type":"bar"|"line","title":"...","x":[...],"y":[...]}}
  {{"type":"pie","title":"...","labels":[...],"y":[...]}}
Catalog ids and meaning:
{catalog}

Only include charts that add value (often 1; sometimes 0). Prefer catalog ids.

Conversation so far:
{transcript}
"""


def _transcript(history: list[dict], max_msgs: int = 10) -> str:
    msgs = history[-max_msgs:]
    return "\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs) or "USER: (none)"


def _extract_json(text: str) -> dict | None:
    """Pull the JSON object out of Claude's reply (tolerates ``` fences / stray
    prose around it)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


def chat(history: list[dict]) -> dict:
    """history: [{'role':'user'|'assistant','content':str}, ...].
    Returns {'answer': str, 'charts': list, 'followups': list}."""
    prompt = PROMPT_TEMPLATE.format(
        catalog="\n".join(f"  - {k}: {v}" for k, v in CATALOG.items()),
        transcript=_transcript(history),
    )
    claude_bin = os.environ.get("AUTO_CLAUDE_BIN", "claude")
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--allowedTools", ALLOWED_TOOLS,
         "--output-format", "json"],
        cwd=str(config.REPO_ROOT), capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"answer": f"(chat failed: {(result.stderr or '').strip()[:200]})",
                "charts": [], "followups": []}
    try:
        raw = json.loads(result.stdout).get("result", "")
    except json.JSONDecodeError:
        raw = result.stdout
    parsed = _extract_json(raw)
    if not parsed:
        return {"answer": raw.strip()[:2000] or "(no answer)", "charts": [], "followups": []}
    return {
        "answer": str(parsed.get("answer", "")).strip() or "(no answer)",
        "charts": [c for c in (parsed.get("charts") or []) if isinstance(c, dict)][:3],
        "followups": [str(f) for f in (parsed.get("followups") or [])][:3],
    }


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]).strip()
    if not q:
        print("Usage: python lib/py/finance_chat.py \"<question>\"")
        raise SystemExit(1)
    out = chat([{"role": "user", "content": q}])
    print(json.dumps(out, indent=2))
