"""Natural-language finance Q&A — Claude answers over anonymized features.

Used by the Telegram bot's /finance command and runnable directly:
    python lib/py/finance_ask.py "how much did I spend on dining?"

Same privacy boundary as finance-analyze: Claude's only data tool is the
aggregate `features` builder (no raw-transaction access). Requires
CLAUDE_CODE_OAUTH_TOKEN in the environment.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import config

ALLOWED_TOOLS = "Bash(python3 lib/py/finance_cli.py features:*)"

PROMPT_TEMPLATE = """\
Answer the user's personal-finance question using ONLY their own data.

Get the data by running: python3 lib/py/finance_cli.py features
That returns anonymized aggregates (cashflow, spend by category, resilience,
portfolio allocation, net worth, Splitwise). It is your ONLY data source — there
is no raw-transaction access by design.

Rules:
- Use ONLY numbers from `features`; never invent figures. Cite the relevant
  number(s) in your answer.
- Spend already excludes transfers, card payments, and investment moves.
- Be concise and direct (a few sentences). This is informational, not licensed
  financial advice.

User question: {question}
"""


def answer(question: str) -> str:
    prompt = PROMPT_TEMPLATE.format(question=question.strip())
    claude_bin = os.environ.get("AUTO_CLAUDE_BIN", "claude")
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--allowedTools", ALLOWED_TOOLS,
         "--output-format", "json"],
        cwd=str(config.REPO_ROOT), capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"(finance-ask failed: {(result.stderr or '').strip()[:200]})"
    try:
        return json.loads(result.stdout).get("result", "").strip() or "(no answer)"
    except json.JSONDecodeError:
        return result.stdout.strip()[:1500] or "(no answer)"


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]).strip()
    if not q:
        print("Usage: python lib/py/finance_ask.py \"<question>\"")
        raise SystemExit(1)
    print(answer(q))
