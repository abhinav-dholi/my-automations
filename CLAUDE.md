# CLAUDE.md

Personal task-automation monorepo. Each automation is a self-contained folder
with a manifest; runs on a schedule (local-cron or GitHub Actions), on demand
via CLI, or as a Claude Code skill. Full design in [PRD.md](PRD.md).

## Commands

```bash
python cli/auto list          # list automations + triggers/schedules
python cli/auto run <id>      # run now (resolves secrets, logs the run)
python cli/auto ui            # Streamlit dashboard, 127.0.0.1 only
python cli/auto skills        # (re)generate /<id> Claude Code skills
python cli/auto deploy <id>   # generate launchd plist or Actions workflow
```

Deps: `pip install -r requirements.txt`. Secret scanner: `pre-commit install`.

## Architecture

- **`cli/auto`** — the single front door. Loads manifests, resolves declared
  secrets (one Keychain touch in the parent), injects them as env vars into the
  child, runs the entry, and wraps the run in structured logging.
- **`automations/<id>/automation.yaml`** — manifest: `id, description, runtime,
  entry, triggers[], schedule, tz, secrets[] (names only), allowed_tools[],
  deploy.target`. Adding an automation = new folder + manifest + entry; no
  central wiring.
- **`lib/py/`** — shared helpers, the ONLY place with cross-cutting logic:
  - `config` (paths; `AUTO_DATA_DIR` overrides the data root, used by tests)
  - `secrets_store` (Keychain local / env+Actions cloud; resolves by name)
  - `scrub` (redacts secret values from all logs/notifications)
  - `runlog` (one JSON line per run to `logs/runs.jsonl`)
  - `notify` (Telegram), `manifest` (load/validate), `deploy`, `skillgen`
  - finance: `store` (SQLite — the only SQL), `simplefin`, `finance_rules`,
    `features` (anonymized aggregates), `finance_cli` (the scoped finance tools)
- **`ui/app.py`** — Streamlit view layer over the same lib; no business logic.

## Runtimes

- `runtime: python | node` — deterministic single-step jobs (e.g. ETL). The
  runner executes the entry directly from the automation's folder.
- `runtime: claude` — **Claude orchestrates the run.** A thin trigger fires one
  headless `claude -p "<entry prompt>" --allowedTools "<manifest allowed_tools>"`.
  Requires an `allowed_tools` key (use `[]` to sandbox). Runs from repo root so
  tool paths resolve. Auth via `CLAUDE_CODE_OAUTH_TOKEN` (subscription) — no API
  key/Console billing.

## Conventions

- **Secrets are names, never values.** Declare names in the manifest; store
  values in Keychain (`security add-generic-password -s my-automations -a NAME -w`)
  locally or Actions Secrets in cloud. Never log/print values — route outbound
  text through `scrub.clean`.
- **Don't import stdlib-shadowing names** — the secrets module is
  `secrets_store`, not `secrets`.
- New shared finance SQL goes in `store.py` only.
- Generated artifacts (`.claude/skills/`, `deploy/launchd/`) are gitignored —
  regenerate with `auto skills` / `auto deploy`, don't commit them.

## Finance subsystem (local-only, never cloud)

- Data source: **SimpleFIN Bridge** (read-only, $15/yr). `SIMPLEFIN_ACCESS_URL`
  is the one credential.
- `finance-sync` (ETL) → SQLite `data/finance.db` (gitignored, immutable raw +
  layered labels + time-series balances/holdings). `finance-weekly` reads the DB
  for a Telegram digest.
- `finance-analyze` (`runtime: claude`) orchestrates a multi-lens investing
  analysis. **Privacy boundary (do not weaken):** Claude's `allowed_tools` are
  scoped to `finance_cli.py features|store-insight|notify` only — `features`
  emits **aggregates only**, so raw transactions never enter Claude's context.
  Never grant raw DB/query tools to a Claude-orchestrated finance run.
- Not licensed financial advice — analysis carries that disclaimer.

## Security

See PRD §11. Mandatory: scrub secrets from output; HTTPS + TLS verify; treat
`401/403` as a security event (stop+notify, don't retry); gitleaks pre-commit;
no raw financial data in the repo tree (lives in gitignored `data/`).

## Testing

No formal suite yet. Validate logic with throwaway scripts that set
`AUTO_DATA_DIR` to a temp dir and monkeypatch `simplefin.get_accounts` /
`secrets_store.get` (see prior usage). The runner accepts `AUTO_CLAUDE_BIN` to
stub the `claude` binary for dispatch tests.
