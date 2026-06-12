"""Manage automations as macOS launchd jobs (start/stop/restart/status).

Wraps `launchctl` so the CLI can control scheduled jobs and always-on services
without the user typing bootstrap/bootout by hand.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import deploy

DOMAIN = f"gui/{os.getuid()}"
LAUNCHAGENTS = Path.home() / "Library" / "LaunchAgents"


def label(automation_id: str) -> str:
    return f"{deploy.LABEL_PREFIX}.{automation_id}"


def is_loaded(automation_id: str) -> bool:
    return subprocess.run(
        ["launchctl", "print", f"{DOMAIN}/{label(automation_id)}"],
        capture_output=True,
    ).returncode == 0


def pid(automation_id: str) -> str | None:
    out = subprocess.run(["launchctl", "list", label(automation_id)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if '"PID"' in line:
            return line.split("=")[-1].strip().strip(";").strip()
    return None


def install(m) -> tuple[bool, str]:
    """Generate the plist, drop it in LaunchAgents, and (re)bootstrap it."""
    lbl, xml, _ = deploy.launchd_plist(m)
    deploy.LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    (deploy.LAUNCHD_DIR / f"{lbl}.plist").write_text(xml)   # repo copy (gitignored)
    LAUNCHAGENTS.mkdir(parents=True, exist_ok=True)
    dest = LAUNCHAGENTS / f"{lbl}.plist"
    dest.write_text(xml)
    subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{lbl}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", DOMAIN, str(dest)],
                       capture_output=True, text=True)
    subprocess.run(["launchctl", "enable", f"{DOMAIN}/{lbl}"], capture_output=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def uninstall(automation_id: str) -> bool:
    lbl = label(automation_id)
    r = subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{lbl}"],
                       capture_output=True)
    dest = LAUNCHAGENTS / f"{lbl}.plist"
    if dest.exists():
        dest.unlink()
    return r.returncode == 0


def kickstart(automation_id: str) -> None:
    subprocess.run(["launchctl", "kickstart", f"{DOMAIN}/{label(automation_id)}"],
                   capture_output=True)
