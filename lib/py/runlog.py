"""Structured per-run logging.

Each run appends one JSON line to logs/runs.jsonl:
  {id, trigger, start, end, status, error?}
All free-text (notably `error`) is scrubbed before writing.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager

import config
import scrub

RUNS_LOG = config.LOGS_DIR / "runs.jsonl"


def _now_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _append(record: dict) -> None:
    config.ensure_runtime_dirs()
    with RUNS_LOG.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


@contextmanager
def record(automation_id: str, trigger: str):
    """Context manager that logs start/end/status for a run.

    Usage:
        with runlog.record("finance-weekly", "cli"):
            ...do work...
    Exceptions are logged (scrubbed) and re-raised.
    """
    start = time.time()
    rec = {"id": automation_id, "trigger": trigger, "start": _now_iso(start)}
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - we log then re-raise
        rec.update(
            status="error",
            end=_now_iso(time.time()),
            error=scrub.clean(f"{type(exc).__name__}: {exc}"),
        )
        _append(rec)
        raise
    else:
        rec.update(status="ok", end=_now_iso(time.time()))
        _append(rec)


def read_recent(limit: int = 50) -> list[dict]:
    """Return the most recent run records (newest last). For the UI."""
    if not RUNS_LOG.exists():
        return []
    lines = RUNS_LOG.read_text().splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
