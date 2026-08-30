"""ccdoing CLI: init | tick | run | status | view | serve | projects |
doctor | test-escalation | install."""

from __future__ import annotations

import argparse
import importlib.resources
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import yaml

from . import __version__, drift, registry, render, signals, status_json, watchdog
from .config import CONFIG_FILENAME, Config, ConfigError, load_config
from .verdict import compute_verdict

DEFAULT_ESCALATION = [
    {"after_quiet_minutes": 15, "action": "log"},
    {"after_quiet_minutes": 30, "action": "notify"},
]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"ccdoing: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccdoing",
        description="Passive status page + watchdog for Claude Code sessions.",
    )
    p.add_argument("--version", action="version", version=f"ccdoing {__version__}")
    p.add_argument(
        "--config", default=CONFIG_FILENAME,
        help=f"path to config (default: ./{CONFIG_FILENAME})",
    )
    sub = p.add_subparsers(required=True)

    sp = sub.add_parser("init", help="inventory this project and write ccdoing.yaml")
    sp.add_argument("--force", action="store_true", help="overwrite an existing config")
    sp.add_argument(
        "--write-nudge-message", action="store_true",
        help="write .ccdoing/nudge-message.md from the packaged template "
        "(also happens automatically when the config has a nudge tier)",
    )
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("tick", help="one cycle: collect, render, escalate (cron-safe)")
    sp.add_argument("--no-watchdog", action="store_true", help="render only, no escalation")
    sp.set_defaults(func=cmd_tick)

    sp = sub.add_parser("run", help="run the loop in the foreground")
    sp.add_argument("--no-watchdog", action="store_true",
                    help="render only, no escalation")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="print the current verdict JSON to stdout")
    sp.add_argument("--fresh", action="store_true",
                    help="recollect now instead of reading the last snapshot")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser(
        "view",
        help="live terminal status viewer (for ssh/headless boxes)",
    )
    sp.add_argument("--project", metavar="NAME_OR_PATH",
                    help="view a registered project instead of the cwd "
                    "(see `ccdoing projects`)")
    sp.add_argument("--fresh", action="store_true",
                    help="recollect each cycle instead of only re-reading "
                    "status.json (view doubles as the generator)")
    sp.add_argument("--once", action="store_true",
                    help="render one frame and exit (script/pipe friendly)")
    sp.add_argument("--interval", type=float, default=None,
                    help="seconds between redraws (default: refresh_seconds)")
    sp.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    sp.add_argument("--dash", action="store_true",
                    help="all-projects dashboard (also the default when run "
                    "outside any configured project)")
    sp.add_argument("--days", type=float, default=4.0,
                    help="dashboard: only show projects with a signal in the "
                    "last N days (default 4; changeable live with 'd')")
    sp.set_defaults(func=cmd_view)

    sp = sub.add_parser(
        "serve",
        help="serve the status pages over HTTP (start | stop | status)",
        description="Tiny stdlib static server for the status pages. "
        "Binds 127.0.0.1 by default so nothing is exposed unless you "
        "explicitly pass --bind 0.0.0.0; the page shows process names, "
        "session ids, and paths, so treat it as internal before widening. "
        "`serve --daemon` runs it in the background (pidfile-tracked); "
        "`serve stop` / `serve status` manage it.",
    )
    sp.add_argument("action", nargs="?", default="start",
                    choices=["start", "stop", "status"],
                    help="start (default; restarts a running daemon), "
                    "stop, or status")
    sp.add_argument("--port", type=int, default=8377)
    sp.add_argument("--bind", default="127.0.0.1",
                    help="interface to bind (default 127.0.0.1; 0.0.0.0 "
                    "exposes it to your network - your call)")
    sp.add_argument("--all", action="store_true", dest="all_projects",
                    help="serve the all-projects dashboard (cards for every "
                    "registered project, each linking to its live status page)")
    sp.add_argument("--daemon", action="store_true",
                    help="run in the background; manage with "
                    "`ccdoing serve stop` / `ccdoing serve status`")
    sp.add_argument("--project", metavar="NAME_OR_PATH",
                    help="serve a registered project instead of the cwd "
                    "(see `ccdoing projects`)")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("projects", help="list registered ccdoing projects on this machine")
    sp.add_argument(
        "--unregister", metavar="NAME_OR_PATH",
        help="remove a project from the registry (the project itself is untouched)",
    )
    sp.set_defaults(func=cmd_projects)

    sp = sub.add_parser("doctor", help="environment and configuration checks")
    sp.add_argument("--arm-check", action="store_true",
                    help="silent unless the watchdog is configured but not running")
    sp.add_argument("--drift", action="store_true",
                    help="config-drift report: unconfigured-but-detectable signals "
                    "plus configured signals whose targets match nothing")
    sp.add_argument("--quiet", action="store_true",
                    help="with --drift: print one line only when drift exists")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("test-escalation", help="exercise an escalation path safely")
    sp.add_argument("--tier", choices=["log", "notify", "nudge"], required=True)
    sp.add_argument("--real", action="store_true",
                    help="actually send/launch (default is dry-run)")
    sp.set_defaults(func=cmd_test_escalation)

    sp = sub.add_parser("install", help="print watchdog install units (systemd/cron)")
    sp.add_argument("--mode", choices=["systemd", "cron"], default="systemd")
    sp.set_defaults(func=cmd_install)

    return p


# --------------------------------------------------------------------------


def _collect(cfg: Config, now: float):
    ctx = signals.Context(
        project_root=cfg.project_root,
        now=now,
        window_s=cfg.active_window_s,
        exclude_dirs=(cfg.output_dir.resolve(), cfg.state_dir.resolve()),
    )
    readings = signals.collect_all(cfg.signals, ctx)
    verdict = compute_verdict(readings, cfg)
    signal_states = drift.apply_states(
        readings, cfg.state_dir, now, cfg.drift_stale_after_s
    )
    maintenance = drift.maintenance_lines(readings, signal_states)
    state = watchdog.load_state(cfg.state_dir)
    quiet_since = None
    if verdict.state in ("QUIET", "STUCK"):
        # The episode LADDER starts at the first observed quiet tick (so a
        # fresh install can't instantly fire the ladder), but the REPORTED
        # quiet duration is honest EVERY tick: the signals have been quiet
        # since the youngest primary last moved, not since we happened to
        # look. Derived from the readings each time so it never regresses
        # to the episode start on the second tick.
        ages = [
            r.age_s for r in readings
            if r.weight == "primary" and r.ok and r.age_s is not None
        ]
        if ages:
            quiet_since = now - min(ages)
        else:
            quiet_since = state.get("quiet_since") or now
    snap = status_json.build_snapshot(
        readings, verdict, cfg, now, quiet_since,
        signal_states=signal_states, maintenance=maintenance,
    )
    return snap


def _write_outputs(cfg: Config, snap: dict) -> Path:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    # pid-suffixed temp names: a loop tick and a `status --fresh` (or an
    # overlapping cron tick) writing the same fixed .tmp path could
    # interleave a truncate with the other's replace. Each writer gets its
    # own temp file; os.replace stays atomic.
    html_tmp = cfg.output_dir / f"status.html.tmp.{os.getpid()}"
    html_tmp.write_text(render.render_html(snap))
    html_tmp.replace(cfg.output_dir / "status.html")
    json_tmp = cfg.output_dir / f"status.json.tmp.{os.getpid()}"
    json_tmp.write_text(json.dumps(snap, indent=1, default=str))
    json_tmp.replace(cfg.output_dir / "status.json")
    return cfg.output_dir / "status.html"


def cmd_tick(args) -> int:
    cfg = load_config(args.config)
    now = time.time()
    snap = _collect(cfg, now)
    out = _write_outputs(cfg, snap)
    if not args.no_watchdog:
        results = watchdog.evaluate(snap, cfg, now=now)
        for r in results:
            print(f"watchdog {r.tier.after_quiet_minutes:g}m/{r.tier.action}: {r.detail}")
    print(f"{snap['verdict']}: {snap['cause']}")
    print(f"wrote {out} and status.json")
    return 0


def cmd_run(args) -> int:
    import signal as _signal

    cfg = load_config(args.config)
    # The loop paces at the faster of the two knobs so check_interval_seconds
    # is real config, not decoration; doctor reports which one is in effect.
    sleep_s = min(cfg.refresh_seconds, cfg.watchdog.check_interval_seconds) \
        if cfg.watchdog.enabled else cfg.refresh_seconds
    print(f"ccdoing loop: every {sleep_s}s (ctrl-c to stop)")
    pidfile = cfg.state_dir / "run.pid"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))

    def _term(_sig, _frame):  # systemd stop sends SIGTERM: exit cleanly
        raise KeyboardInterrupt

    try:
        _signal.signal(_signal.SIGTERM, _term)
    except (ValueError, OSError):
        pass  # non-main thread / unsupported platform
    try:
        while True:
            cmd_tick(args)
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            pidfile.unlink()
        except OSError:
            pass


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    if args.fresh:
        snap = _collect(cfg, time.time())
    else:
        p = cfg.output_dir / "status.json"
        if not p.is_file():
            snap = _collect(cfg, time.time())
        else:
            snap = json.loads(p.read_text())
    print(json.dumps(snap, indent=1, default=str))
    return 0


def _resolve_view_config(args) -> Config:
    """Config for view/serve: --project via the registry, else the cwd."""
    if getattr(args, "project", None):
        root = registry.find(args.project)
        if root is None:
            known = ", ".join(r.name for r in registry.load()) or "(none registered)"
            raise ConfigError(
                f"no registered project matches {args.project!r}; known: {known}"
            )
        return load_config(root / CONFIG_FILENAME)
    try:
        return load_config(args.config)
    except ConfigError:
        projects = registry.load()
        if not projects:
            raise
        if len(projects) == 1:
            return load_config(projects[0] / CONFIG_FILENAME)
        raise ConfigError(
            "no ccdoing.yaml in the current directory; pick one with "
            "--project (see `ccdoing projects`)"
        )


def cmd_view(args) -> int:
    from . import tui

    if getattr(args, "dash", False):
        return tui.dashboard(
            interval=args.interval or 30.0, days=args.days,
            once=args.once, color=False if args.no_color else None,
        )
    try:
        cfg = _resolve_view_config(args)
    except ConfigError:
        # Outside any configured project with several registered: the
        # dashboard IS the picker (arrow + enter opens a project).
        if not getattr(args, "project", None) and len(registry.load()) > 1:
            return tui.dashboard(
                interval=args.interval or 30.0, days=args.days,
                once=args.once, color=False if args.no_color else None,
            )
        raise
    refresh_fn = None
    if args.fresh:
        def refresh_fn() -> None:  # regenerate on the view's own cadence
            _write_outputs(cfg, _collect(cfg, time.time()))
    return tui.view(
        cfg.output_dir / "status.json",
        interval=args.interval or float(cfg.refresh_seconds),
        refresh_fn=refresh_fn,
        once=args.once,
        color=False if args.no_color else None,
    )


def cmd_serve(args) -> int:
    from . import serve as _serve

    all_projects = getattr(args, "all_projects", False)

    def _candidates() -> list[tuple[str, Path]]:
        """(label, pidfile) pairs stop/status should consider."""
        rows: list[tuple[str, Path]] = []
        try:
            cfg = _resolve_view_config(args)
            rows.append((f"project {cfg.project_name}",
                         _serve.project_pidfile(cfg.state_dir)))
        except ConfigError:
            pass
        rows.append(("all-projects dashboard", _serve.all_pidfile()))
        return rows

    action = getattr(args, "action", "start") or "start"
    if action == "status":
        running = False
        for label, pf in _candidates():
            info = _serve.read_pidfile(pf)
            if info:
                print(f"{label}: running (pid {info['pid']}) - {info['url']}")
                running = True
        if not running:
            print("serve: not running")
        return 0
    if action == "stop":
        stopped = False
        for label, pf in _candidates():
            msg = _serve.stop_daemon(pf)
            if msg != "not running":
                print(f"{label}: {msg}")
                stopped = True
        if not stopped:
            print("serve: nothing running")
        return 0

    # start
    if all_projects:
        pidfile = _serve.all_pidfile()
        outdir = None
    else:
        cfg = _resolve_view_config(args)
        pidfile = _serve.project_pidfile(cfg.state_dir)
        outdir = cfg.output_dir
    if getattr(args, "daemon", False):
        child = (["--all"] if all_projects else []) + [
            "--port", str(args.port), "--bind", args.bind,
        ]
        if not all_projects and getattr(args, "project", None):
            child += ["--project", args.project]
        # The respawned child has its own cwd; point it at the exact config
        # file the parent resolved, or it silently serves whatever project
        # (if any) its cwd happens to contain.
        global_args: list[str] = []
        if not all_projects and cfg.source_path is not None:
            global_args = ["--config", str(cfg.source_path)]
        info = _serve.start_daemon(
            child, pidfile, pidfile.with_suffix(".log"), global_args=global_args
        )
        if info is None:
            return 1
        print(f"serve daemon running (pid {info['pid']})")
        _serve._print_urls(info["url"], daemonized=True)
        return 0
    prior = _serve.read_pidfile(pidfile)
    if prior is not None:
        print(f"already running (pid {prior['pid']}) - {prior['url']}")
        print("`ccdoing serve stop` first, or use --daemon to restart it")
        return 1
    if all_projects:
        return _serve.serve_all(port=args.port, bind=args.bind, pidfile=pidfile)
    return _serve.serve(outdir, port=args.port, bind=args.bind, pidfile=pidfile)


def cmd_projects(args) -> int:
    if getattr(args, "unregister", None):
        removed = registry.unregister(args.unregister)
        if removed is None:
            print(f"no registered project matches {args.unregister!r}")
            return 1
        print(f"unregistered {removed} (the project itself is untouched)")
        return 0
    projects = registry.load()
    if not projects:
        print("no registered projects (run `ccdoing init` in a project to register it)")
        return 0
    rows = []
    for root in projects:
        verdict, epoch = "-", None
        snap_p = None
        try:
            cfg = load_config(root / CONFIG_FILENAME)
            snap_p = cfg.output_dir / "status.json"
        except ConfigError:
            pass
        if snap_p and snap_p.is_file():
            try:
                snap = json.loads(snap_p.read_text())
                verdict = str(snap.get("verdict", "-"))
                e = snap.get("generated_epoch")
                epoch = float(e) if isinstance(e, (int, float)) else None
            except (OSError, json.JSONDecodeError, ValueError):
                verdict = "unreadable"
        rows.append((root, verdict, epoch))
    # Most recently generated first; never-generated last, stable by name.
    rows.sort(key=lambda r: (-(r[2] or 0), r[0].name))
    from .tui import fmt_age
    for root, verdict, epoch in rows:
        age = f" ({fmt_age(time.time() - epoch)} ago)" if epoch else ""
        print(f"  {root.name:<24} {verdict}{age}  {root}")
    return 0


def nudge_template() -> str:
    """The packaged nudge message template (ships inside the wheel)."""
    return (
        importlib.resources.files("whats_cc_doing")
        .joinpath("templates/nudge-message.md")
        .read_text()
    )


def write_nudge_message(cfg_doc: dict, root: Path) -> Path:
    """Render the packaged template into .ccdoing/nudge-message.md."""
    minutes = 45
    for t in (cfg_doc.get("watchdog") or {}).get("escalation") or []:
        if isinstance(t, dict) and t.get("action") == "nudge":
            minutes = t.get("after_quiet_minutes", 45)
    text = (
        nudge_template()
        .replace("{{PROJECT_NAME}}", str(cfg_doc.get("project_name", root.name)))
        .replace("{{AFTER_QUIET_MINUTES}}", str(minutes))
        .replace(
            "{{STATUS_JSON_PATH}}",
            str(Path(cfg_doc.get("output_dir", "reports/status")) / "status.json"),
        )
    )
    dest = root / ".ccdoing" / "nudge-message.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    return dest


def cmd_init(args) -> int:
    root = Path.cwd()
    target = root / args.config
    if target.exists() and not args.force:
        if args.write_nudge_message:
            doc = yaml.safe_load(target.read_text()) or {}
            dest = write_nudge_message(doc, root)
            print(f"wrote {dest} - review and edit it; the watchdog only ever "
                  "sends this file's current contents")
            return 0
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    detected = signals.detect_signals(root)
    doc = {
        "version": 1,
        "project_name": root.name,
        "output_dir": "reports/status",
        "refresh_seconds": 30,
        # health_failure_is_down defaults OFF in generated configs: dev
        # servers that are only sometimes running would otherwise scream
        # DOWN forever AND reset the quiet ladder. Turn it on for
        # endpoints that are expected to be always-up.
        "verdict": {"active_window_minutes": 15, "health_failure_is_down": False},
        "signals": detected
        or [{"type": "git", "label": "git commits", "weight": "primary"}],
        "watchdog": {
            "enabled": True,
            "check_interval_seconds": 60,
            "escalation": DEFAULT_ESCALATION,
            "nudge_message": ".ccdoing/nudge-message.md",
            "notify_urls_file": ".ccdoing/notify.urls",
        },
    }
    target.write_text(yaml.safe_dump(doc, sort_keys=False))
    print(f"wrote {target} with {len(doc['signals'])} detected signal(s):")
    for s in doc["signals"]:
        print(f"  - {s['type']}: {s.get('label')} [{s.get('weight', 'primary')}]")
    has_nudge = any(t.get("action") == "nudge" for t in DEFAULT_ESCALATION)
    if args.write_nudge_message or has_nudge:
        dest = write_nudge_message(doc, root)
        print(f"wrote {dest} - review it; the watchdog only ever sends this "
              "file's current contents")
    if any(t.get("action") == "notify" for t in DEFAULT_ESCALATION):
        nf = watchdog.scaffold_notify_urls_file(root, ".ccdoing/notify.urls")
        print(f"notify targets file: {nf} (one apprise URL per line; "
              f"$CCDOING_NOTIFY_URLS overrides it when set)")
    _print_gitignore_hint(root)
    registry.register(root)  # so `ccdoing projects` / `view --project` find it
    print("review it, then: ccdoing tick")
    return 0


def _print_gitignore_hint(root: Path) -> None:
    """Suggest (never silently edit) .gitignore coverage for the state dir.
    Everything under .ccdoing/ is runtime state or secrets (notify.urls
    holds topics/webhooks) EXCEPT nudge-message.md, which is reviewable
    config worth committing."""
    if not (root / ".git").exists():
        return
    gi = root / ".gitignore"
    try:
        text = gi.read_text() if gi.is_file() else ""
    except OSError:
        text = ""
    if ".ccdoing" in text:
        return
    print("suggested .gitignore additions (state + notify secrets stay "
          "untracked; the nudge message stays committable):")
    print("  .ccdoing/*")
    print("  !.ccdoing/nudge-message.md")


def _doctor_drift(args) -> int:
    """Config-drift report. Always exits 0 - informational, never gating."""
    quiet = getattr(args, "quiet", False)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        if not quiet:
            print(f"ccdoing: {exc}")
        return 0

    # Configured-but-dead signals: prefer the last snapshot (cheap, and the
    # hook must stay fast); recollect only when no snapshot exists yet.
    unhealthy: list[str] = []
    snap_p = cfg.output_dir / "status.json"
    snap = None
    if snap_p.is_file():
        try:
            snap = json.loads(snap_p.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            snap = None
    if snap is None:
        snap = _collect(cfg, time.time())
    for sig in snap.get("signals", []):
        st = sig.get("state", "ok")
        if st != "ok":
            unhealthy.append(
                f"{sig.get('label')} ({sig.get('type')}): {st} - "
                f"{sig.get('detail') or sig.get('error') or ''}"
            )

    # Detectable-but-unconfigured: re-run the init inventory and diff.
    candidates = drift.inventory_drift(cfg.project_root, cfg.signals)

    n = len(unhealthy) + len(candidates)
    if quiet:
        if n:
            print(
                f"ccdoing: config drift detected ({n} finding(s)) - "
                "run `ccdoing doctor --drift` for details, or /ccdoing:tune to fix"
            )
        return 0
    if not n:
        print("no drift detected: configured signals are matching, and the "
              "inventory found nothing unconfigured")
        return 0
    if unhealthy:
        print("configured signals whose targets are not matching:")
        for u in unhealthy:
            print(f"  - {u}")
    if candidates:
        print("detectable in this project but not configured:")
        for c in candidates:
            extras = {k: v for k, v in c.items() if k not in ("type", "label", "weight")}
            hint = f"  {extras}" if extras else ""
            print(f"  - {c['type']}: {c.get('label')}{hint}")
    print("to apply changes: /ccdoing:tune (proposes deltas; never rewrites "
          "your tuned config wholesale)")
    return 0


def cmd_doctor(args) -> int:
    if getattr(args, "drift", False):
        return _doctor_drift(args)
    problems: list[str] = []
    notes: list[str] = []

    cfg = None
    try:
        cfg = load_config(args.config)
        notes.append(f"config ok: {cfg.project_name}, {len(cfg.signals)} signal(s)")
    except ConfigError as exc:
        problems.append(str(exc))

    for binary, why in [("git", "git signal"), ("pgrep", "process signal")]:
        if shutil.which(binary) is None:
            notes.append(f"missing {binary} ({why} degraded)")
    if shutil.which("claude") is None:
        notes.append("claude CLI not on PATH (nudge tier unavailable)")

    armed = False
    if cfg is not None:
        try:
            state = watchdog.load_state(cfg.state_dir)
            # "Armed" distinguishes a live loop (run.pid alive) from
            # recent ticks (manual or scheduled): a single manual tick
            # during setup is NOT arming, and doctor says which it sees.
            loop_pid = None
            pidfile = cfg.state_dir / "run.pid"
            if pidfile.is_file():
                try:
                    pid = int(pidfile.read_text().strip())
                    os.kill(pid, 0)
                    loop_pid = pid
                except (ValueError, ProcessLookupError, OSError):
                    loop_pid = None
            snap_p = cfg.output_dir / "status.json"
            recent_ticks = False
            if snap_p.is_file():
                age = time.time() - os.path.getmtime(snap_p)
                recent_ticks = age < max(300, cfg.watchdog.check_interval_seconds * 5)
                if loop_pid:
                    notes.append(f"loop running (pid {loop_pid}); last tick {int(age)}s ago")
                elif recent_ticks:
                    notes.append(
                        f"last tick {int(age)}s ago (manual or scheduled ticks); "
                        "no foreground loop detected"
                    )
                else:
                    notes.append(f"last tick {int(age)}s ago (NOT running)")
            else:
                notes.append("no status.json yet (never ticked)")
            armed = bool(loop_pid) or recent_ticks
            if cfg.watchdog.enabled:
                pace = min(cfg.refresh_seconds, cfg.watchdog.check_interval_seconds)
                notes.append(f"loop pace: {pace}s (min of refresh_seconds and "
                             "watchdog.check_interval_seconds)")
            if state.get("quiet_since"):
                notes.append("currently in a quiet episode")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"state unreadable: {exc}")

    if args.arm_check:
        # For the SessionStart hook: silent when healthy, one line when not.
        if cfg is not None and cfg.watchdog.enabled and not armed:
            print(
                "ccdoing: watchdog is configured but not running - "
                "start it (ccdoing run) or install it (ccdoing install)"
            )
        return 0

    for n in notes:
        print(f"  ok/note: {n}")
    for p in problems:
        print(f"  PROBLEM: {p}")
    return 1 if problems else 0


def cmd_test_escalation(args) -> int:
    cfg = load_config(args.config)
    dry = not args.real
    snap = _collect(cfg, time.time())
    if args.tier == "log":
        watchdog.log_line(cfg.state_dir, "TEST escalation (tier log)")
        print(f"appended TEST line to {cfg.state_dir / watchdog.LOG_FILE}")
        return 0
    if args.tier == "notify":
        urls, source = watchdog.resolve_notify_urls(cfg)
        if urls:
            print(f"notify targets ({source}):")
            for u in urls:
                link = watchdog.ntfy_subscribe_link(u)
                print(f"  - {u}" + (f"  (subscribe: {link})" if link else ""))
        detail = watchdog.send_notification(
            cfg,
            title=f"[ccdoing] TEST from {cfg.project_name}",
            body="This is a test of the ccdoing notify path. No action needed.",
            dry_run=dry,
        )
        print(detail)
        return 0
    # nudge
    tier = None
    for t in cfg.watchdog.escalation:
        if t.action == "nudge":
            tier = t
            break
    if tier is None:
        from .config import EscalationTier

        tier = EscalationTier(after_quiet_minutes=0, action="nudge")
    state = watchdog.load_state(cfg.state_dir)
    result = watchdog._nudge(  # noqa: SLF001 - deliberate test entry
        tier, snap, cfg, state, time.time(),
        dry_run=dry, runner=None,
    )
    print(result.detail)
    if dry:
        # Show exactly what would launch: the evidence bundle is the part
        # a user cannot otherwise preview before 3am.
        print("\n--- evidence bundle that would be appended ---")
        print(watchdog.build_evidence(snap, cfg))
    elif result.fired:
        # A --real launch consumed cooldown/daily-cap accounting: persist it
        # so real test launches don't bypass the rails.
        watchdog.save_state(cfg.state_dir, state)
    return 0


def cmd_install(args) -> int:
    cfg = load_config(args.config)
    root = cfg.project_root
    # systemd and cron both run with a minimal PATH; a venv/pipx/uv-tool
    # `ccdoing` is not on it, so always emit the resolved absolute path.
    ccdoing_bin = shutil.which("ccdoing") or sys.argv[0] or "ccdoing"
    if args.mode == "cron":
        import shlex

        print("# add with: crontab -e")
        print("# (absolute path: cron's PATH is /usr/bin:/bin, which will not"
              " include venv/uv-tool installs)")
        print(
            f"* * * * * cd {shlex.quote(str(root))} && "
            f"{shlex.quote(ccdoing_bin)} tick >> "
            f"{shlex.quote(str(cfg.state_dir / 'cron.log'))} 2>&1"
        )
        return 0
    # A project name with spaces/oddities would make a broken unit filename
    # and unquoted ExecStart; sanitize the name, quote the exec path.
    unit_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", cfg.project_name) or "project"
    exec_bin = f'"{ccdoing_bin}"' if " " in ccdoing_bin else ccdoing_bin
    unit = f"""[Unit]
Description=ccdoing status watchdog for {cfg.project_name}
After=network.target

[Service]
WorkingDirectory={root}
ExecStart={exec_bin} run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    print("# save as ~/.config/systemd/user/ccdoing-" + unit_name + ".service")
    print("# then: systemctl --user daemon-reload && systemctl --user enable --now "
          f"ccdoing-{unit_name}")
    print("# headless boxes also need: loginctl enable-linger $USER")
    print(unit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
