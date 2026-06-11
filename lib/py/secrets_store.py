"""Resolve secrets by NAME from the right backend.

- Cloud (GitHub Actions) or any preset env var -> read from os.environ.
- Local -> macOS Keychain via `security find-generic-password`.

Manifests and code reference secret *names* only; values live in Keychain
(local) or Actions Secrets (cloud) and never touch the repo. Every resolved
value is registered with `scrub` so it can't leak into logs/notifications.
"""
from __future__ import annotations

import os
import subprocess

import scrub

# Keychain "service" all of this repo's secrets are stored under.
KEYCHAIN_SERVICE = "my-automations"


class SecretNotFound(Exception):
    pass


def _from_keychain(name: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE, "-a", name, "-w",
            ],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None  # not macOS / `security` unavailable
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get(name: str, required: bool = True) -> str | None:
    """Return secret `name`, or raise SecretNotFound if required and missing.

    Resolution order: explicit env var (covers cloud + manual override),
    then macOS Keychain.
    """
    value = os.environ.get(name) or _from_keychain(name)
    if value:
        scrub.register(value)
        return value
    if required:
        raise SecretNotFound(
            f"Secret '{name}' not found. Set it locally with:\n"
            f"  security add-generic-password -s {KEYCHAIN_SERVICE} "
            f"-a {name} -w\n"
            f"or as a GitHub Actions secret named '{name}' for cloud runs."
        )
    return None
