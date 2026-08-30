"""Tests for the headless-box surface: registry, serve, and the TUI view."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

from whats_cc_doing import registry, serve, tui
from whats_cc_doing.cli import main


def _make_project(root: Path, name: str = "proj") -> Path:
    p = root / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "ccdoing.yaml").write_text(
        "version: 1\nsignals:\n- type: git\n  label: git commits\n"
    )
    return p


# -- registry ---------------------------------------------------------------


def test_registry_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    a = _make_project(tmp_path, "alpha")
    b = _make_project(tmp_path, "beta")
    registry.register(a)
    registry.register(b)
    registry.register(a)  # idempotent
    roots = registry.load()
    assert roots == sorted([a, b], key=str) or set(roots) == {a, b}
    assert len(roots) == 2


def test_registry_drops_stale_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    a = _make_project(tmp_path, "alpha")
    gone = _make_project(tmp_path, "gone")
    noconf = _make_project(tmp_path, "noconf")
    for p in (a, gone, noconf):
        registry.register(p)
    (gone / "ccdoing.yaml").unlink()
    gone.rmdir()
    (noconf / "ccdoing.yaml").unlink()  # dir exists, config removed
    assert registry.load() == [a]
    # and the stale entries were rewritten out of the file itself
    assert json.loads(registry.registry_path().read_text()) == [str(a)]


def test_registry_find(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    a = _make_project(tmp_path, "alpha")
    registry.register(a)
    assert registry.find("alpha") == a
    assert registry.find(str(a)) == a.resolve()
    assert registry.find("alp") == a  # unique substring
    assert registry.find("nope") is None


def test_registry_corrupt_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    registry.registry_path().parent.mkdir(parents=True)
    registry.registry_path().write_text("{not json")
    assert registry.load() == []


# -- serve ------------------------------------------------------------------


def test_serve_no_store_headers(tmp_path):
    (tmp_path / "status.html").write_text("<title>ok</title>")
    httpd = serve.make_server(tmp_path, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status.html") as resp:
            assert resp.status == 200
            assert resp.headers["Cache-Control"] == "no-store"
            assert b"ok" in resp.read()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/missing.html")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
            assert e.headers["Cache-Control"] == "no-store"
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- tui --------------------------------------------------------------------


def _snapshot(now: float) -> dict:
    return {
        "project": "demo",
        "generated_epoch": now - 12,
        "refresh_seconds": 30,
        "verdict": "ACTIVE",
        "cause": "activity on: git commits",
        "quiet_for_seconds": None,
        "maintenance": ["claude sessions (claude_session): no-match"],
        "signals": [
            {"label": "git commits", "type": "git", "weight": "primary",
             "state": "ok", "ok": True, "fresh": True, "healthy": None,
             "age_seconds": 30.0, "sessions": []},
            {"label": "test runner", "type": "process", "weight": "primary",
             "state": "ok", "ok": True, "fresh": False, "healthy": None,
             "age_seconds": 69000.0, "sessions": []},
            {"label": "api health", "type": "http", "weight": "health",
             "state": "ok", "ok": True, "fresh": True, "healthy": True,
             "age_seconds": 0.0, "sessions": []},
            {"label": "claude sessions", "type": "claude_session",
             "weight": "primary", "state": "no-match", "ok": True,
             "fresh": False, "healthy": None, "age_seconds": None,
             "sessions": [{"state": "WORKING"}, {"state": "IDLE"},
                          {"state": "IDLE"}]},
        ],
    }


def test_render_frame_smoke():
    now = time.time()
    frame = tui.render_frame(_snapshot(now), width=100, color=False, now=now)
    assert "ACTIVE - activity on: git commits" in frame
    assert "git commits" in frame and "ACTIVE" in frame
    assert "inactive (19h 10m ago)" in frame  # human age, space-separated
    assert "api health" in frame and "UP" in frame
    assert "no-match - config?" in frame
    assert "sessions: 1 WORKING, 2 IDLE" in frame
    assert "drift: 1 finding(s)" in frame
    assert "updated 12s ago" in frame
    assert "\x1b[" not in frame  # color disabled


def test_render_frame_missing_snapshot():
    frame = tui.render_frame(None, width=80, color=False, source="x/status.json")
    assert "no status.json yet" in frame
    assert "ccdoing tick" in frame


def test_render_frame_quiet_banner():
    now = time.time()
    snap = _snapshot(now)
    snap["verdict"] = "QUIET"
    snap["cause"] = "no activity"
    snap["quiet_for_seconds"] = 4520
    frame = tui.render_frame(snap, width=80, color=False, now=now)
    assert "QUIET - no activity (for 1h 15m)" in frame


def test_fmt_age_human_style():
    assert tui.fmt_age(42) == "42s"
    assert tui.fmt_age(69000) == "19h 10m"
    assert tui.fmt_age(3600) == "1h"
    assert tui.fmt_age(90000) == "1d 1h"
    assert tui.fmt_age(None) == "?"


def test_view_once_noncolor(tmp_path, capsys):
    now = time.time()
    p = tmp_path / "status.json"
    p.write_text(json.dumps(_snapshot(now)))
    rc = tui.view(p, interval=30, once=True, color=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "What's CC Doing" in out  # no title configured -> plain fallback, no slug concat


# -- cli wiring -------------------------------------------------------------


def test_cli_projects_lists_registered(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    proj = _make_project(tmp_path, "alpha")
    out_dir = proj / "reports" / "status"
    out_dir.mkdir(parents=True)
    (out_dir / "status.json").write_text(json.dumps(
        {"verdict": "ACTIVE", "generated_epoch": time.time() - 60}
    ))
    registry.register(proj)
    rc = main(["projects"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out and "ACTIVE" in out and "1m ago" in out


def test_cli_view_once_via_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    proj = _make_project(tmp_path, "alpha")
    out_dir = proj / "reports" / "status"
    out_dir.mkdir(parents=True)
    (out_dir / "status.json").write_text(json.dumps(_snapshot(time.time())))
    registry.register(proj)
    monkeypatch.chdir(tmp_path)  # not a project dir
    rc = main(["view", "--project", "alpha", "--once", "--no-color"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "What's CC Doing" in out  # no title configured -> plain fallback, no slug concat


def test_unregister_by_name_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    proj = tmp_path / "projA"
    proj.mkdir()
    (proj / "ccdoing.yaml").write_text("version: 1\nproject_name: a\n")
    registry.register(proj)
    assert registry.load() == [proj.resolve()]
    assert registry.unregister("projA") is not None
    assert registry.load() == []
    assert registry.unregister("nope") is None


def test_projects_sorted_by_recency(tmp_path, monkeypatch, capsys):
    import json as _json
    import time as _time
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    now = _time.time()
    for name, age in (("older", 500), ("newer", 5)):
        proj = tmp_path / name
        (proj / "reports" / "status").mkdir(parents=True)
        (proj / "ccdoing.yaml").write_text(
            f"version: 1\nproject_name: {name}\n"
        )
        (proj / "reports" / "status" / "status.json").write_text(
            _json.dumps({"verdict": "ACTIVE", "generated_epoch": now - age})
        )
        registry.register(proj)
    from whats_cc_doing import cli
    assert cli.main(["projects"]) == 0
    out = capsys.readouterr().out
    assert out.index("newer") < out.index("older")


# ------------------------------------------------------- serve daemon control


def test_pidfile_roundtrip_and_stale_cleanup(tmp_path):
    from whats_cc_doing import serve

    pf = tmp_path / "serve.pid"
    serve.write_pidfile(pf, port=8123, bind="127.0.0.1",
                        url="http://127.0.0.1:8123/", all_projects=True)
    info = serve.read_pidfile(pf)
    assert info and info["port"] == 8123 and info["pid"] == __import__("os").getpid()

    # stale pid -> treated as not running AND the file is cleaned up
    pf.write_text('{"pid": 99999999, "port": 1, "bind": "x", "url": "u"}')
    assert serve.read_pidfile(pf) is None
    assert not pf.exists()
    # garbage file -> None, no crash
    pf.write_text("not json")
    assert serve.read_pidfile(pf) is None


def test_stop_daemon_not_running(tmp_path):
    from whats_cc_doing import serve

    assert serve.stop_daemon(tmp_path / "nope.pid") == "not running"


def test_daemon_start_status_stop_lifecycle(tmp_path):
    """Real lifecycle: spawn the detached server, hit it, stop it.

    The pidfile must be the one the CHILD computes for itself (the
    all-projects one under isolated XDG state) - parent and child agree
    by construction, and this test would catch a divergence."""
    import os
    import time
    import urllib.request

    from whats_cc_doing import serve

    pf = serve.all_pidfile()
    log = pf.with_suffix(".log")
    info = serve.start_daemon(["--all", "--port", "0"], pf, log, timeout=10.0)
    assert info is not None, log.read_text() if log.exists() else "no log"
    try:
        assert info["port"] > 0 and str(info["port"]) in info["url"]
        with urllib.request.urlopen(info["url"], timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers.get("Cache-Control") == "no-store"
        # status: pidfile readable while running
        assert serve.read_pidfile(pf)["pid"] == info["pid"]
        msg = serve.stop_daemon(pf)
        assert msg.startswith("stopped")
        # In-test the daemon is OUR child: reap it, then it must be gone
        # (in real use the CLI parent exits and init reaps).
        try:
            os.waitpid(info["pid"], 0)
        except ChildProcessError:
            pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(info["pid"], 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("daemon process survived stop")
        assert not pf.exists()
    finally:
        serve.stop_daemon(pf)  # belt and braces on failure paths


def test_daemon_restart_replaces_previous(tmp_path):
    from whats_cc_doing import serve

    pf = serve.all_pidfile()
    log = pf.with_suffix(".log")
    first = serve.start_daemon(["--all", "--port", "0"], pf, log, timeout=10.0)
    assert first is not None
    try:
        second = serve.start_daemon(["--all", "--port", "0"], pf, log,
                                    timeout=10.0)
        assert second is not None and second["pid"] != first["pid"]
        assert serve.read_pidfile(pf)["pid"] == second["pid"]
    finally:
        serve.stop_daemon(pf)
