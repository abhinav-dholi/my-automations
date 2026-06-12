# my-automations

Personal task automations & integrations. Each automation is a self-contained
folder with a manifest; run it on a schedule (cloud), on demand from the CLI,
or as a Claude Code skill. See [PRD.md](PRD.md) for the full design.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Secret scanner (recommended)
pip install pre-commit && pre-commit install
```

## Usage

```bash
python cli/auto list [--category C]      # list automations, grouped by category
python cli/auto status [--category C]    # service state (running/stopped) + last run
python cli/auto run <id>                 # run one now (resolves secrets, logs it)
python cli/auto start|stop|restart <id|category|all>   # manage launchd services
python cli/auto logs <id> [--lines N]    # tail service/schedule logs
python cli/auto ui                       # dashboard on 127.0.0.1:8501 (single instance)
python cli/auto skills                   # (re)generate /<id> Claude Code skills
python cli/auto update [--category C]    # apply latest code: pull + skills + restart services
python cli/auto deploy <id>              # (advanced) just generate the schedule trigger
```

### Services & scheduling
`auto start <id|category|all>` installs a macOS launchd job (scheduled jobs use
local time and run missed jobs on wake; `service`-trigger automations run as
KeepAlive daemons). Manage with `start`/`stop`/`restart`/`status`/`logs`.
`github-actions`-target automations instead emit `.github/workflows/<id>.yml`
(cron converted to UTC; add the listed secrets in repo Settings → Actions).

### Skills (`auto skills`)
Automations with a `skill` trigger become `/<id>` slash commands in Claude Code (written to `.claude/skills/`). `runtime: claude` automations orchestrate in-session; code automations run via `auto run`.

### Dashboard (`auto ui`)
Streamlit app (binds `127.0.0.1` only): automations table + run-now, run history, finance charts, and a safe manifest editor (never edits secret values).

## Secrets

Manifests declare secret **names** only. Values live in:

- **Local:** macOS Keychain, under service `my-automations`:
  ```bash
  security add-generic-password -s my-automations -a TELEGRAM_TOKEN -w
  # (omit the value to be prompted — keeps it out of shell history)
  ```
- **Cloud (GitHub Actions):** repo Settings → Secrets and variables → Actions,
  named identically.

Nothing sensitive is committed. See PRD §11 for the full security model.

## Automations

### finance-weekly
Weekly spend + balance digest from [SimpleFIN Bridge](https://www.simplefin.org)
($15/yr, read-only) → Telegram.

One-time setup:
1. Get a SimpleFIN **setup token** from the Bridge.
2. Claim it into an Access URL:
   ```bash
   python -c "import sys; sys.path.insert(0,'lib/py'); \
     import simplefin; print(simplefin.claim_access_url('<SETUP_TOKEN>'))"
   ```
3. Store it:
   ```bash
   security add-generic-password -s my-automations -a SIMPLEFIN_ACCESS_URL -w
   ```
4. Add `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` the same way, then:
   ```bash
   python cli/auto run finance-weekly
   ```

## Layout

```
automations/<category>/<id>/   automation.yaml + entry point  (e.g. automations/finance/finance-sync/)
lib/py/                        shared + domain helpers (secrets, logging, notify, manifest, finance clients)
cli/auto                       the runner + service manager
ui/                            Streamlit dashboard
```

Finance is the first category. New domains are new folders under `automations/<domain>/`.
The Telegram bot namespaces commands per domain (`/finance …`, plus global `/run`, `/status`, `/help`).
