"""Compute the verdict: ACTIVE / QUIET / DOWN / STUCK, with cause attribution.

Precedence (loudest problem wins):
  DOWN   a health-weighted signal failed (and health_failure_is_down)
  STUCK  the harness adapter proved a dead wait
  ACTIVE any primary signal shows activity inside the window
  QUIET  none did

The cause string always explains the verdict in one line; a page that
says QUIET without saying why teaches nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .signals import Reading
from .util import age_str as _age


@dataclass
class Verdict:
    state: str  # ACTIVE | QUIET | DOWN | STUCK
    cause: str
    stuck_session_ids: list[str] = field(default_factory=list)


def compute_verdict(readings: list[Reading], cfg: Config) -> Verdict:
    down_causes = []
    if cfg.verdict.health_failure_is_down:
        for r in readings:
            if r.weight == "health" and r.healthy is False:
                down_causes.append(f"{r.label}: {r.detail or 'failed'}")
    if down_causes:
        return Verdict("DOWN", "; ".join(down_causes))

    stuck_ids: list[str] = []
    stuck_causes: list[str] = []
    for r in readings:
        for s in r.sessions:
            if s.state == "DEAD_WAIT":
                stuck_ids.append(s.session_id)
                stuck_causes.append(f"session {s.session_id[:12]}: {s.evidence}")
    if stuck_ids:
        return Verdict("STUCK", "; ".join(stuck_causes), stuck_ids)

    primaries = [r for r in readings if r.weight == "primary"]
    fresh = [r for r in primaries if r.ok and r.fresh]
    if fresh:
        names = ", ".join(r.label for r in fresh)
        return Verdict("ACTIVE", f"activity on: {names}")

    if not primaries:
        return Verdict("QUIET", "no primary signals configured")

    parts = []
    for r in primaries:
        if not r.ok:
            parts.append(f"{r.label} unreadable ({r.error})")
        elif r.age_s is not None:
            parts.append(f"{r.label} last moved {_age(r.age_s)} ago")
        else:
            parts.append(f"{r.label}: {r.detail or 'no activity'}")
    return Verdict("QUIET", "; ".join(parts))
