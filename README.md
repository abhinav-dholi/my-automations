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
python cli/auto list              # list automations
python cli/auto run <id>          # run one now (resolves secrets, logs the run)
python cli/auto ui                # launch the Streamlit dashboard (localhost only)
python cli/auto skills            # (re)generate /<id> Claude Code skills from manifests
python cli/auto deploy <id>       # generate the schedule trigger (launchd or Actions)
```

### Scheduling (`auto deploy`)
- **local-cron** automations → a macOS launchd plist in `deploy/launchd/`. Install with the printed `launchctl bootstrap gui/$(id -u) …` commands. launchd uses local time and runs missed jobs on wake.
- **github-actions** automations → `.github/workflows/<id>.yml` with the cron converted to UTC (add the listed secrets in repo Settings → Actions).

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
automations/<id>/   automation.yaml + entry point
lib/py/             shared helpers (secrets, logging, notify, manifest, clients)
cli/auto            the runner
ui/                 Streamlit dashboard (Phase 5)
```
