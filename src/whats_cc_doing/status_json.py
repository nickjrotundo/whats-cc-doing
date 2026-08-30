"""Build the machine-readable snapshot: status.json is the interface.

Agents, watchdogs, and scripts consume this JSON. Nothing should ever
scrape status.html - the HTML is a rendering of this same snapshot.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from . import __version__
from .config import Config
from .signals import Reading
from .verdict import Verdict


def build_snapshot(
    readings: list[Reading],
    verdict: Verdict,
    cfg: Config,
    now: float,
    quiet_since: float | None = None,
    signal_states: dict[str, str] | None = None,
    maintenance: list[str] | None = None,
) -> dict[str, Any]:
    signal_states = signal_states or {}
    return {
        "generator": f"whats-cc-doing {__version__}",
        "project": cfg.project_name,
        # Human-facing page identity; None -> the renderer shows plain
        # "What's CC Doing". Machines should key on `project`.
        "title": cfg.title,
        "timezone": cfg.timezone,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "generated_epoch": now,
        "refresh_seconds": cfg.refresh_seconds,
        "verdict": verdict.state,
        "cause": verdict.cause,
        "stuck_session_ids": verdict.stuck_session_ids,
        "quiet_since_epoch": quiet_since,
        "quiet_for_seconds": (now - quiet_since) if quiet_since else None,
        "active_window_minutes": cfg.verdict.active_window_minutes,
        # Drift summary (see drift.py): non-empty when configured signals
        # look misconfigured; /ccdoing:tune and `ccdoing doctor --drift`
        # start from here.
        "maintenance": maintenance or [],
        "signals": [
            {
                "label": r.label,
                "type": r.type,
                "weight": r.weight,
                # ok = probe worked | no-match = target matched nothing this
                # tick | stale = matched nothing for stale_after_days
                "state": signal_states.get(f"{r.type}:{r.label}#{i}", "ok"),
                "ok": r.ok,
                "fresh": r.fresh,
                "healthy": r.healthy,
                "age_seconds": r.age_s,
                "detail": r.detail,
                "lines": r.lines,
                "error": r.error,
                "sessions": [asdict(s) for s in r.sessions],
            }
            for i, r in enumerate(readings)
        ],
    }
