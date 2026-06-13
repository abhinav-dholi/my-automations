# PRD: my-automations

**Owner:** Abhinav Dholi (dholi.a@northeastern.edu)
**Status:** Draft v1
**Last updated:** 2026-06-11

## 1. Summary

A personal monorepo for building, running, and maintaining small task automations and service integrations. Each automation is a self-contained unit that can run on a schedule, on demand from the CLI, or as a Claude Code skill. The repo prioritizes low friction: adding a new automation should take minutes, and the same automation should run identically on a local Mac and in the cloud.

## 2. Goals

- **Fast to add:** A new automation is a new folder + a manifest. No central wiring to edit.
- **Run anywhere:** Identical behavior locally and in cloud (GitHub Actions / serverless / VPS).
- **Three trigger modes:** scheduled (cron), manual (CLI), and Claude Code skill — any automation can support one or more.
- **Safe secrets:** Credentials pulled at runtime from macOS Keychain (local) / GitHub Actions Secrets (cloud); nothing sensitive ever committed.
- **Mixed stack:** Python or TypeScript per automation, chosen by best fit, behind a common run contract.
- **Observable:** Every run logs start/end/status; failures are surfaced (notification channel).
- **Local UI:** A Streamlit dashboard to view runs, trigger automations, see finance charts, and edit config — local-first, designed to deploy later.

## 3. Non-goals (v1)

- No multi-user / shared infra. Single user (me).
- No real-time/sub-minute event processing. Webhooks deferred to a later phase.
- No heavy orchestration engine (Airflow/Temporal). Keep it lightweight.
- UI is local-only in v1 (no authenticated public deployment yet — that's a later phase).

## 4. Users & primary use cases

Single user. First automations target **home/life ops** and **personal productivity**:

- Daily/weekly digest (calendar + tasks + finance summary) pushed to a messaging channel.
- Email triage / inbox summary from Gmail.
- Task sync between Notion / Todoist / Obsidian.
- Finance: pull transactions (bank/Plaid/Splitwise), categorize, weekly budget report.
- File / note organization and backups.

## 5. Integrations (target)

| Service | Use | Auth |
|---|---|---|
| Google (Calendar, Gmail, Drive) | events, email, docs/sheets | OAuth, token in 1Password |
| Notion / Todoist / Obsidian | tasks, notes, KB | API token (Notion/Todoist), local vault (Obsidian) |
| Slack / Discord / Telegram | notifications, digests, bot replies | bot token / webhook URL |
| Finance (bank / Plaid / Splitwise) | transactions, budgets, splits | API keys / Plaid tokens |

## 6. Architecture

### 6.1 Repo layout

```
my-automations/
  automations/
    <name>/
      automation.yaml      # manifest: id, runtime, triggers, schedule, entry
      main.py | index.ts   # entry point
      README.md
      (deps: requirements.txt | package.json)
  lib/
    py/                    # shared Python helpers (secrets, logging, notify, integration clients)
    ts/                    # shared TS helpers
  skills/                  # generated/symlinked Claude Code skills per automation
  .github/workflows/       # scheduled cloud runs
  cli/                     # `auto` runner (list/run/deploy)
  ui/                      # Streamlit dashboard (reads manifests + run logs via shared lib)
  PRD.md
```

### 6.2 Automation manifest (`automation.yaml`)

```yaml
id: daily-digest
description: Calendar + tasks + finance summary to Telegram each morning
runtime: python          # python | node
entry: main.py
triggers:
  - cli                   # runnable on demand
  - schedule
  - skill                 # exposed as Claude Code /daily-digest
schedule: "0 7 * * *"     # cron in LOCAL time
tz: America/New_York      # auto deploy converts schedule -> UTC cron for Actions
secrets:                  # secret NAMES only (resolved from Keychain/Actions)
  - GOOGLE_OAUTH_TOKEN
  - TELEGRAM_TOKEN
deploy:
  target: github-actions  # github-actions | local-cron | none
```

### 6.3 Run contract

Every automation exposes one entry that:
1. Reads config + resolves declared secrets via 1Password CLI (`op read`).
2. Does its work.
3. Returns exit code 0 (success) / non-zero (fail), and emits a structured log line (JSON) for the run.

The `cli/auto` runner is the single front door:

- `auto list` — list automations + their triggers/schedules.
- `auto run <id>` — run locally now (resolves secrets, picks runtime).
- `auto deploy <id>` — generate/update the cloud trigger (Actions workflow).
- `auto skills` — (re)generate Claude Code skill wrappers from manifests.

### 6.4 Triggers

- **CLI:** `auto run <id>` — always available.
- **Scheduled:** cloud via generated GitHub Actions workflow (cron, UTC — converted from manifest `schedule` + `tz`) when `deploy.target: github-actions`; local via `launchd`/cron calling `auto run` when `deploy.target: local-cron` (e.g. Obsidian/local-file automations).
- **Claude Code skill:** `auto skills` reads manifests with `skill` trigger and writes a skill in `skills/<id>/` that shells out to `auto run <id>`.
- **Webhooks/events:** out of scope v1, reserved in manifest schema for later.

### 6.5 Secrets — macOS Keychain (local) + GitHub Actions Secrets (cloud)

Free, no third-party subscription. Manifests declare secret **names** only — never values or refs. The runner resolves them per environment behind one `secrets.get(name)` helper:

- **Local:** macOS Keychain. `security find-generic-password -s my-automations -a <NAME> -w`.
- **Cloud:** GitHub Actions Secrets, injected as env vars; helper falls back to `os.environ[NAME]`.

The helper picks the backend by environment (e.g. `CI`/`GITHUB_ACTIONS` env var present → cloud path, else Keychain). Resolved values are injected as env vars to the child process; nothing sensitive is committed. `.gitignore` covers `.env`, tokens, `*.key`.

One-time key setup:
- Local: `security add-generic-password -s my-automations -a TELEGRAM_TOKEN -w '<value>'`
- Cloud: add `TELEGRAM_TOKEN` under repo Settings → Secrets and variables → Actions.

Tradeoff: two stores to keep in sync on rotation. Accepted for $0 cost and zero extra tooling. (SOPS+age — one committed encrypted file, single key — is the upgrade path if syncing two stores becomes painful.)

### 6.6 Local vs cloud parity

- Same entry point + same secret **names** both places; only the resolution backend and trigger source differ.
- Runtime deps pinned per automation (`requirements.txt` / `package.json`) so the Actions image matches local.

## 7. Observability

- Each run writes a JSON log line: `{id, trigger, start, end, status, error?}`.
- On failure: send a message to **Telegram** (channel of record) via shared `notify` helper. Slack/Discord pluggable later as additional backends.
- Cloud runs additionally surface in the Actions run log.

## 8. Phases

**Phase 0 — Skeleton**
- Repo layout, `cli/auto` runner (list/run), manifest schema + validation, shared `secrets` (Keychain/Actions) + `logging` + `notify` helpers, `.gitignore`.

**Phase 1 — First automation end to end**
- One real automation (proposed: `daily-digest`) running locally via `auto run`, secrets from Keychain, notify on Telegram.

**Phase 2 — Scheduling + cloud**
- `auto deploy` generates a GitHub Actions cron workflow; required secrets added as Actions Secrets; daily-digest runs in cloud.

**Phase 3 — Claude Code skills**
- `auto skills` generates skill wrappers; daily-digest invokable as a slash command.

**Phase 4 — Breadth**
- Add finance, email triage, task sync automations. Harden error handling/retries.

**Phase 5 — UI (Streamlit, local)**
- Local Streamlit dashboard: list automations + run status/history, trigger an automation (shells to `auto run`), finance charts from `finance-weekly` output, edit manifest fields/schedules. See §13.

**Phase 6 (later) — Webhooks/events + UI deploy**
- Lightweight HTTP receiver for event-driven automations. Authenticated public UI deploy if desired.

## 9. Decisions (resolved)

Optimized for least infra / most coverage ("best bang for buck"):

- **Cloud target → GitHub Actions only.** Free, no servers, git-native, secrets built in. Cron is best-effort (5–15 min delay) — fine for life-ops jobs. No VPS/serverless until an automation genuinely needs sub-minute or always-on; revisit then.
- **Timezone → store cron in UTC, declare local tz in manifest.** Add a `tz` field (e.g. `tz: America/New_York`); `auto deploy` computes the UTC cron from local time + tz so schedules fire at the intended local hour.
- **Obsidian → local-only, never cloud.** Vault is local files; git-syncing it for cloud runs is merge pain for little gain. Obsidian-touching automations set `deploy.target: local-cron` and run on the Mac.
- **Secrets → macOS Keychain (local) + GitHub Actions Secrets (cloud).** No subscription (dropped 1Password). Manifests hold secret names only. SOPS+age is the upgrade path if two-store sync gets painful. See §6.5.
- **Failure / notify channel → Telegram.** Free, one bot token, no workspace setup, instant push to phone. Slack/Discord can be added later as additional `notify` backends if needed.
- **Integration clients → shared lib, extracted lazily.** Auth/token-refresh for each service lives in `lib/` and is reused — but extract to shared only when a *second* automation needs the same service. No speculative client layer up front.

## 9a. Open questions (deferred)

- Webhook receiver shape (Phase 5) — serverless function vs tiny self-hosted endpoint.
- Retry/backoff policy for flaky integrations (decide during Phase 4 hardening).

## 10. Success criteria

- Adding a new automation = create folder + manifest + entry; `auto run` works with zero central edits.
- `daily-digest` runs reliably on schedule in cloud and on demand locally and as a skill.
- No secret ever committed; rotating a key = update Keychain (local) + Actions Secret (cloud).

## 11. Security requirements

Secret stores (Keychain, Actions Secrets) are only as safe as how the runner handles resolved values. These are mandatory, not optional, and built into Phase 0.

### 11.1 Secret handling
- **Names, not values, in code/manifests.** Manifests declare secret names only. Values exist only at runtime in process memory + env.
- **Resolve via `secrets.get(name)`; inject as env vars to the child process** — never as command-line args (args leak via `ps`/shell history).
- **Never log a secret value.** Logging may reference names only. The `notify`/`logging` helpers run all outbound strings through a **scrubber** that redacts any known secret value (and high-entropy token-shaped substrings) before write/send. Error messages are a primary leak vector — scrub `error` fields too (per §7 JSON log).
- **Keychain writes without shell history exposure:** document `security add-generic-password` with an interactive prompt or a leading-space command; never paste live tokens into committed files.

### 11.2 Repo / commit hygiene
- **`.gitignore`** covers `.env`, `*.key`, `*.pem`, `token*`, `*.credentials`, `secrets.*`, local cache/state files.
- **Pre-commit secret scanner** (`gitleaks`) as a git hook — blocks commits containing token-shaped strings. Single highest-value safeguard. Also run `gitleaks` in CI as a backstop.
- **No secrets in generated artifacts.** `auto deploy` emits `${{ secrets.NAME }}` references in Actions YAML, never literal values. Generated skill wrappers shell out to `auto run` — they hold no secrets.

### 11.3 Token scope & blast radius
- **Least privilege per token:** Telegram bot scoped to one chat; Google OAuth minimal scopes (read-only where possible); SimpleFIN is read-only by design; Splitwise minimal scope.
- **Read-only by default** for finance/data sources — no write/transfer scopes unless an automation truly needs them.
- **Rotation plan:** document the two-store rotation (Keychain + Actions Secret) per secret; on suspected leak, revoke at the provider first, then rotate.

### 11.4 Transport & runtime
- **HTTPS only**; verify TLS certs on every outbound call (no cert/host verification disabled).
- **Handle auth-failure signals:** treat `401`/`403` (e.g. SimpleFIN `403` = compromised token) as a security event — stop, notify, do not retry blindly.
- **Pin dependencies** per automation (`requirements.txt`/`package.json` with lockfile) so cloud image matches local and supply-chain drift is visible. Optionally enable Dependabot.
- **Local file state** (caches, last-run cursors) lives in gitignored paths; never write fetched financial data into the repo tree.

### 11.5 Financial data storage (revised — persistent local store)

The finance-analysis subsystem (§14–15) requires keeping a labeled history of financial data. This supersedes the earlier "discard raw pulls" stance with explicit local-only controls:

- **Local-only, never cloud.** The finance datastore (`data/finance.db`) lives on the Mac, is gitignored, and is never synced to GitHub/Actions or any cloud. Finance-sync/analysis run with `deploy.target: local-cron`.
- **Encryption at rest.** Rely on macOS FileVault (full-disk encryption — verify it's on). SQLCipher is the upgrade path if a stronger boundary is wanted; the DB path/API is designed so swapping the driver is contained.
- **Raw kept immutable, labels separate.** Raw transactions stored once; all categorization/enrichment lives in separate label tables with a `label_source` (rule/manual/agent) + confidence, so re-labeling never destroys source data (§14).
- **Anonymized egress only.** What leaves the machine to Claude is aggregated *feature packs* (category totals, ratios, allocation %) — never account numbers, raw merchant descriptions, or identifiers (§15.3).
- **Fetch minimally** (SimpleFIN date windows, Google minimal scopes); cache market data, don't re-pull.

## 12. Finance management (first automation)

### 12.1 Data source decision — SimpleFIN Bridge

Cheapest correct option for personal read-only finance aggregation:

| Option | Cost | API | Verdict |
|---|---|---|---|
| **RocketMoney** | — | **No public API** (CSV export only) | Rejected — not automatable |
| **Plaid** | Free sandbox + 200 calls + free Trial plan (≤10 items, accts created after 2026-04-15); then pay-per-call (~$0.10–0.60), sales-led | OAuth + Link + webhooks | Overkill — built for fintech products; cost drifts up |
| **SimpleFIN Bridge** | **$15/year flat** | Token claim → Access URL w/ Basic Auth → REST | **Chosen** — read-only, daily refresh, ~16k US institutions (MX-backed), trivial to code |

Real-time / write access is a non-goal for life-ops, so SimpleFIN's daily-refresh / read-only model is sufficient. Plaid reserved as upgrade path only if those needs appear.

### 12.2 SimpleFIN mechanics
1. **One-time:** obtain a setup token from SimpleFIN Bridge; Base64-decode → claim URL; `POST` claim URL → returns an **Access URL** containing `https://user:pass@host/...` (HTTP Basic auth embedded).
2. **Store** the Access URL as secret `SIMPLEFIN_ACCESS_URL` (Keychain local / Actions Secret cloud). It is the single credential.
3. **Fetch:** `GET {ACCESS_URL}/accounts?start-date=<unix>&end-date=<unix>` → JSON `AccountSet`:
   - `accounts[]`: `id, name, currency, balance, available-balance, balance-date, transactions[]`
   - `transactions[]`: `id, posted (unix), amount (numeric string), description, pending?`
   - `errlist[]`: structured `{code, msg}` — handle, don't ignore.
4. **Security:** `403` ⇒ token compromised → stop + notify (per §11.4). HTTPS + TLS verify. Scrub Access URL (contains creds) from all logs/notifications.

### 12.3 First finance automation — `finance-weekly`
- **Trigger:** `cli`, `schedule` (weekly), `skill`.
- **Does:** pull last 7 days of transactions across accounts, categorize (simple rules first), compute spend by category + balances, format a digest, send to Telegram.
- **Deploy target:** `github-actions` (no local-file dependency).
- **State:** optional gitignored local cache of last-seen transaction ids to avoid double-counting; never commit financial data.
- **Secrets:** `SIMPLEFIN_ACCESS_URL`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.

## 13. UI — Streamlit dashboard

Local-first control panel over the same building blocks the CLI uses. Cheapest path: Streamlit (Python, minimal frontend code), $0 to run on localhost.

### 13.1 Features (v1)
- **Automations view:** table of all automations from manifests — id, triggers, schedule, deploy target, last-run status/time (from run logs §7).
- **Trigger:** a "Run now" button per automation → invokes `auto run <id>` as a subprocess, streams output/exit status back into the page.
- **Runs & logs:** history of recent runs (parse the JSON run-log lines), filter by automation, view errors.
- **Finance dashboard:** charts from `finance-weekly` output — spend by category, balances over time, weekly trend. Reads cached/summary data, not raw Keychain pulls.
- **Edit config/schedules:** form to edit safe manifest fields (description, schedule, tz, triggers, deploy target) with validation against the manifest schema; writes back the YAML. **Does not edit secret values** — only references secret names. Secret values are managed in Keychain/Actions, never typed into the UI.

### 13.2 Architecture
- `ui/app.py` Streamlit app. Reuses `lib/py` (manifest loader, run-log reader, `auto` runner invocation) — the UI is a thin view layer, no business logic duplicated.
- Run: `streamlit run ui/app.py` (add `auto ui` convenience command).
- Reads run logs + manifests from disk; triggers via the existing `auto run`. No new backend service.

### 13.3 Security (UI-specific)
- **Localhost bind only** in v1 (`--server.address 127.0.0.1`); not exposed to the network.
- **Never render secret values.** UI shows secret *names*/presence only. The §11.1 scrubber applies to any log/finance output displayed.
- **Finance data shown in UI** comes from local summaries; raw financial pulls are not persisted to repo (per §11.5).
- **Deploy-later caveat:** a public deploy (Phase 6) requires auth (password/OAuth) + moving secrets to the host's secret store + HTTPS. Explicitly out of scope until then because exposing finance data demands real authn/z.

## 14. Finance data layer (storage + labeling)

Foundation for the analysis agents. A single local SQLite database (`data/finance.db`, gitignored, FileVault-encrypted at rest). Raw data is immutable; enrichment/labels are layered on top so we can re-label freely as rules and agents improve.

### 14.1 Design principles
- **Immutable raw + layered labels.** Transactions/holdings are written once. Categories, tags, and agent enrichments live in separate label tables keyed by source + timestamp + confidence. Multiple labelers (rules, manual, agent) coexist; latest-per-source wins; history is never overwritten.
- **Time-series, not snapshots.** Balances and holdings are recorded with `as_of` so trends over time are queryable (net worth, allocation drift, savings rate).
- **Provider-agnostic store.** `lib/py/store.py` is the only module that touches SQL; sources (SimpleFIN, manual, market data) write through it. Swapping SQLite→SQLCipher or adding Plaid touches only ingest + store.
- **Idempotent ingest.** Re-running sync never duplicates: transactions keyed by provider id (`INSERT OR IGNORE`), balances/holdings keyed by `(account, as_of)`.

### 14.2 Schema (tables)
- `accounts` — id, name, type (checking/savings/credit/investment/loan), institution, currency, source, first_seen, last_synced.
- `balances` — account_id, balance, available, as_of (PK: account_id+as_of). Time series.
- `transactions` — id (provider), account_id, posted (unix), amount, description, pending, source, raw_json, ingested_at. **Immutable.**
- `transaction_labels` — txn_id, category, subcategory, tags (json), label_source (rule/manual/agent), confidence, model, labeled_at (PK: txn_id+label_source).
- `holdings` — account_id, symbol, name, quantity, cost_basis, market_value, asset_class, as_of, source (PK: account_id+symbol+as_of). Time series.
- `market_prices` — symbol, date, close (PK: symbol+date). Cached public market data.
- `sync_runs` — id, source, started, ended, status, n_accounts, n_transactions, note. Ingest audit.
- `insights` — id, agent, generated_at, model, input_hash, output_json. Stored agent outputs (also labeled data).

### 14.3 Ingest pipeline
- **`finance-sync`** (automation, `local-cron`, daily): pull accounts + transactions + holdings from SimpleFIN → upsert via `store` → apply rule-based labels (source=rule) → record `sync_runs`. Manual holdings read from a gitignored `data/holdings_manual.yaml`. Market prices fetched/cached for held symbols.
- **`finance-weekly`** (digest): reads the DB (not live), summarizes last 7d, notifies Telegram.
- Labeling order: deterministic rules first; the **labeler agent** (§15) refines low-confidence/`Other` transactions, writing source=agent labels without touching rule labels.

## 15. Finance analysis agents (hybrid)

A local orchestrator runs multiple specialized agents over the data to advise where/how to invest. **Hybrid**: all raw-data crunching is deterministic local SQL; only anonymized *feature packs* go to the Claude API for reasoning. Lives under `automations/finance-agents/` (`local-cron`, e.g. weekly/monthly).

> **Not financial advice.** Outputs are informational analysis from your own data, not licensed investment advice. Every report carries this disclaimer.

### 15.1 Pipeline
1. **Feature builder (local, deterministic):** query the DB → build aggregated feature packs: monthly income/spend, savings rate, spend-by-category trend, emergency-fund months, current allocation by asset class, concentration, cost basis vs market value, cash drag. No PII, no raw descriptions, no account numbers.
2. **Agent fan-out (Claude API):** independent agents reason over the feature packs:
   - **Cashflow/Capacity** — sustainable monthly investable amount.
   - **Allocation** — current vs target allocation, diversification, concentration risk.
   - **Risk/Resilience** — emergency fund, debt load, liquidity.
   - **Opportunity/Rebalance** — concrete moves given allocation + market data + a stated risk profile.
3. **Synthesizer (Claude API):** merges agent outputs into one ranked, actionable report + disclaimer.
4. **Persist + notify:** store in `insights`, write report to `data/`, summary to Telegram / surface in UI.

### 15.2 Orchestration — Claude orchestrates the run

Decision: **Claude orchestrates the work inside each run**, not a deterministic python pipeline. A thin trigger (cron/`auto run`) fires ONE Claude Code headless invocation; Claude then drives the analysis — pulls feature packs, decides/sequences the lenses, synthesizes, writes results — using a tightly scoped tool set. (Claude Code, not the API SDK: data goes to Anthropic either way, so the SDK buys nothing on privacy, while Claude Code reuses subscription auth + the skill/local-cron triggers + built-in orchestration.)

- **`finance-analyze` automation, `runtime: claude`, `local-cron`:** `entry` is a prompt (orchestration spec); the runner invokes `claude -p "<prompt>" --allowedTools "<scoped>" --output-format json`, with secrets injected. Claude orchestrates: call the `features` tool → reason across lenses (cashflow, allocation, risk, opportunity) → synthesize a ranked report → call `store-insight` and `notify` tools.
- **Privacy is enforced by tool scope (critical).** Because Claude runs via Anthropic, anything in its context leaves the box. So Claude's data access is limited to the deterministic **`features` tool, which emits aggregates only** — Claude is *not* granted raw DB/query access. `--allowedTools` whitelists only `finance_cli.py features|store-insight|notify`; headless aborts on any unapproved tool, so the §15.3 boundary is mechanically enforced, not just convention.
- **Tools (python CLI Claude calls):** `features` (anonymized packs from DB + profile), `store-insight` (write to `insights`), `notify` (Telegram). Raw SQL stays inside `features`, never exposed.
- **Also a skill:** same flow as `/finance-analyze` interactive; scheduled run is `auto run finance-analyze` (→ `claude -p`).
- **Auth:** subscription via `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` in Keychain, injected by the runner. No `ANTHROPIC_API_KEY` / Console billing. (API key is a fallback only.)
- **Risk profile** (conservative/balanced/aggressive + target allocation) in `data/profile.yaml` (gitignored; `profile.example.yaml` committed). The `features` tool includes it so one tool call gives Claude everything.
- Sub-analysis fan-out is Claude's call (sequential subagents in-session). Native *parallel* agent-teams need the SDK — deferred, unneeded for monthly analysis.

### 15.2a `runtime: claude` (general automation model)

Generalizes beyond finance: an automation may be `runtime: code` (python/node — deterministic single-step jobs like ETL) or `runtime: claude` (Claude orchestrates the run). For `runtime: claude`, the manifest carries a prompt `entry` + `allowed_tools`; the runner resolves secrets, then runs `claude -p` with that prompt and whitelisted tools. This is how "Claude orchestrates the work in each run" is realized repo-wide while mechanical plumbing stays cheap deterministic code.

### 15.3 Privacy boundary (what leaves the machine)
- **Sent to Claude:** aggregated numbers and ratios only (e.g. "savings rate 22%", "equity 70% / bonds 20% / cash 10%", "top category Dining $640/mo", "VTI 45% of equity"). Symbols/asset classes are not PII.
- **Never sent:** account numbers, institution login data, raw transaction descriptions, the SimpleFIN Access URL, full transaction lists.
- **Credential:** `CLAUDE_CODE_OAUTH_TOKEN` (subscription, via `claude setup-token`) in Keychain — see §15.2. Anthropic does not train on this data by default — noted, but minimization (features-only) is the primary control.

### 15.4 Phasing
- **15a** Data layer + `finance-sync` + DB-backed `finance-weekly` (build now — provider-agnostic foundation).
- **15b** Feature builder + manual holdings + market-price cache.
- **15c** Agent orchestrator via Claude Code headless (`claude -p`) + synthesizer + `data/profile.yaml`; exposed as `/finance-analyze` skill. Subscription auth (`CLAUDE_CODE_OAUTH_TOKEN`).
- **15d** UI surfacing of insights/allocation (extends §13 finance dashboard).

## 16. Investment Council (multi-agent investor debate) — PLAN

A panel of agents, each embodying a top investing philosophy, debates over the
user's anonymized portfolio + a live market brief; a fiduciary **mediator**
synthesizes ranked, actionable advice. On-demand. **Not licensed advice** —
personas simulate *frameworks*, not real-time opinions of living people.

### 16.1 Decisions (locked)
- **Panel (8):** Value (Buffett/Graham), Passive/Index (Bogle), Macro/All-Weather
  (Dalio), Growth/GARP (Lynch), Risk & Cycles (Marks), Quant/Factor (Fama-French),
  Tail-risk/Antifragility (Taleb), Disruptive Growth (Cathie Wood).
- **Mediator:** fiduciary CFP — weighs all views against the user's profile
  (risk, horizon, goals, liquidity, emergency-fund status) and resolves conflicts.
- **Debate:** panel opinions → one cross-critique round → mediator synthesis.
- **Data:** hybrid — structured APIs (prices + macro) + Claude web tools (news).
- **Cadence:** on-demand (Telegram/UI/CLI). `market-watch` may run more often.

### 16.2 Components (all category `finance`)
- **`market-data-sync`** (python, scheduled/on-demand): prices for held tickers +
  benchmarks (SPY/QQQ/AGG/GLD) via a free API (Finnhub), macro series via FRED
  (Fed funds, CPI YoY, 10y yield, unemployment). Stores in `market_prices` (exists)
  + new `macro_series`. Secrets: `FINNHUB_API_KEY`, `FRED_API_KEY` (both free).
- **`market-watch`** (`runtime: claude`, WebSearch/WebFetch): gathers + summarizes
  market-moving news relevant to holdings + macro into `market_news` +
  `data/market-brief.json`, with cited sources.
- **`finance-council`** (`runtime: claude`, on-demand): the debate + mediation.
  Tools (scoped): `finance_cli features` (anonymized portfolio), a new
  `market_cli brief` (prices/macro/news brief), `store-insight`, `notify`.
  Prompt orchestrates: (1) load features + brief, (2) each of the 8 personas gives
  a recommendation through its lens citing data, (3) a cross-critique round where
  personas challenge each other, (4) mediator synthesis → ranked actions with
  confidence + noted dissent. Output → `insights` (agent=finance-council) +
  Telegram + UI.

### 16.3 Data model (new)
- `macro_series` — series, date, value (PK series+date).
- `market_news` — id, fetched_at, source, headline, summary, url, tickers, relevance.
- Reuse `market_prices` and `insights`.

### 16.4 Surfaces
- Telegram: `/finance council` (runs finance-council); `/finance brief` (latest market-watch).
- UI: new **Council** page — per-persona takes, the critique round, and the
  mediator's verdict + prioritized actions.
- CLI: `auto run market-data-sync | market-watch | finance-council`.

### 16.5 Privacy & guardrails
- Personas receive ONLY anonymized features (allocation %, tickers, cash, risk
  profile) + public market/news data — same boundary as §15. No raw transactions.
- Ground every claim in the brief or features; mediator flags uncertainty and
  cites sources; "not licensed financial advice" disclaimer on every output.
- News hallucination risk mitigated by fetch-then-reason (market-watch stores
  sourced facts; council reasons over the stored brief, not free recall).

### 16.6 Phasing
- **16a** Data: `macro_series` + `market_news` tables; `market-data-sync`
  (Finnhub + FRED); `market-watch` (web news → brief) + `market_cli brief` tool.
- **16b** `finance-council` automation (8 personas + critique + mediator).
- **16c** Telegram `/finance council` + `/finance brief`.
- **16d** UI Council page.

### 16.7 User setup & cost
- Free API keys in Keychain: `FINNHUB_API_KEY`, `FRED_API_KEY`.
- Cost: on-demand; a full 8-persona + critique + mediator run is the heaviest
  automation (~$0.50–$2/run depending on context). Kept on-demand by design.

## 17. Historical context & learning loop — PLAN

Goal: the system should remember and improve — each analysis/council run builds
on prior runs and on how the portfolio + market evolved, and we track whether
advice was acted on and how it played out.

### 17.1 What we already persist (foundation — keep)
- Time series: `market_prices`, `macro_series`, `balances`, `holdings` (per
  `as_of`), `market_news` (timestamped).
- `insights` — every finance-analyze / finance-council run as timestamped JSON.

### 17.2 Gaps to close
1. **Feature snapshots** — derived metrics aren't snapshotted, so trending recomputes
   from raw and we can't cheaply diff "then vs now."
2. **No continuity** — agents don't see the previous verdict or what changed, so each
   run starts cold instead of "since last time, X moved, here's the update."
3. **No follow-through tracking** — recommendations aren't tracked as
   open/done/dismissed, and outcomes (did the flagged risk materialize?) aren't recorded.

### 17.3 Plan
- **`snapshots` table** — `(id, kind, as_of, data_json)`. On each council/analyze
  run, snapshot the feature pack (net worth, allocation, cashflow, invested total,
  key macro). Enables clean trend charts + fast deltas.
- **`recommendations` table** — flatten each run's actions into rows
  `(id, run_id, source, action, rationale, priority, confidence, created_at,
  status[open|done|dismissed], resolved_at, note)`. User marks status in the UI /
  Telegram; carried into the next run.
- **Continuity injection** — a `_history()` context block fed to council/analyze:
  prior verdict + summary, portfolio delta since last snapshot (net worth,
  allocation drift, savings-rate change), notable market moves since, and the list
  of still-open recommendations. Agents must explicitly note "what changed."
- **Outcome review (later)** — a periodic pass that checks whether flagged risks
  played out (e.g. did META dividend pay / dilution happen) and whether open
  recommendations are now moot, surfacing a short "learning" note.

### 17.4 Bias / informational stance (council refinement)
- Individual personas stay opinionated (diversity = the value); the **mediator**
  must be unbiased: present consensus, the genuine tradeoffs (both sides + what
  would tip the call), conditions under which each camp is right, and what to
  watch — then a balanced synthesis tied to the user's profile, explicitly framed
  as one informed option, not dogma. Output gains `consensus`, `tradeoffs`,
  `watch_items` alongside ranked actions.

### 17.5 Phasing
- **17a** `snapshots` table + capture on each run; `_history()` continuity block
  into council + analyze; mediator made unbiased/informational. (build first)
- **17b** `recommendations` table + status tracking (UI/Telegram mark done).
- **17c** outcome-review pass + a History/Trends UI page (net worth, savings rate,
  allocation drift, verdict-over-time).
