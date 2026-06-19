# Module reference

Every module in the platform, what it does, and how it fits the
[financial-intelligence system](financial-intelligence-system.svg). See
[../README.md](../README.md) for the overview and [../PRD.md](../PRD.md) for design.

---

## Front door

| Module | Role |
|---|---|
| `cli/auto` | The runner & service manager. Loads manifests, resolves declared secrets (one Keychain touch in the parent), injects them as env into the child, runs the entry, wraps it in structured logging. Subcommands: `list · status · run · start · stop · restart · logs · ui · skills · update · deploy`. |
| `bin/auto` | Thin launcher so `auto <cmd>` works from any directory (resolves the repo + the Python that has deps). |

## Cross-cutting library (`lib/py/`) — platform

| Module | Role |
|---|---|
| `config.py` | Paths & runtime dirs (`DATA_DIR`, `LIB_PY`, `REPO_ROOT`), `AUTO_DATA_DIR` override for tests. |
| `secrets_store.py` | Read secrets from macOS Keychain (service `my-automations`) or env. Stdlib-shadow-safe name (not `secrets`). |
| `scrub.py` | Redact secrets/PII from any outbound text (`scrub.clean`); register sensitive values. |
| `runlog.py` | Structured per-run logging + `read_recent()` for status/history. |
| `notify.py` | Telegram send helper used by automations. |
| `manifest.py` | Parse/validate `automation.yaml`; recursive discovery (`discover()`). |
| `deploy.py` | Emit launchd plists / GitHub Actions YAML from a manifest's trigger + schedule. |
| `service.py` | `launchctl` wrappers (bootstrap/kickstart/stop) for managed services. |
| `skillgen.py` | Generate `/<id>` Claude Code skills from `skill`-trigger automations. |

## Finance library (`lib/py/`) — the intelligence system

| Module | Role |
|---|---|
| `store.py` | **The only SQL.** SQLite schema + all reads/writes: accounts, transactions, balances, holdings, labels, `learned_categories`, **`txn_flags`** (one-time/exclude), splitwise, market prices/macro/news, snapshots, insights, `chat_messages`, `balance_overrides`. Key helpers: `latest_balances` (effective values), `latest_holdings` (drops stale/0-share positions), `merchant_key` (strips processor noise), `data_freshness`, `set_txn_flags`, `txn_flags`, `set_account_subtype`. |
| `simplefin.py` | SimpleFIN Bridge client (read-only): claim setup token → Access URL; fetch accounts/txns/balances/holdings (+ `org` institution). Treats 402/403 as events. |
| `splitwise.py` | Splitwise client — friend balances + your expense shares. |
| `marketdata.py` | Finnhub (prices) + FRED (macro) clients. |
| `transfers.py` | **Deterministic** internal-transfer detection: pair each debit to an equal-magnitude credit in another account within a few days. `return_pairs` lets callers label card-bill payments `Payment` vs generic `Transfer`. Excludes P2P reimbursements from matching. |
| `finance_rules.py` | Deterministic keyword categorization (`categorize`), the `NON_SPEND` set, and P2P helpers: `is_reimbursement` (inbound from a person) / `is_p2p_outflow`. |
| `account_classify.py` | Account **subtype** classifier: HYSA (behavioural APY + product/org list) vs savings; brokerage / retirement / equity-awards. Labels + icons for the UI. |
| `features.py` | **Deterministic analytics core / single source of truth.** `build()` computes the cash-honest cashflow waterfall (take-home − spend + net reimbursements = operating surplus; 401k/one-time/savings-transfers separate), net worth (effective values − debts + Splitwise receivable), allocation, concentration, emergency fund, `month_to_date`, monthly series — honoring `one_time`/`excluded` tags. `window_summary()` for exact N-day totals. |
| `integrity.py` | **Data-Health invariants.** `check()`/`summary()` enforce: net worth == assets − debts (+ receivable), assets ≥ net worth, no duplicate txns, deterministic duplicate-charge detection, stale/phantom holdings, stale feeds, balance↔txn reconciliation. Flags only. |
| `categorizer.py` | AI categorizer — Claude labels only `Other`/`Uncategorized` (never overrides manual/transfer/payroll). |
| `finance_ask.py` | NL Q&A over aggregates (Telegram text path / CLI), aggregates-only boundary. |
| `finance_chat.py` | **Conversational agent** (UI Ask AI + Telegram). Multi-turn; returns a strict JSON contract `{answer, charts, followups}` over the finance_cli tools. Charts are catalog ids or custom specs the UI/bot render. |
| `finance_viz.py` | Renders a chart spec → PNG (matplotlib) for Telegram (the UI uses Plotly inline). |
| `council.py` / `council_cli.py` | 8-investor parallel debate + fiduciary mediator over the anonymized portfolio + market brief; single-expert mode. |
| `market_cli.py` | Assemble the market brief (`_brief`) and store news; tickers/benchmarks. |
| `finance_cli.py` | The **scoped toolset** a Claude run may call: `features · window · accounts · transactions · store-insight · notify · setup-simplefin`. Defines the data-egress surface (per-automation `allowed_tools`). |
| `telegram_setup.py` | Store Telegram token/chat-id to Keychain without printing. |

## UI (`ui/app.py`)

Streamlit dashboard (Plotly), binds `127.0.0.1` only — a view layer over the same
lib. Pages: **Overview** (reconciling cash-flow waterfall, net worth, allocation),
**Spending** (editable categories + one-time/exclude tags, search), **Accounts**
(by institution, subtypes, effective values + Splitwise receivable), **Investments**
(holdings, gain/loss, concentration, search), **Market** (your holdings vs
benchmarks, positions, searchable news), **Ask AI** (chat + auto-charts + persistent
history), **Data Health** (integrity invariants), **AI Analysis**, **Council**, plus
**Automations**/**Settings**. Global sidebar: data-freshness indicator + refresh.

## Automations (`automations/`)

| id | category | runtime | role |
|---|---|---|---|
| `finance-sync` | finance | python | SimpleFIN ETL → SQLite; transfer/reimbursement detection; subtypes; rule labels |
| `splitwise-sync` | finance | python | Splitwise balances + expense shares |
| `finance-categorize` | finance | claude | AI labels remaining `Other` |
| `market-data-sync` | finance | python | Finnhub prices + FRED macro |
| `market-watch` | finance | claude | Web → sourced market news brief |
| `finance-summary` | finance | python | point-in-time snapshot → Telegram + UI JSON |
| `finance-weekly` | finance | python | last-7-days digest → Telegram |
| `finance-analyze` | finance | claude | multi-lens investing analysis (aggregates only) |
| `finance-council` | finance | claude | 8-investor debate + mediator |
| `telegram-bot` | system | python | always-on dispatcher (KeepAlive service) |

Each automation is `automations/<category>/<id>/automation.yaml` + an entry point.
Adding one = a new folder + manifest; discovery is recursive, no central wiring.
