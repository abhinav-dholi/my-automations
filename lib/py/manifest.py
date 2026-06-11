"""Load and validate automation.yaml manifests."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

import config

VALID_RUNTIMES = {"python", "node", "claude"}
VALID_TRIGGERS = {"cli", "schedule", "skill", "webhook", "service"}
VALID_DEPLOY = {"github-actions", "local-cron", "none"}


class ManifestError(Exception):
    pass


@dataclass
class Manifest:
    id: str
    description: str
    runtime: str
    entry: str
    triggers: list[str]
    schedule: str | None
    tz: str | None
    secrets: list[str]
    deploy_target: str
    allowed_tools: list[str]
    dir: Path

    @property
    def entry_path(self) -> Path:
        return self.dir / self.entry


def _require(data: dict, key: str, path: Path):
    if key not in data or data[key] in (None, ""):
        raise ManifestError(f"{path}: missing required field '{key}'")
    return data[key]


def load(manifest_path: Path) -> Manifest:
    data = yaml.safe_load(manifest_path.read_text()) or {}
    d = manifest_path.parent

    runtime = _require(data, "runtime", manifest_path)
    if runtime not in VALID_RUNTIMES:
        raise ManifestError(f"{manifest_path}: runtime must be one of {VALID_RUNTIMES}")

    triggers = data.get("triggers") or []
    bad = set(triggers) - VALID_TRIGGERS
    if bad:
        raise ManifestError(f"{manifest_path}: invalid triggers {bad}")

    deploy = (data.get("deploy") or {}).get("target", "none")
    if deploy not in VALID_DEPLOY:
        raise ManifestError(f"{manifest_path}: deploy.target must be one of {VALID_DEPLOY}")

    if "schedule" in triggers and not data.get("schedule"):
        raise ManifestError(f"{manifest_path}: 'schedule' trigger requires a 'schedule' cron")

    allowed_tools = data.get("allowed_tools") or []
    if runtime == "claude" and "allowed_tools" not in data:
        raise ManifestError(
            f"{manifest_path}: runtime 'claude' requires an 'allowed_tools' key "
            f"(use [] explicitly to sandbox with no tools)"
        )

    return Manifest(
        id=_require(data, "id", manifest_path),
        description=data.get("description", ""),
        runtime=runtime,
        entry=_require(data, "entry", manifest_path),
        triggers=triggers,
        schedule=data.get("schedule"),
        tz=data.get("tz"),
        secrets=data.get("secrets") or [],
        deploy_target=deploy,
        allowed_tools=allowed_tools,
        dir=d,
    )


def discover() -> list[Manifest]:
    """Load every automations/*/automation.yaml (sorted by id)."""
    out = []
    for path in sorted(config.AUTOMATIONS_DIR.glob("*/automation.yaml")):
        out.append(load(path))
    return sorted(out, key=lambda m: m.id)


def get(automation_id: str) -> Manifest:
    for m in discover():
        if m.id == automation_id:
            return m
    raise ManifestError(f"No automation with id '{automation_id}'")
