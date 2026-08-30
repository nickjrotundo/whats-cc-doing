"""Config-drift detection: is the page still telling the truth?

Origin story: the original internal page's "Latest eval headlines" went stale the
day after shipping because eval JSONs moved from hardcoded /tmp paths to
ad-hoc --save names (found in the field, 2026-08-30). A signal whose
configured target stops matching anything should SAY so instead of
quietly rendering nothing forever.

Deterministic, no LLM. Two mechanisms:

- Per-tick match bookkeeping (.ccdoing/drift.json): every signal whose
  collector reports whether its target matched gets a
  first_seen/last_matched record. From that, each rendered signal gets a
  `state`:
    ok        target matched (or matching is not meaningful for the type)
    no-match  target matched nothing this tick (path-shaped types only)
    stale     target has matched nothing for stale_after_days
- Inventory re-diff (`ccdoing doctor --drift`): re-run the init-time
  detection against the project and report detectable-but-unconfigured
  signal types alongside the no-match/stale signals.

The Claude layer (/ccdoing:tune) turns these findings into proposed
config deltas; nothing here edits config.

drift.json is deliberately separate from the watchdog's state.json: the
escalation state machine is lock-guarded and validated; this bookkeeping
is monotonic timestamps where last-writer-wins is harmless.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .signals import Reading, detect_signals

DRIFT_FILE = "drift.json"

# Types where "matched nothing this tick" is itself worth showing: the
# configured path/glob/pattern points at something that does not exist.
NOMATCH_VISIBLE = {"file_mtime", "log_tail", "jsonl_log", "json_headline", "claude_session"}

# Types tracked for staleness. `process` joins here but NOT above: a test
# runner not running right now is normal data, a pattern that has matched
# nothing for a week is probably misconfigured.
STALE_TRACKED = NOMATCH_VISIBLE | {"process"}

HINT = "may be misconfigured - run `ccdoing doctor --drift`"


def key_for(reading: Reading, index: int) -> str:
    # Positional index disambiguates two signals sharing type+label
    # (otherwise their bookkeeping and rendered states collide).
    # Reordering config resets bookkeeping for moved signals - fine.
    return f"{reading.type}:{reading.label}#{index}"


def load_drift(state_dir: Path) -> dict[str, dict[str, Any]]:
    p = state_dir / DRIFT_FILE
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        first = v.get("first_seen")
        last = v.get("last_matched")
        if not isinstance(first, (int, float)):
            continue
        if last is not None and not isinstance(last, (int, float)):
            continue
        out[k] = {"first_seen": float(first),
                  "last_matched": float(last) if last is not None else None}
    return out


def save_drift(state_dir: Path, data: dict[str, dict[str, Any]]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (DRIFT_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(state_dir / DRIFT_FILE)


def apply_states(
    readings: list[Reading],
    state_dir: Path,
    now: float | None = None,
    stale_after_s: float = 7 * 86400.0,
) -> dict[str, str]:
    """Update match bookkeeping and return {key_for(r): state} for all readings.

    Never raises: bookkeeping failures degrade to everything-ok, because
    drift annotation must not take the monitor down.
    """
    now = now if now is not None else time.time()
    states: dict[str, str] = {}
    try:
        data = load_drift(state_dir)
        for i, r in enumerate(readings):
            key = key_for(r, i)
            if r.matched is None or not r.ok:
                # matching not meaningful for this type, or the probe itself
                # failed (already rendered loudly as unreadable)
                states[key] = "ok"
                continue
            ent = data.setdefault(key, {"first_seen": now, "last_matched": None})
            if r.matched:
                ent["last_matched"] = now
                states[key] = "ok"
                continue
            ref = ent["last_matched"] if ent["last_matched"] is not None else ent["first_seen"]
            if r.type in STALE_TRACKED and (now - ref) > stale_after_s:
                states[key] = "stale"
            elif r.type in NOMATCH_VISIBLE:
                states[key] = "no-match"
            else:
                states[key] = "ok"
        save_drift(state_dir, data)
    except Exception:  # noqa: BLE001 - annotation must never break a tick
        for i, r in enumerate(readings):
            states.setdefault(key_for(r, i), "ok")
    return states


def maintenance_lines(readings: list[Reading], states: dict[str, str]) -> list[str]:
    """Human-readable drift summary for the snapshot's `maintenance` list."""
    lines = []
    for i, r in enumerate(readings):
        st = states.get(key_for(r, i), "ok")
        if st == "no-match":
            lines.append(f"signal '{r.label}' ({r.type}) matched nothing this tick")
        elif st == "stale":
            lines.append(
                f"signal '{r.label}' ({r.type}) has matched nothing for days - "
                "probably misconfigured"
            )
    return lines


def inventory_drift(project_root: Path, configured: list[dict]) -> list[dict]:
    """Detected-but-unconfigured suggestions (conservative: diff by type).

    Type-level diffing keeps this quiet: if the project has ANY process
    signal configured, no process suggestion is repeated even if detection
    would word it differently.
    """
    have = {str(s.get("type", "")) for s in configured}
    return [s for s in detect_signals(project_root) if s.get("type") not in have]
