"""Regression tests for the publish-readiness review findings."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from whats_cc_doing import __version__, cli, drift, watchdog
from whats_cc_doing.config import (
    Config,
    EscalationTier,
    VerdictConfig,
    WatchdogConfig,
    load_config,
)
from whats_cc_doing.signals import Reading

from .conftest import NOW, touch

REPO = Path(__file__).resolve().parent.parent


# -- version discipline ----------------------------------------------------


def test_version_matches_in_all_three_places():
    py = tomllib.loads((REPO / "pyproject.toml").read_text())
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    assert py["project"]["version"] == __version__ == plugin["version"], (
        "pyproject.toml, __init__.__version__, and plugin.json must carry "
        "the same version - bump all three together"
    )


# -- serve --daemon forwards --config --------------------------------------


def test_daemon_child_argv_carries_resolved_config(tmp_path, monkeypatch):
    proj = tmp_path / "proj with spaces"
    proj.mkdir()
    (proj / "ccdoing.yaml").write_text("version: 1\nsignals: []\n")
    cfg = load_config(proj / "ccdoing.yaml")
    assert cfg.source_path == (proj / "ccdoing.yaml").resolve()

    captured = {}

    def fake_start(child, pidfile, log_path, timeout=5.0, global_args=None):
        captured["child"] = child
        captured["global_args"] = global_args
        return {"pid": 1, "url": "http://x"}

    from whats_cc_doing import serve as _serve

    monkeypatch.setattr(_serve, "start_daemon", fake_start)
    monkeypatch.setattr(cli, "_resolve_view_config", lambda args: cfg)

    args = cli.build_parser().parse_args(
        ["serve", "start", "--daemon", "--port", "0"]
    )
    rc = args.func(args)
    assert rc == 0
    assert captured["global_args"] == ["--config", str(cfg.source_path)]


def test_start_daemon_places_global_args_before_serve(monkeypatch, tmp_path):
    from whats_cc_doing import serve as _serve

    recorded = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return 1  # dies immediately; start_daemon returns None

    def fake_popen(argv, **kw):
        recorded["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(_serve.subprocess, "Popen", fake_popen)
    pidfile = tmp_path / "serve.pid"
    _serve.start_daemon(
        ["--port", "0"], pidfile, tmp_path / "serve.log", timeout=0.2,
        global_args=["--config", "/abs/ccdoing.yaml"],
    )
    argv = recorded["argv"]
    assert argv[argv.index("--config"):][:2] == ["--config", "/abs/ccdoing.yaml"]
    assert argv.index("--config") < argv.index("serve")


# -- structured retryable escalation ---------------------------------------


def _nudge_cfg(project: Path) -> Config:
    project.mkdir(parents=True, exist_ok=True)
    return Config(
        project_name="p",
        project_root=project,
        output_dir=project / "reports" / "status",
        verdict=VerdictConfig(active_window_minutes=15),
        watchdog=WatchdogConfig(
            enabled=True,
            escalation=[
                EscalationTier(
                    after_quiet_minutes=45, action="nudge",
                    cooldown_minutes=60, max_per_day=2,
                )
            ],
        ),
    )


def _stuck_snap():
    return {
        "verdict": "STUCK",
        "cause": "dead wait",
        "stuck_session_ids": ["abc123-session"],
        "quiet_for_seconds": 46 * 60,
        "generated_at": "t",
        "signals": [],
    }


def test_transient_launch_failure_does_not_consume_tier(tmp_path, monkeypatch):
    cfg = _nudge_cfg(tmp_path / "proj")
    monkeypatch.setattr(
        watchdog, "_project_claude_pids", lambda root: [1234]
    )
    msg = cfg.project_root / ".ccdoing" / "nudge-message.md"
    msg.parent.mkdir(parents=True)
    msg.write_text("ignore this if you are fine")
    watchdog.save_state(
        cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": []}
    )

    def boom(cmd, c):
        raise OSError("claude not on PATH")

    [r] = watchdog.evaluate(
        _stuck_snap(), cfg, now=NOW, runner=boom, prober=lambda cmd, c: "NO_NOTICE"
    )
    assert not r.fired and r.retryable
    st = json.loads((cfg.state_dir / "state.json").read_text())
    assert st["fired_tiers"] == [], "a failed launch must leave the tier armed"
    # and the daily cap was not consumed
    assert st.get("nudge", {}).get("count", 0) == 0


def test_retry_semantics_do_not_depend_on_detail_wording(tmp_path, monkeypatch):
    cfg = _nudge_cfg(tmp_path / "proj")
    watchdog.save_state(
        cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": []}
    )

    reworded = watchdog.ActionResult(
        tier=cfg.watchdog.escalation[0], fired=False,
        detail="totally reworded skip text with no known prefix",
        retryable=True,
    )
    monkeypatch.setattr(watchdog, "_fire", lambda *a, **k: reworded)
    watchdog.evaluate(_stuck_snap(), cfg, now=NOW)
    st = json.loads((cfg.state_dir / "state.json").read_text())
    assert st["fired_tiers"] == []

    consumed = watchdog.ActionResult(
        tier=cfg.watchdog.escalation[0], fired=False,
        detail="skipped: cooldown - but flagged NOT retryable", retryable=False,
    )
    monkeypatch.setattr(watchdog, "_fire", lambda *a, **k: consumed)
    watchdog.evaluate(_stuck_snap(), cfg, now=NOW)
    st = json.loads((cfg.state_dir / "state.json").read_text())
    assert st["fired_tiers"] == [45.0], (
        "the structured flag, not the wording, decides consumption"
    )


def test_no_detail_string_matching_left_in_engine():
    src = (REPO / "src" / "whats_cc_doing" / "watchdog.py").read_text()
    fn = src.split("def _evaluate_locked", 1)[1].split("\ndef ", 1)[0]
    assert "startswith" not in fn


# -- install output quoting -------------------------------------------------


def test_cron_install_quotes_spaced_paths(tmp_path, capsys, monkeypatch):
    proj = tmp_path / "my project"
    proj.mkdir()
    (proj / "ccdoing.yaml").write_text("version: 1\nsignals: []\n")
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/opt/some dir/bin/ccdoing"
    )
    args = cli.build_parser().parse_args(
        ["--config", str(proj / "ccdoing.yaml"), "install", "--mode", "cron"]
    )
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert f"cd '{proj}'" in out
    assert "'/opt/some dir/bin/ccdoing' tick" in out


def test_systemd_install_sanitizes_unit_name(tmp_path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ccdoing.yaml").write_text(
        "version: 1\nproject_name: 'My Cool App!'\nsignals: []\n"
    )
    args = cli.build_parser().parse_args(
        ["--config", str(proj / "ccdoing.yaml"), "install"]
    )
    assert args.func(args) == 0
    out = capsys.readouterr().out
    m = re.search(r"ccdoing-([^\s.]+)\.service", out)
    assert m and re.fullmatch(r"[A-Za-z0-9_.-]+", m.group(1))


# -- quiet duration must not regress after the first tick -------------------


def test_quiet_duration_persists_across_ticks(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    stale = proj / "thing.txt"
    touch(stale, NOW - 3600)  # last primary movement an hour ago
    cfg = Config(
        project_name="p",
        project_root=proj,
        output_dir=proj / "reports" / "status",
        verdict=VerdictConfig(active_window_minutes=15),
        watchdog=WatchdogConfig(enabled=True, escalation=[]),
        signals=[{"type": "file_mtime", "label": "files", "glob": "thing.txt"}],
    )
    import time as _time

    snap1 = cli._collect(cfg, NOW)
    assert snap1["verdict"] == "QUIET"
    assert snap1["quiet_for_seconds"] and snap1["quiet_for_seconds"] >= 3600 - 1
    watchdog.evaluate(snap1, cfg, now=NOW)  # starts the episode in state

    snap2 = cli._collect(cfg, NOW + 60)
    assert snap2["quiet_for_seconds"] >= 3660 - 1, (
        "second tick must keep reporting quiet-since-signal-movement, not "
        "reset to quiet-since-episode-start"
    )


# -- drift keys must not collide for duplicate type:label -------------------


def test_drift_states_distinct_for_duplicate_type_label(tmp_path):
    r_ok = Reading(
        label="files", type="file_mtime", weight="primary", ok=True,
        fresh=True, matched=True,
    )
    r_nomatch = Reading(
        label="files", type="file_mtime", weight="primary", ok=True,
        fresh=False, matched=False,
    )
    states = drift.apply_states([r_ok, r_nomatch], tmp_path, NOW, 7 * 86400)
    assert len(states) == 2
    assert set(states.values()) == {"ok", "no-match"}
