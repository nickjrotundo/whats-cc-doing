"""Multi-project registry: which projects on this machine run ccdoing.

One machine, many monitored projects (each with its own ccdoing.yaml and
status output) is the normal case. The registry is a tiny JSON list of
project roots in XDG state, written by `ccdoing init` and read by
`ccdoing projects` and `ccdoing view --project`, so a headless/ssh user
can find and open any project's status without remembering paths or
serving anything on a port.

Entries are validated on every read: a directory that no longer exists
or no longer has a ccdoing.yaml is silently dropped (and the file
rewritten), so the registry never accumulates corpses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import CONFIG_FILENAME

_STATE_SUBDIR = "ccdoing"
_STATE_FILE = "projects.json"


def registry_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / _STATE_SUBDIR / _STATE_FILE


def _read_raw() -> list[str]:
    p = registry_path()
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(doc, list):
        return []
    return [e for e in doc if isinstance(e, str)]


def _write_raw(entries: list[str]) -> None:
    p = registry_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(set(entries)), indent=1))
        tmp.replace(p)
    except OSError:
        # The registry is a convenience; never let it break init/view.
        pass


def _valid(root: Path) -> bool:
    return root.is_dir() and (root / CONFIG_FILENAME).is_file()


def register(root: Path) -> None:
    """Idempotently add a project root (called by `ccdoing init`)."""
    entries = _read_raw()
    resolved = str(Path(root).resolve())
    if resolved not in entries:
        entries.append(resolved)
        _write_raw(entries)


def unregister(query: str) -> Path | None:
    """Remove a project by name or path; returns what was removed.

    Works even on entries whose directory/config is already gone (the
    one case validation-on-read would eventually clean anyway)."""
    entries = _read_raw()
    q = query.rstrip("/")
    resolved = str(Path(q).expanduser().resolve()) if "/" in q else None
    for e in list(entries):
        if e == resolved or Path(e).name == q or e == q:
            entries.remove(e)
            _write_raw(entries)
            return Path(e)
    return None


def load() -> list[Path]:
    """Registered project roots, validated; stale entries are dropped."""
    raw = _read_raw()
    roots = [Path(e) for e in raw]
    valid = [r for r in roots if _valid(r)]
    if len(valid) != len(roots):
        _write_raw([str(r) for r in valid])
    return valid


def find(query: str) -> Path | None:
    """Resolve a --project argument: exact path, exact name, then substring."""
    q = query.rstrip("/")
    as_path = Path(q).expanduser()
    if _valid(as_path):
        return as_path.resolve()
    projects = load()
    for r in projects:
        if r.name == q:
            return r
    matches = [r for r in projects if q in str(r)]
    return matches[0] if len(matches) == 1 else None
