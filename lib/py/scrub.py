"""Redact secret values from any string before it is logged or sent.

Every secret value resolved at runtime is registered here. `clean()` removes
registered values plus common token-shaped / basic-auth-in-URL patterns. This
is the §11.1 leak guard — logging and notify both run output through it.
"""
from __future__ import annotations

import re

_REDACTED = "***REDACTED***"

# Registered exact secret values (filled at runtime by secrets_store.get()).
_SECRETS: set[str] = set()

# Basic-auth credentials embedded in a URL: https://user:pass@host/...
_URL_CREDS = re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@")

# Telegram bot token shape: 123456789:AA... (35 char tail)
_TG_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")


def register(value: str | None) -> None:
    """Mark a value as secret so clean() will redact it everywhere."""
    if value and len(value) >= 4:
        _SECRETS.add(value)


def clean(text: str) -> str:
    if not text:
        return text
    out = text
    # Exact registered secrets first (longest first to avoid partial overlaps).
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, _REDACTED)
    out = _URL_CREDS.sub(r"\1" + _REDACTED + "@", out)
    out = _TG_TOKEN.sub(_REDACTED, out)
    return out
