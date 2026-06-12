# CLAUDE.md

Personal automation platform. Automations live in `automations/<id>/` with a
manifest; each runs on a schedule, on demand (CLI/UI/Telegram), or as a Claude
Code skill. **Finance is the first category** — the platform is general; new
domains are added as new automations with a different `category`. Full design in
[PRD.md](PRD.md).

## CLI (`cli/auto`)

```bash
python cli/auto list [--category C]     # automations grouped by category
python cli/auto status [--category C]   # service state (running/stopped) + last run
python cli/auto run <id>                # run once now
python cli/auto start  <id|category|all># install + start launchd service(s)
python cli/auto stop   <id|category|all>
python cli/auto restart<id|category|all>
python cli/auto logs <id> [--lines N]   # tail service/schedule logs
python cli/auto ui                      # dashboard on 127.0.0.1:8501 (kills stale, fixed port)
python cli/auto skills                  # regenerate /<id> Claude Code skills
python cli/auto update [--category C]   # pull (if remote) + skills + restart running services
python cli/auto deploy <id>             # (advanced) just generate the launchd/Actions trigger
```

`start/stop/restart/update` accept an **id**, a **category** (e.g. `finance`), or
**`all`**. Deps: `pip install -r requirements.txt`. Secret scanner: `pre-commit install`.

## Architecture

- **`cli/auto`** — front door. Loads manifests, resolves declared secrets (one
  Keychain touch in the parent), injects them as env into the child, runs the
  entry, wraps in structured logging. Also manages launchd services.
- **`automations/<id>/automation.yaml`** — manifest: `id, category, description,
  runtime, entry, triggers[], schedule, tz, secrets[] (names only), allowed_tools[],
  deploy.target`. Add an automation = new folder + manifest + entry; no central wiring.
- **`lib/py/`** — the only cross-cutting code: `config`, `secrets_store`
  (Keychain/env), `scrub`, `runlog`, `notify` (Telegram), `manifest`, `deploy`
  (launchd plists / Actions yaml), `service` (launchctl wrappers), `skillgen`.
  Finance: `store` (SQLite, only SQL), `simplefin`, `splitwise`, `transfers`,
  `finance_rules`, `features` (anonymized aggregates), `categorizer` (AI labels),
  `finance_ask` (NL Q&A).
- **`ui/app.py`** — Streamlit dashboard (Plotly), view layer over the same lib.
  Guard `nav.run()` with `if __name__ == "__main__"` so it's importable for tests.

## Runtimes & triggers

- `runtime: python|node` — deterministic single-step jobs.
- `runtime: claude` — Claude orchestrates the run via headless `claude -p`;
  manifest needs an `allowed_tools` key (tightly scoped); auth via
  `CLAUDE_CODE_OAUTH_TOKEN` (subscription, no API key).
- Triggers: `cli`, `schedule` (launchd StartCalendarInterval, local tz),
  `service` (launchd KeepAlive daemon), `skill`, `webhook` (reserved). `managed`
  = has schedule or service → installable via `auto start`.

## Categories

`category:` groups automations (default `general`). All current ones are
`finance`. Adding a new domain (e.g. `productivity`, `home`): create
`automations/<id>/` with `category: <domain>` — `list/status/start` group and
target by it automatically. No code changes needed.

## Conventions

- **Secrets are names, not values.** Declare names; store values in Keychain
  (`security add-generic-password -s my-automations -a NAME -w`) or Actions
  Secrets. Never log values — route outbound text through `scrub.clean`.
- Stdlib-shadow: the secrets module is `secrets_store`, not `secrets`.
- New finance SQL goes in `store.py` only.
- Generated artifacts (`.claude/skills/`, `deploy/launchd/`) are gitignored —
  regenerate with `auto skills` / `auto deploy`; never commit them.
- After editing a running automation's code, `auto update` (or `auto restart <id>`).

## Finance subsystem (local-only, never cloud)

Sources: **SimpleFIN** (banks/cards/investments, $15/yr; `SIMPLEFIN_ACCESS_URL`)
and **Splitwise** (free; `SPLITWISE_API_KEY`). Data in SQLite `data/finance.db`
(gitignored, FileVault at rest). Automations: `finance-sync` (ETL, backfills
~9mo in 45-day chunks, internal-transfer detection), `splitwise-sync`,
`finance-categorize` (AI), `finance-weekly` (digest), `finance-analyze` (Claude),
`telegram-bot` (service).

**Accuracy model (don't regress):**
- Income = payroll/stipend only (keyword `Income`); other inflows → `Credit`
  (excluded from income AND spend). Positive amount is NOT assumed income.
- Spend excludes money movement: `NON_SPEND = {Income, Transfer, Payment,
  Investment, Credit}`. Internal transfers detected by equal-magnitude
  debit/credit across accounts within 4 days (`transfers.py`) → relabel both legs.
- Monthly income/spend = **median of complete calendar months** (drop current +
  partial-start month); one-off months don't skew. `features.window_summary(days)`
  gives exact raw N-day totals for date-bounded questions.
- Net worth = sum of all account balances (investment balance already includes
  holdings — never add holdings again). Portfolio = funded investment-account
  balances; unvested/$0 accounts excluded. Allocation = % of investable assets
  (cash + investments), computed deterministically in `features` — analysis uses
  it verbatim.

**Category labels** — three sources, precedence **manual > ai > rule**:
- `rule` (finance_rules keywords + learned merchant rules + transfer-match), set each sync.
- `ai` (categorizer): Claude labels only Other/Uncategorized (so it never
  overrides transfer/payroll/manual). Cut "Other" 75%→<1%.
- `manual` (UI edit): wins always; also learns merchant→category, re-tags
  matching txns, and persists across syncs (`store.learn_category`).

**Privacy boundary:** `finance-analyze` and `finance_ask` send only aggregates
to Claude. `finance-categorize` and `/finance` send transaction *descriptions*
(deliberate accuracy tradeoff) — never account numbers/ids. `--allowedTools`
scoping keeps `finance-analyze` to aggregate tools only. Analysis output carries
a "not licensed financial advice" disclaimer.

## Security

See PRD §11. Scrub secrets from output; HTTPS + TLS verify; treat `401/403/402`
as events (stop+notify); gitleaks pre-commit; no raw financial data in the repo
tree (gitignored `data/`).

## Telegram bot

Always-on service. Chat-id whitelisted (ignores everyone else). Commands:
`/analyze`, `/finance <question>` (NL Q&A), `/sync`, `/categorize`, `/weekly`,
`/status`, `/help`. Logs to `logs/telegram-bot.service.out` (unbuffered).

## launchd notes

`auto start` writes `~/Library/LaunchAgents/com.my-automations.<id>.plist` and
bootstraps it. Plists set `PYTHONUNBUFFERED` + a `PATH` incl. `~/.local/bin`
(for `claude`). Harmless `getcwd … Operation not permitted` lines in stderr come
from the login shell under macOS TCC (`~/Documents`); all paths are absolute so
runs work. Force a run: `launchctl kickstart gui/$(id -u)/com.my-automations.<id>`.

## Testing

No formal suite. Validate with throwaway scripts that set `AUTO_DATA_DIR` to a
temp dir and monkeypatch `simplefin.get_accounts` / `secrets_store.get`. Runner
accepts `AUTO_CLAUDE_BIN` to stub `claude`. UI: `from streamlit.testing.v1 import
AppTest` for the default page; import `ui/app.py` as a module (nav guarded) to
call page functions directly.
