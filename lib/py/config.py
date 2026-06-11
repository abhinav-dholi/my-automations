"""Shared paths and repo-root resolution for all automations."""
from __future__ import annotations

import os
from pathlib import Path

# lib/py/config.py -> repo root is three levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]

AUTOMATIONS_DIR = REPO_ROOT / "automations"
LIB_PY = REPO_ROOT / "lib" / "py"
SKILLS_DIR = REPO_ROOT / "skills"

# Gitignored local state. AUTO_DATA_DIR overrides the finance/data location
# (used for tests and alternate data roots); logs follow it.
DATA_DIR = Path(os.environ["AUTO_DATA_DIR"]) if os.environ.get("AUTO_DATA_DIR") else REPO_ROOT / "data"
LOGS_DIR = DATA_DIR / "logs" if os.environ.get("AUTO_DATA_DIR") else REPO_ROOT / "logs"


def in_cloud() -> bool:
    """True when running in GitHub Actions (or any CI)."""
    return bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))


def ensure_runtime_dirs() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
