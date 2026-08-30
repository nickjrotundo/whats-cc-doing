"""The ten signal types: passive readings of observed side effects.

Every signal follows the same contract:

- collect() NEVER raises. Failure to read is itself a reading
  (ok=False with an error string), because a monitoring page that
  crashes on a missing binary is worse than no page.
- detect() answers "does this signal plausibly apply to this project?"
  and returns suggested config entries. The Claude Code setup skill uses
  these suggestions to propose a config; a human (or Claude) can always
  add more by hand.

Weights (config `weight`):
- primary  drives the ACTIVE/QUIET verdict (fresh primary => ACTIVE)
- info     displayed only
- health   drives UP/DOWN, not quiet (a down app is DOWN, not QUIET)
- alert    pattern-match warnings (rendered, never part of the verdict)
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import string
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import harness
from .util import age_str as _age

FILE_GLOB_CAP = 20000  # file_mtime stops scanning past this many entries


@dataclass
class Reading:
    label: str
    type: str
    weight: str  # primary | info | health | alert
    ok: bool  # the probe itself succeeded
    fresh: bool | None = None  # primary/alert: activity inside the window?
    healthy: bool | None = None  # health: probe passed?
    age_s: float | None = None
    detail: str = ""
    lines: list[str] = field(default_factory=list)  # extra rendered rows
    sessions: list[harness.SessionState] = field(default_factory=list)
    error: str | None = None
    # Drift bookkeeping (see drift.py): did the configured target (glob,
    # path, pattern, ...) match anything this tick? None = matching is not
    # a meaningful question for this type.
    matched: bool | None = None


@dataclass
class Context:
    project_root: Path
    now: float
    window_s: float  # the verdict's active window
    # Directories whose contents never count as project activity - the
    # monitor's own output and state dirs. Without this, a broad glob like
    # "**/*" reads every tick's status.html write as fresh activity and the
    # verdict can never go QUIET (self-triggering ACTIVE).
    exclude_dirs: tuple[Path, ...] = ()


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 10) -> str:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
    ).stdout


def _fail(cfg: dict, kind: str, err: str) -> Reading:
    return Reading(
        label=cfg.get("label", kind),
        type=kind,
        weight=cfg.get("weight", "primary"),
        ok=False,
        error=err,
    )


# --------------------------------------------------------------------------
# git


def collect_git(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "git commits")
    weight = cfg.get("weight", "primary")
    repo = ctx.project_root / cfg.get("repo", ".")
    try:
        raw = _run(["git", "log", "-1", "--format=%ct"], cwd=repo).strip()
        if not raw.isdigit():
            return _fail(cfg, "git", "no commits found (or not a git repo)")
        age = max(0.0, ctx.now - float(raw))
        show = int(cfg.get("show_last", 8))
        lines = _run(
            ["git", "log", f"-{show}", "--pretty=%h %cr %s"], cwd=repo
        ).strip().splitlines()
        return Reading(
            label=label,
            type="git",
            weight=weight,
            ok=True,
            fresh=age < ctx.window_s,
            age_s=age,
            detail=f"last commit {_age(age)} ago",
            lines=lines,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(cfg, "git", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# process


def _redact_process_line(line: str) -> str:
    """'12345 /long/path/python3 -m pytest -x --foo' -> '12345 python3 (+3 args)'.

    Full command lines can leak paths, env fragments, and one-liners into a
    status page that may be published (GitHub Pages, a static mount).
    Redaction is the default; opt out per-signal with `redact: false`.
    """
    parts = line.split()
    if len(parts) < 2:
        return line
    pid, argv0, rest = parts[0], parts[1], parts[2:]
    base = argv0.rsplit("/", 1)[-1]
    suffix = f" (+{len(rest)} args)" if rest else ""
    return f"{pid} {base}{suffix}"


def collect_process(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "processes")
    weight = cfg.get("weight", "primary")
    pattern = cfg.get("pattern", "")
    if not pattern:
        return _fail(cfg, "process", "no pattern configured")
    try:
        proc = subprocess.run(
            ["pgrep", "-af", pattern], capture_output=True, text=True, timeout=10
        )
        # pgrep: 0 = matches, 1 = no matches, 2/3 = bad pattern / error.
        # A bad pattern must surface as an error, never as "not running".
        if proc.returncode not in (0, 1):
            err = (proc.stderr or "").strip() or f"pgrep exited {proc.returncode}"
            return _fail(cfg, "process", f"invalid pattern? {err}")
        own_pids = {str(os.getpid()), str(os.getppid())}
        lines = []
        for l in proc.stdout.splitlines():
            if not l.strip():
                continue
            pid = l.split(None, 1)[0]
            if pid in own_pids:
                continue  # this tick (or the loop that spawned it)
            if re.search(r"(^|[/\s])ccdoing(\s|$)", l.split(None, 1)[-1]):
                continue  # another ccdoing invocation, not the monitored work
            lines.append(l)
        running = bool(lines)
        redact = bool(cfg.get("redact", True))
        shown = lines[:8] if not redact else [_redact_process_line(l) for l in lines[:8]]
        return Reading(
            label=label,
            type="process",
            weight=weight,
            ok=True,
            fresh=running,
            age_s=0.0 if running else None,
            detail=f"{len(lines)} matching process(es)" if running else "not running",
            lines=shown, matched=running,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(cfg, "process", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# file_mtime


def collect_file_mtime(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "files")
    weight = cfg.get("weight", "primary")
    pattern = cfg.get("glob", "")
    if not pattern:
        return _fail(cfg, "file_mtime", "no glob configured")
    if not os.path.isabs(pattern) and not pattern.startswith("~"):
        pattern = str(ctx.project_root / pattern)
    pattern = os.path.expanduser(pattern)
    try:
        rows: list[tuple[str, float]] = []
        capped = False
        excluded = [str(d) + os.sep for d in ctx.exclude_dirs]
        for i, p in enumerate(glob.iglob(pattern, recursive=True)):
            if i >= FILE_GLOB_CAP:
                capped = True
                break
            try:
                ap = os.path.abspath(p)
                if any(ap.startswith(ex) for ex in excluded):
                    continue
                if os.path.isfile(p):
                    rows.append((p, os.path.getmtime(p)))
            except OSError:
                continue
        if not rows:
            return Reading(
                label=label, type="file_mtime", weight=weight, ok=True,
                fresh=False, detail="no matching files", matched=False,
            )
        rows.sort(key=lambda r: r[1], reverse=True)
        newest_age = max(0.0, ctx.now - rows[0][1])
        show = int(cfg.get("show_newest", 5))
        lines = [f"{Path(p).name}  {_age(ctx.now - m)} ago" for p, m in rows[:show]]
        cap_note = f" (scan capped at {FILE_GLOB_CAP})" if capped else ""
        return Reading(
            label=label, type="file_mtime", weight=weight, ok=True,
            fresh=newest_age < ctx.window_s, age_s=newest_age,
            detail=f"newest of {len(rows)} file(s): {_age(newest_age)} ago{cap_note}",
            lines=lines, matched=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(cfg, "file_mtime", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# http


def collect_http(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "health check")
    weight = cfg.get("weight", "health")
    url = cfg.get("url", "")
    if not url:
        return _fail(cfg, "http", "no url configured")
    expect = int(cfg.get("expect_status", 200))
    timeout = float(cfg.get("timeout_s", 4))
    try:
        import urllib.error

        req = urllib.request.Request(url, headers={"User-Agent": "ccdoing"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as http_exc:
            # A served 4xx/5xx is a REAL status, not "unreachable" - so
            # expect_status: 404 (etc.) is honored. Note: redirects are
            # auto-followed, so 3xx expectations cannot match.
            status = http_exc.code
        healthy = status == expect
        return Reading(
            label=label, type="http", weight=weight, ok=True,
            healthy=healthy,
            detail=f"{url} -> {status}" + ("" if healthy else f" (expected {expect})"),
        )
    except Exception as exc:  # noqa: BLE001
        return Reading(
            label=label, type="http", weight=weight, ok=True, healthy=False,
            detail=f"{url} unreachable: {type(exc).__name__}",
        )


# --------------------------------------------------------------------------
# log_tail


def collect_log_tail(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "log errors")
    weight = cfg.get("weight", "alert")
    path = cfg.get("path", "")
    if not path:
        return _fail(cfg, "log_tail", "no path configured")
    p = Path(path)
    if not p.is_absolute():
        p = ctx.project_root / p
    pattern = cfg.get("error_pattern", r"ERROR|CRITICAL|Traceback")
    window_s = float(cfg.get("window_minutes", 15)) * 60.0
    try:
        if not p.is_file():
            return Reading(
                label=label, type="log_tail", weight=weight, ok=True,
                fresh=False, detail=f"{p.name}: no such file", matched=False,
            )
        mtime = os.path.getmtime(p)
        if ctx.now - mtime > window_s:
            return Reading(
                label=label, type="log_tail", weight=weight, ok=True,
                fresh=False, age_s=ctx.now - mtime,
                detail=f"quiet (log untouched for {_age(ctx.now - mtime)})",
                matched=True,
            )
        size = p.stat().st_size
        with open(p, "rb") as fh:
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", errors="replace")
        rx = re.compile(pattern)
        hits = [l for l in tail.splitlines() if rx.search(l)]
        return Reading(
            label=label, type="log_tail", weight=weight, ok=True,
            fresh=bool(hits), age_s=ctx.now - mtime,
            detail=f"{len(hits)} matching line(s) in recent tail"
            if hits else "no matches in recent tail",
            lines=hits[-5:], matched=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(cfg, "log_tail", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# jsonl_log


def collect_jsonl_log(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "usage log")
    weight = cfg.get("weight", "primary")
    path = cfg.get("path", "")
    if not path:
        return _fail(cfg, "jsonl_log", "no path configured")
    p = Path(path)
    if not p.is_absolute():
        p = ctx.project_root / p
    try:
        if not p.is_file():
            return Reading(
                label=label, type="jsonl_log", weight=weight, ok=True,
                fresh=False, detail=f"{p.name}: no such file", matched=False,
            )
        mtime = os.path.getmtime(p)
        age = max(0.0, ctx.now - mtime)
        # "Today" is UTC-today (log timestamps are assumed UTC). Only the
        # tail is read so a large usage log cannot make ticks crawl; the
        # count is honest about it when truncated.
        today = time.strftime("%Y-%m-%d", time.gmtime(ctx.now))
        tail_bytes = 262144
        size = p.stat().st_size
        with open(p, "rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            tail = fh.read().decode("utf-8", errors="replace")
        count = sum(1 for line in tail.splitlines() if today in line[:120])
        approx = "~" if size > tail_bytes else ""
        return Reading(
            label=label, type="jsonl_log", weight=weight, ok=True,
            fresh=age < ctx.window_s, age_s=age,
            detail=f"{approx}{count} entr(ies) today (UTC); last {_age(age)} ago",
            matched=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(cfg, "jsonl_log", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# claude_session (the harness adapter)


def collect_claude_session(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "claude sessions")
    weight = cfg.get("weight", "primary")
    try:
        source = harness.TranscriptSource(
            claude_home=cfg.get("claude_home"),
            task_root_glob=cfg.get("task_root_glob"),
        )
        sessions = harness.classify_sessions(
            cfg.get("project_root") or ctx.project_root,
            source=source,
            now=ctx.now,
            working_window_s=float(cfg.get("working_window_s", harness.WORKING_WINDOW_S)),
            dead_after_s=float(cfg.get("dead_after_s", harness.DEAD_AFTER_S)),
            stuck_max_age_s=float(cfg.get("stuck_max_age_minutes", 120)) * 60.0,
        )
        if not sessions:
            return Reading(
                label=label, type="claude_session", weight=weight, ok=True,
                fresh=False, detail="no sessions found for this project",
                matched=False,
            )
        states = [s.state for s in sessions]
        active = any(s in ("WORKING", "WAITING_ON") for s in states)
        newest = sessions[0]
        summary = ", ".join(
            f"{st}:{states.count(st)}" for st in dict.fromkeys(states)
        )
        return Reading(
            label=label, type="claude_session", weight=weight, ok=True,
            fresh=active, age_s=newest.transcript_age_s,
            detail=summary, sessions=sessions, matched=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(cfg, "claude_session", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# json_headline
#
# Origin: the original status page's "Latest eval headlines" read two
# HARDCODED /tmp paths and went stale the day after shipping, because
# eval runs save under ad-hoc --save names. Nick's field patch
# (2026-08-30) became this signal type: glob-pattern discovery, newest
# file BY MTIME wins, and a min_items shape filter so a single-scenario
# save cannot masquerade as the full battery headline.


class _Missing:
    """Renders as '?' under any format spec, so a template referencing a
    field the JSON lacks degrades instead of raising."""

    def __format__(self, _spec: str) -> str:
        return "?"


class _SafeFormatter(string.Formatter):
    def get_value(self, key, args, kwargs):  # noqa: ANN001
        if isinstance(key, str) and key in kwargs:
            return kwargs[key]
        return _Missing()


def _headline_fields(doc: dict, items: list) -> dict[str, Any]:
    dict_items = [i for i in items if isinstance(i, dict)]
    fields: dict[str, Any] = {
        k: v for k, v in doc.items() if isinstance(v, (str, int, float, bool))
    }
    fields["passed"] = sum(1 for i in dict_items if i.get("passed"))
    fields["skipped"] = sum(1 for i in dict_items if i.get("skipped"))
    fields["total"] = len(items)
    fields["runnable"] = fields["total"] - fields["skipped"]
    return fields


def _headline_items(doc: Any, items_key: str | None) -> list | None:
    if not isinstance(doc, dict):
        return None
    keys = [items_key] if items_key else ["sessions", "results"]
    for k in keys:
        v = doc.get(k)
        if isinstance(v, list):
            return v
    return None


def collect_json_headline(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "eval headline")
    weight = cfg.get("weight", "info")
    patterns = cfg.get("patterns") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    if not patterns:
        return _fail(cfg, "json_headline", "no patterns configured")
    min_items = int(cfg.get("min_items", 1))
    items_key = cfg.get("items_key")
    template = str(cfg.get("template", "{passed}/{total} passed"))
    try:
        candidates: list[str] = []
        for pat in patterns:
            if not os.path.isabs(pat) and not pat.startswith("~"):
                pat = str(ctx.project_root / pat)
            candidates.extend(glob.glob(os.path.expanduser(pat), recursive=True))
        candidates = [p for p in candidates if os.path.isfile(p)]
        # Newest file whose item count clears min_items wins; smaller,
        # newer partial saves are skipped, unreadable JSON is skipped.
        winner = winner_items = None
        for p in sorted(candidates, key=os.path.getmtime, reverse=True):
            try:
                doc = json.load(open(p))
            except Exception:  # noqa: BLE001
                continue
            items = _headline_items(doc, items_key)
            if items is not None and len(items) >= min_items:
                winner, winner_items, winner_doc = p, items, doc
                break
        if winner is None:
            return Reading(
                label=label, type="json_headline", weight=weight, ok=True,
                fresh=False, matched=False,
                detail=f"no matching JSON with >= {min_items} item(s)",
            )
        fields = _headline_fields(winner_doc, winner_items)
        try:
            rendered = _SafeFormatter().vformat(template, (), fields)
        except Exception:  # noqa: BLE001 - a broken template still headlines
            rendered = f"{fields['passed']}/{fields['total']} passed"
        age = max(0.0, ctx.now - os.path.getmtime(winner))
        return Reading(
            label=label, type="json_headline", weight=weight, ok=True,
            fresh=age < ctx.window_s, age_s=age, matched=True,
            detail=f"{rendered} (saved {_age(age)} ago)",
            lines=[Path(winner).name],
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(cfg, "json_headline", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# ci


def collect_ci(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "GitHub Actions")
    weight = cfg.get("weight", "info")
    try:
        if shutil.which("gh") is None:
            return Reading(
                label=label, type="ci", weight=weight, ok=True,
                fresh=False, detail="gh CLI not installed",
            )
        out = _run(
            ["gh", "run", "list", "--limit", str(int(cfg.get("limit", 5))),
             "--json", "displayTitle,conclusion,updatedAt,status"],
            cwd=ctx.project_root, timeout=15,
        )
        runs = json.loads(out or "[]")
        if not runs:
            return Reading(
                label=label, type="ci", weight=weight, ok=True,
                fresh=False, detail="no runs",
            )
        lines = [
            f"{r.get('conclusion') or r.get('status')}: {r.get('displayTitle')}"
            for r in runs
        ]
        return Reading(
            label=label, type="ci", weight=weight, ok=True,
            fresh=any(r.get("status") == "in_progress" for r in runs),
            detail=f"latest: {lines[0]}", lines=lines,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(cfg, "ci", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# command (escape hatch)
#
# TRUST MODEL: the command string runs with shell=True as YOU. ccdoing.yaml
# is config-as-code - treat it like a Makefile. Never run `ccdoing tick`
# against a config you haven't read (e.g. in a freshly cloned repo), and
# remember command stdout flows into rendered pages and the nudge
# evidence bundle (delimited there as untrusted data).


def collect_command(cfg: dict, ctx: Context) -> Reading:
    label = cfg.get("label", "command")
    weight = cfg.get("weight", "info")
    command = cfg.get("command", "")
    if not command:
        return _fail(cfg, "command", "no command configured")
    parse = cfg.get("parse", "text")
    try:
        out = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=int(cfg.get("timeout_s", 10)), cwd=ctx.project_root,
        ).stdout.strip()
        if parse == "number":
            value = float(out.splitlines()[0]) if out else 0.0
            return Reading(
                label=label, type="command", weight=weight, ok=True,
                fresh=value > 0, detail=f"{value:g}",
            )
        if parse == "epoch_mtime":
            epoch = float(out.splitlines()[0]) if out else 0.0
            age = max(0.0, ctx.now - epoch)
            return Reading(
                label=label, type="command", weight=weight, ok=True,
                fresh=age < ctx.window_s, age_s=age, detail=f"{_age(age)} ago",
            )
        return Reading(
            label=label, type="command", weight=weight, ok=True,
            fresh=bool(out), detail=out[:200] or "(no output)",
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(cfg, "command", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# registry + detection

COLLECTORS = {
    "git": collect_git,
    "process": collect_process,
    "file_mtime": collect_file_mtime,
    "http": collect_http,
    "log_tail": collect_log_tail,
    "jsonl_log": collect_jsonl_log,
    "claude_session": collect_claude_session,
    "json_headline": collect_json_headline,
    "ci": collect_ci,
    "command": collect_command,
}


def collect_all(signal_cfgs: list[dict], ctx: Context) -> list[Reading]:
    readings = []
    for cfg in signal_cfgs:
        kind = cfg.get("type", "")
        fn = COLLECTORS.get(kind)
        if fn is None:
            readings.append(_fail(cfg, kind or "?", f"unknown signal type '{kind}'"))
            continue
        readings.append(fn(cfg, ctx))
    return readings


def detect_signals(project_root: Path) -> list[dict[str, Any]]:
    """Inventory a project and suggest signal configs (the setup skill's raw material)."""
    root = project_root.resolve()
    suggestions: list[dict[str, Any]] = []

    if (root / ".git").exists():
        suggestions.append({"type": "git", "label": "git commits", "weight": "primary"})

    slug = harness.slug_for(root)
    if (Path.home() / ".claude" / "projects" / slug).is_dir() or (root / ".claude").is_dir():
        suggestions.append(
            {"type": "claude_session", "label": "claude sessions", "weight": "primary"}
        )

    for pat, label in [
        ("dist/**/*", "build output"),
        ("build/**/*", "build output"),
        ("coverage/**/*", "coverage output"),
        ("target/**/*", "build output"),
    ]:
        if list(root.glob(pat.split("/")[0])):
            suggestions.append(
                {"type": "file_mtime", "label": label, "glob": pat, "weight": "info"}
            )
            break

    logs = list(root.glob("logs/*.log")) + list(root.glob("*.log"))
    if logs:
        rel = logs[0].relative_to(root)
        suggestions.append(
            {"type": "log_tail", "label": "log errors", "path": str(rel), "weight": "alert"}
        )

    if (root / ".github" / "workflows").is_dir() and shutil.which("gh"):
        suggestions.append({"type": "ci", "label": "GitHub Actions", "weight": "info"})

    # json_headline: conservative - only suggest when a conventional results
    # dir holds JSON that actually looks like eval/test output (a list under
    # "sessions"/"results" whose items carry a "passed" key).
    for d in ("eval-results", "results", "test-results"):
        observed = 0
        candidates = sorted(
            (root / d).glob("*.json"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:20]
        for p in candidates:
            try:
                doc = json.load(open(p))
            except Exception:  # noqa: BLE001
                continue
            items = _headline_items(doc, None)
            if items and any(isinstance(i, dict) and "passed" in i for i in items):
                observed = len(items)
                break
        if observed:
            # min_items exists so a single-scenario save cannot masquerade
            # as the full battery. Default toward 8, but never above what
            # the freshest real file holds (a suggestion that immediately
            # no-matches its own trigger file would be dead on arrival).
            suggestions.append(
                {"type": "json_headline", "label": "eval results",
                 "patterns": [f"{d}/*.json"],
                 "min_items": max(2, min(8, observed)), "weight": "info"}
            )
            break

    # Process patterns are anchored to the project root: pgrep -af matches
    # the FULL machine-wide command line, so a bare "pytest" would count any
    # checkout's (or any agent sandbox's) test run as this project's
    # activity - false ACTIVE that silently suppresses the watchdog.
    scope = re.escape(str(root))
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists():
        suggestions.append(
            {"type": "process", "label": "test runner",
             "pattern": f"{scope}.*(pytest)", "weight": "primary"}
        )
    elif (root / "package.json").exists():
        suggestions.append(
            {"type": "process", "label": "test runner",
             "pattern": f"{scope}.*(vitest|jest)", "weight": "primary"}
        )

    return suggestions
