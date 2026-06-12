"""AI transaction categorizer — Claude labels what the rules couldn't.

Targets only transactions whose effective category is Other/Uncategorized AND
that have no manual or AI label yet, so it never overrides high-confidence
labels (transfer-pairing, payroll, manual edits). Stores label_source='ai',
which sits below manual and above rule in precedence.

PRIVACY: this sends transaction *descriptions* (merchant text + amount) to
Claude — a deliberate tradeoff for accuracy, beyond the aggregates-only boundary
used elsewhere. No account numbers/ids are sent (lines are numbered locally).
Requires CLAUDE_CODE_OAUTH_TOKEN in the environment.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import config
import finance_rules
import store

CATEGORIES = sorted(set(
    [c for c, _ in finance_rules.CATEGORY_RULES]
    + list(finance_rules.NON_SPEND) + ["Other"]
))

PROMPT = """\
You are a precise bank-transaction categorizer. Assign each transaction exactly
one category from this list (use the money-movement ones for non-spending):
{cats}

Guidance: Transfer = moving your own money between accounts; Payment = paying a
credit card/loan; Investment = buying securities / brokerage funding; Income =
salary/stipend; Credit = refunds/reimbursements/other inflows; Rent, Groceries,
Dining, Transport, Travel, Shopping, Subscriptions, Utilities, Health for spend.
Negative amount = outflow, positive = inflow.

Return ONLY a JSON object mapping the line number to a category, e.g.
{{"1":"Dining","2":"Transfer"}}. No prose, no code fences.

Transactions (number. amount | description):
{lines}
"""


def _run_claude(prompt: str) -> str:
    claude_bin = os.environ.get("AUTO_CLAUDE_BIN", "claude")
    res = subprocess.run(
        [claude_bin, "-p", prompt, "--allowedTools", "", "--output-format", "json"],
        cwd=str(config.REPO_ROOT), capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError((res.stderr or "claude failed").strip()[:200])
    try:
        return json.loads(res.stdout).get("result", "")
    except json.JSONDecodeError:
        return res.stdout


def _parse_mapping(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def categorize_uncategorized(limit: int = 150) -> dict:
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT t.id, t.description, t.amount
               FROM transactions t
               JOIN transaction_labels lr ON lr.txn_id=t.id AND lr.label_source='rule'
               LEFT JOIN transaction_labels lm ON lm.txn_id=t.id AND lm.label_source='manual'
               LEFT JOIN transaction_labels la ON la.txn_id=t.id AND la.label_source='ai'
               WHERE lr.category IN ('Other','Uncategorized')
                 AND lm.txn_id IS NULL AND la.txn_id IS NULL
               ORDER BY ABS(t.amount) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    if not rows:
        return {"labeled": 0, "candidates": 0, "note": "nothing uncategorized"}

    idx_map = {i + 1: r["id"] for i, r in enumerate(rows)}
    lines = "\n".join(
        f'{i + 1}. {r["amount"]:.2f} | {(r["description"] or "")[:80]}'
        for i, r in enumerate(rows)
    )
    text = _run_claude(PROMPT.format(cats=", ".join(CATEGORIES), lines=lines))
    mapping = _parse_mapping(text)

    valid = set(CATEGORIES)
    n = 0
    with store.connect() as conn:
        for k, cat in mapping.items():
            try:
                tid = idx_map.get(int(k))
            except (ValueError, TypeError):
                continue
            if tid and cat in valid:
                store.set_label(conn, txn_id=tid, category=cat,
                                label_source="ai", confidence=0.7, model="claude")
                n += 1
    return {"labeled": n, "candidates": len(rows)}


if __name__ == "__main__":
    print(categorize_uncategorized())
