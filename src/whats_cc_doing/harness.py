"""The harness adapter: semantic liveness for Claude Code sessions.

Claude Code already writes its truth to disk:

- session transcripts:  ~/.claude/projects/<slug>/<session-id>.jsonl
- background task output: /tmp/claude-<uid>/<slug>/<session-id>/tasks/*.output

This module joins those artifacts (plus wall-clock time and a best-effort
look at the process table) into a semantic state per session, WITHOUT
instrumenting anything:

- WORKING     transcript is actively growing
- WAITING_ON  parked on background task(s) that are still producing output
              (or whose output file some process still holds open)
- DEAD_WAIT   parked on task(s) whose output stopped moving past threshold
              and that no process appears to still be producing -- stuck,
              with the evidence attached. This is evidence-based inference
              from mtimes and the process table, not proof.
- ABANDONED   would be DEAD_WAIT, but the session itself has been inactive
              longer than stuck_max_age -- treated as informational, never
              STUCK (nobody should auto-resume a long-dead session)
- IDLE        session exists, nothing pending

Design constraints (see DESIGN.md):
- The transcript format is internal and undocumented. Everything here
  degrades gracefully: an unparseable line, a renamed key, or a missing
  directory falls back to plain mtime semantics and never raises.
- All filesystem access goes through TranscriptSource so the data source
  is swappable (Anthropic's Software Directory Policy 1.F makes direct
  transcript reads a submission risk; a hook-event-sourced implementation
  can replace this class without touching the classifier).
- Transcript CONTENT is never surfaced: only timestamps, sizes, line
  types, and ids leave this module.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import age_str as _fmt_age

WORKING_WINDOW_S = 180.0  # transcript younger than this => WORKING
DEAD_AFTER_S = 900.0  # task output older than this => dead-wait territory
STUCK_MAX_AGE_S = 7200.0  # sessions older than this are ABANDONED, not DEAD_WAIT


@dataclass
class TaskInfo:
    path: str
    age_s: float

    @property
    def name(self) -> str:
        return Path(self.path).stem


@dataclass
class SubagentInfo:
    """A subagent of a session, from the dedicated subagents/ metadata store.

    `description` comes from the agent's .meta.json (the short task label
    recorded at spawn time), never from conversation text.
    """

    agent_id: str
    description: str
    age_s: float
    active: bool


@dataclass
class SessionState:
    session_id: str
    state: str  # WORKING | WAITING_ON | DEAD_WAIT | ABANDONED | IDLE | UNKNOWN
    transcript_age_s: float | None
    evidence: str
    tasks: list[TaskInfo] = field(default_factory=list)
    last_line_type: str | None = None
    name: str | None = None  # user-set session name (from ~/.claude/sessions)
    alive: bool | None = None  # registry pid check; None = unknown
    subagents: list[SubagentInfo] = field(default_factory=list)


def slug_for(project_root: str | Path) -> str:
    """Claude Code's project slug: every non-alphanumeric character -> '-'.

    Verified empirically against real ~/.claude/projects entries:
    '/home/nick/Upwork/SSPO/SS PO SUBMISSION/...' is stored as
    '-home-nick-Upwork-SSPO-SS-PO-SUBMISSION-...' -- so spaces (and dots,
    underscores, every other non [A-Za-z0-9] character) become dashes,
    not just '/'.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(project_root).resolve()))


def _norm(p: str | Path) -> str:
    return os.path.normpath(str(p))


def _is_within(child: str, parent: str) -> bool:
    child, parent = _norm(child), _norm(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _cwd_relevant(cwd: str, root: str) -> bool:
    """A recorded cwd counts when it is inside the monitored root, or is an
    ancestor of it (a session started higher up, working on this project --
    the observed real-world case: a session started in the parent dir whose
    transcript is filed under the PARENT's slug, invisible to a plain
    slug lookup)."""
    return _is_within(cwd, root) or _is_within(root, cwd)


def _pid_alive(pid: object, proc_start: object = None) -> bool | None:
    """Best-effort: does the registry pid still belong to that session?

    Uses /proc/<pid>/stat's starttime (field 22) against the registry's
    procStart to guard pid reuse. Returns None when unknowable (no /proc,
    odd data) -- never raises.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    if not os.path.isdir("/proc"):
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if proc_start not in (None, ""):
        try:
            fields = raw.rsplit(")", 1)[1].split()
            return fields[19] == str(proc_start)
        except Exception:  # noqa: BLE001 - degradation contract
            return None
    return True


def _task_file_open(path: Path) -> bool:
    """Best-effort: is some process still holding this task output open?

    Uses `fuser` when available (exit 0 = file in use). A held-open output
    file means the producer is plausibly alive but buffering -- e.g. a
    long test suite that flushes rarely -- so the session is WAITING_ON,
    not DEAD_WAIT. Degrades to False (unknown) without fuser or on error.
    """
    if shutil.which("fuser") is None:
        return False
    try:
        rc = subprocess.run(
            ["fuser", "-s", "--", str(path)],
            capture_output=True, timeout=3,
        ).returncode
        return rc == 0
    except Exception:  # noqa: BLE001 - degradation contract
        return False


class TranscriptSource:
    """Filesystem access seam for Claude Code's on-disk artifacts.

    Swap this class (e.g. for a hook-event-backed source) and the
    classifier below keeps working unchanged.
    """

    def __init__(
        self,
        claude_home: str | Path | None = None,
        task_root_glob: str | None = None,
    ) -> None:
        self.claude_home = Path(claude_home or Path.home() / ".claude")
        # default matches /tmp/claude-<uid>/<slug>/<session-id>/tasks/
        self.task_root_glob = task_root_glob or "/tmp/claude-*"

    def session_transcripts(self, slug: str) -> list[Path]:
        d = self.claude_home / "projects" / slug
        try:
            return sorted(d.glob("*.jsonl"))
        except OSError:
            return []

    def project_dir_names(self) -> list[str]:
        d = self.claude_home / "projects"
        try:
            return [p.name for p in d.iterdir() if p.is_dir()]
        except OSError:
            return []

    def recent_cwd(self, path: Path, max_bytes: int = 65536) -> str | None:
        """Most recent 'cwd' recorded in a transcript (tail read, head
        fallback). Surfaces the path string alone, never content."""
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                fh.seek(max(0, size - max_bytes))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        for line in reversed(tail.strip().splitlines()):
            cwd = self._cwd_of_line(line)
            if cwd:
                return cwd
        if size > max_bytes:
            try:
                with open(path, "rb") as fh:
                    head = fh.read(32768).decode("utf-8", errors="replace")
            except OSError:
                return None
            for line in head.splitlines():
                cwd = self._cwd_of_line(line)
                if cwd:
                    return cwd
        return None

    @staticmethod
    def _cwd_of_line(line: str) -> str | None:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(obj, dict):
            c = obj.get("cwd")
            if isinstance(c, str) and c and os.path.isabs(c):
                return c
        return None

    def subagent_entries(self, slug: str, session_id: str) -> list[tuple[Path, dict]]:
        """(jsonl path, meta dict) per subagent, from the dedicated
        subagents/ store. Meta values are short spawn-time labels
        (description, agentType), not conversation text."""
        d = self.claude_home / "projects" / slug / session_id / "subagents"
        out: list[tuple[Path, dict]] = []
        try:
            for p in sorted(d.glob("agent-*.jsonl")):
                meta: dict = {}
                mp = p.parent / (p.stem + ".meta.json")
                try:
                    if mp.stat().st_size < 65536:
                        raw = json.loads(mp.read_text())
                        if isinstance(raw, dict):
                            meta = raw
                except (OSError, ValueError):
                    meta = {}
                out.append((p, meta))
        except OSError:
            return []
        return out

    def session_registry(self) -> dict[str, dict]:
        """~/.claude/sessions/<pid>.json -> {session_id: {name, pid,
        proc_start}}. Only small scalar fields are surfaced."""
        d = self.claude_home / "sessions"
        out: dict[str, dict] = {}
        try:
            files = sorted(d.glob("*.json"))
        except OSError:
            return out
        for p in files:
            try:
                if p.stat().st_size > 65536:
                    continue
                raw = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            sid = raw.get("sessionId")
            if not isinstance(sid, str):
                continue
            name = raw.get("name")
            entry = {
                "name": name if isinstance(name, str) and name else None,
                "pid": raw.get("pid") if isinstance(raw.get("pid"), int) else None,
                "proc_start": raw.get("procStart"),
            }
            prev = out.get(sid)
            # newest registry file wins for a duplicated session id
            if prev is None or (raw.get("updatedAt") or 0) >= (prev.get("_upd") or 0):
                entry["_upd"] = raw.get("updatedAt") or 0
                out[sid] = entry
        return out

    def task_outputs(self, slug: str, session_id: str) -> list[Path]:
        pattern = f"{self.task_root_glob}/{slug}/{session_id}/tasks/*.output"
        try:
            return [Path(p) for p in glob.glob(pattern)]
        except OSError:
            return []

    def mtime(self, path: Path) -> float | None:
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def last_line_type(self, path: Path, max_bytes: int = 65536) -> str | None:
        """Best-effort 'type' of the last parseable JSONL line.

        Reads only the tail; surfaces the type field alone, never content.
        Returns None on any problem (graceful degradation is the contract).
        """
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                fh.seek(max(0, size - max_bytes))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        for line in reversed(tail.strip().splitlines()):
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                t = obj.get("type")
                if isinstance(t, str):
                    return t
        return None


def classify_sessions(
    project_root: str | Path,
    source: TranscriptSource | None = None,
    now: float | None = None,
    working_window_s: float = WORKING_WINDOW_S,
    dead_after_s: float = DEAD_AFTER_S,
    stuck_max_age_s: float = STUCK_MAX_AGE_S,
    limit: int = 8,
) -> list[SessionState]:
    """Classify every session relevant to a project, newest first.

    Discovery (the cross-slug fix): sessions are filed under the slug of
    their STARTING cwd, so a session started in a parent directory that
    later works on this project is invisible to a plain slug lookup.
    Candidate dirs are therefore: the project's own slug (always trusted,
    old behavior), each ancestor directory's slug, and any project dir
    whose name extends the project's slug (a descendant starting cwd).
    For the latter two, a session is included only when its recorded cwd
    (transcript tail) is inside the monitored root or an ancestor of it.

    Never raises: any per-session failure yields an UNKNOWN state row.
    """
    source = source or TranscriptSource()
    now = now if now is not None else time.time()
    root = _norm(Path(project_root).resolve())
    exact = slug_for(root)

    # (dir name, cwd match required?) - exact slug keeps legacy trust so
    # transcripts without parseable cwd data still show up.
    dirs: list[tuple[str, bool]] = [(exact, False)]
    anc = Path(root).parent
    while anc != anc.parent:
        dirs.append((slug_for(anc), True))
        anc = anc.parent
    names = set(source.project_dir_names())
    scan = [(n, req) for n, req in dirs if n in names]
    scan += sorted((n, True) for n in names if n.startswith(exact + "-"))

    candidates: list[tuple[float, Path, str, bool]] = []
    for dname, req in scan:
        for transcript in source.session_transcripts(dname):
            m = source.mtime(transcript) or 0.0
            candidates.append((m, transcript, dname, req))
    candidates.sort(key=lambda r: r[0], reverse=True)

    rows: list[tuple[float, str, SessionState]] = []
    seen: set[str] = set()
    for mtime, transcript, dname, req in candidates:
        if transcript.stem in seen:
            continue
        if req:
            cwd = source.recent_cwd(transcript)
            if not cwd or not _cwd_relevant(cwd, root):
                continue
        seen.add(transcript.stem)
        try:
            state = _classify_one(
                transcript, dname, source, now, working_window_s, dead_after_s,
                stuck_max_age_s,
            )
        except Exception as exc:  # noqa: BLE001 - degradation contract
            state = SessionState(
                session_id=transcript.stem,
                state="UNKNOWN",
                transcript_age_s=None,
                evidence=f"classification failed: {type(exc).__name__}",
            )
        rows.append((mtime, dname, state))
        if len(rows) >= limit:
            break

    try:
        registry = source.session_registry()
    except Exception:  # noqa: BLE001 - degradation contract
        registry = {}
    out: list[SessionState] = []
    for _, dname, state in rows:
        info = registry.get(state.session_id)
        if info:
            state.name = info.get("name")
            state.alive = _pid_alive(info.get("pid"), info.get("proc_start"))
        try:
            state.subagents = _collect_subagents(
                source, dname, state.session_id, now, working_window_s
            )
        except Exception:  # noqa: BLE001 - degradation contract
            state.subagents = []
        out.append(state)
    return out


_SUBAGENT_SHOW_MAX = 6
_SUBAGENT_MAX_AGE_S = 6 * 3600.0


def _collect_subagents(
    source: TranscriptSource,
    slug: str,
    session_id: str,
    now: float,
    working_window_s: float,
) -> list[SubagentInfo]:
    """Recent subagents of a session, newest first (dedicated store only)."""
    found: list[SubagentInfo] = []
    for path, meta in source.subagent_entries(slug, session_id):
        m = source.mtime(path)
        if m is None:
            continue
        age = max(0.0, now - m)
        if age > _SUBAGENT_MAX_AGE_S:
            continue
        desc = meta.get("description")
        if not isinstance(desc, str) or not desc:
            desc = meta.get("agentType") if isinstance(meta.get("agentType"), str) else ""
        found.append(
            SubagentInfo(
                agent_id=path.stem.removeprefix("agent-")[:17],
                description=str(desc)[:80],
                age_s=age,
                active=age < working_window_s,
            )
        )
    found.sort(key=lambda s: s.age_s)
    return found[:_SUBAGENT_SHOW_MAX]


def _classify_one(
    transcript: Path,
    slug: str,
    source: TranscriptSource,
    now: float,
    working_window_s: float,
    dead_after_s: float,
    stuck_max_age_s: float = STUCK_MAX_AGE_S,
) -> SessionState:
    session_id = transcript.stem
    t_mtime = source.mtime(transcript)
    t_age = None if t_mtime is None else max(0.0, now - t_mtime)
    line_type = source.last_line_type(transcript)

    tasks: list[TaskInfo] = []
    newest_task_mtime: float | None = None
    for out in source.task_outputs(slug, session_id):
        m = source.mtime(out)
        if m is None:
            continue
        tasks.append(TaskInfo(path=str(out), age_s=max(0.0, now - m)))
        if newest_task_mtime is None or m > newest_task_mtime:
            newest_task_mtime = m
    tasks.sort(key=lambda t: t.age_s)

    if t_age is None:
        return SessionState(
            session_id, "UNKNOWN", None, "transcript unreadable", tasks, line_type
        )

    if t_age < working_window_s:
        return SessionState(
            session_id,
            "WORKING",
            t_age,
            f"transcript updated {_fmt_age(t_age)} ago",
            tasks,
            line_type,
        )

    if newest_task_mtime is not None:
        task_age = max(0.0, now - newest_task_mtime)
        newest = tasks[0]
        if task_age < dead_after_s:
            return SessionState(
                session_id,
                "WAITING_ON",
                t_age,
                f"task {newest.name} produced output {_fmt_age(task_age)} ago",
                tasks,
                line_type,
            )
        # Both transcript and tasks are stale. If the session's last
        # activity predates (or ties) the tasks' last output, the session
        # is parked on work that stopped moving: dead-wait territory.
        # If the transcript moved after the tasks went quiet, the session
        # saw them and moved on: IDLE.
        # This is evidence-based inference from mtimes and the process
        # table -- not proof -- so two escape hatches apply first:
        # a producer still holding the output file open means "slow, not
        # dead" (WAITING_ON), and a session inactive past stuck_max_age_s
        # is ABANDONED (informational) rather than DEAD_WAIT, so the
        # watchdog never auto-resumes a long-dead session.
        if t_mtime <= newest_task_mtime + 1.0:
            if _task_file_open(Path(newest.path)):
                return SessionState(
                    session_id,
                    "WAITING_ON",
                    t_age,
                    (
                        f"task {newest.name} output is stale ({_fmt_age(task_age)}) "
                        "but still held open by a process - producer alive, "
                        "likely buffering"
                    ),
                    tasks,
                    line_type,
                )
            if t_age > stuck_max_age_s:
                return SessionState(
                    session_id,
                    "ABANDONED",
                    t_age,
                    (
                        f"session inactive {_fmt_age(t_age)} (past the "
                        f"{int(stuck_max_age_s / 60)}m stuck_max_age cutoff); "
                        f"was parked on task {newest.name} - treated as "
                        "abandoned, not stuck"
                    ),
                    tasks,
                    line_type,
                )
            return SessionState(
                session_id,
                "DEAD_WAIT",
                t_age,
                (
                    f"session last acted {_fmt_age(t_age)} ago and is parked on "
                    f"task {newest.name}, whose output stopped moving "
                    f"{_fmt_age(task_age)} ago (threshold {int(dead_after_s / 60)}m)"
                ),
                tasks,
                line_type,
            )

    return SessionState(
        session_id,
        "IDLE",
        t_age,
        f"no pending work; transcript idle {_fmt_age(t_age)}",
        tasks,
        line_type,
    )
