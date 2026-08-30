from __future__ import annotations

import http.server
import threading
from pathlib import Path

from whats_cc_doing import signals
from whats_cc_doing.signals import Context

from .conftest import NOW, touch


def ctx(root: Path, now: float = NOW, window_s: float = 900) -> Context:
    return Context(project_root=root, now=now, window_s=window_s)


# -- git -------------------------------------------------------------------


def test_git_fresh_and_lines(git_repo):
    import os

    r = signals.collect_git({"type": "git"}, ctx(git_repo, now=os.path.getmtime(git_repo / ".git")))
    assert r.ok and r.type == "git"
    assert r.fresh is True
    assert r.lines and "first" in r.lines[0]


def test_git_not_a_repo(tmp_path):
    r = signals.collect_git({"type": "git"}, ctx(tmp_path))
    assert not r.ok
    assert "no commits" in r.error


# -- process ---------------------------------------------------------------


def test_process_matches_own_python(project):
    project.mkdir(parents=True, exist_ok=True)
    r = signals.collect_process({"type": "process", "pattern": "python"}, ctx(project))
    assert r.ok
    assert r.fresh is True  # the test runner itself is a python process


def test_process_no_match(project):
    project.mkdir(parents=True, exist_ok=True)
    r = signals.collect_process(
        {"type": "process", "pattern": "definitely-not-a-real-proc-xyz"}, ctx(project)
    )
    assert r.ok and r.fresh is False and "not running" in r.detail


def test_process_requires_pattern(project):
    project.mkdir(parents=True, exist_ok=True)
    assert not signals.collect_process({"type": "process"}, ctx(project)).ok


# -- file_mtime ------------------------------------------------------------


def test_file_mtime_fresh_and_stale(project):
    project.mkdir(parents=True, exist_ok=True)
    touch(project / "out" / "a.txt", NOW - 60)
    touch(project / "out" / "b.txt", NOW - 5000)
    r = signals.collect_file_mtime(
        {"type": "file_mtime", "glob": "out/*"}, ctx(project)
    )
    assert r.ok and r.fresh is True and r.age_s < 120
    r2 = signals.collect_file_mtime(
        {"type": "file_mtime", "glob": "out/*"}, ctx(project, window_s=30)
    )
    assert r2.fresh is False


def test_file_mtime_no_matches(project):
    project.mkdir(parents=True, exist_ok=True)
    r = signals.collect_file_mtime({"type": "file_mtime", "glob": "nope/*"}, ctx(project))
    assert r.ok and r.fresh is False and "no matching" in r.detail


# -- http ------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        code = 200 if self.path == "/ok" else 500
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"x")

    def log_message(self, *a):  # silence
        pass


def _serve():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def test_http_healthy_and_unhealthy(project):
    project.mkdir(parents=True, exist_ok=True)
    srv = _serve()
    port = srv.server_address[1]
    try:
        ok = signals.collect_http(
            {"type": "http", "url": f"http://127.0.0.1:{port}/ok"}, ctx(project)
        )
        assert ok.ok and ok.healthy is True and ok.weight == "health"
        # urllib raises on 500 -> handled as unhealthy, not an exception
        bad = signals.collect_http(
            {"type": "http", "url": f"http://127.0.0.1:{port}/boom"}, ctx(project)
        )
        assert bad.healthy is False
    finally:
        srv.shutdown()


def test_http_unreachable_is_unhealthy_not_error(project):
    project.mkdir(parents=True, exist_ok=True)
    r = signals.collect_http(
        {"type": "http", "url": "http://127.0.0.1:9/none", "timeout_s": 0.3},
        ctx(project),
    )
    assert r.ok and r.healthy is False and "unreachable" in r.detail


# -- log_tail --------------------------------------------------------------


def test_log_tail_hits_and_quiet(project):
    project.mkdir(parents=True, exist_ok=True)
    log = project / "app.log"
    log.write_text("fine\nERROR boom\nfine\nTraceback (most recent call last)\n")
    touch(log, NOW - 10)
    r = signals.collect_log_tail({"type": "log_tail", "path": "app.log"}, ctx(project))
    assert r.ok and r.fresh is True and "2 matching" in r.detail
    touch(log, NOW - 7200)
    r2 = signals.collect_log_tail({"type": "log_tail", "path": "app.log"}, ctx(project))
    assert r2.fresh is False and "quiet" in r2.detail


# -- jsonl_log -------------------------------------------------------------


def test_jsonl_log_counts_today(project):
    import time as _t

    project.mkdir(parents=True, exist_ok=True)
    today = _t.strftime("%Y-%m-%d", _t.gmtime(NOW))
    log = project / "usage.jsonl"
    log.write_text(
        f'{{"ts": "{today}T01:00:00Z"}}\n{{"ts": "2001-01-01T00:00:00Z"}}\n'
        f'{{"ts": "{today}T02:00:00Z"}}\n'
    )
    touch(log, NOW - 30)
    r = signals.collect_jsonl_log({"type": "jsonl_log", "path": "usage.jsonl"}, ctx(project))
    assert r.ok and r.fresh is True and r.detail.startswith("2 entr")


# -- claude_session --------------------------------------------------------


def test_claude_session_signal_reports_sessions(claude_tree):
    home, tasks, proj, slug = claude_tree
    from .conftest import write_transcript

    write_transcript(home, slug, "sess-a", NOW - 30)
    r = signals.collect_claude_session(
        {
            "type": "claude_session",
            "claude_home": str(home),
            "task_root_glob": str(tasks),
            "project_root": str(proj),
        },
        ctx(proj),
    )
    assert r.ok and r.fresh is True
    assert r.sessions and r.sessions[0].state == "WORKING"
    assert "WORKING:1" in r.detail


def test_claude_session_no_sessions(claude_tree, tmp_path):
    home, tasks, _, _ = claude_tree
    other = tmp_path / "other"
    other.mkdir()
    r = signals.collect_claude_session(
        {"type": "claude_session", "claude_home": str(home),
         "task_root_glob": str(tasks), "project_root": str(other)},
        ctx(other),
    )
    assert r.ok and r.fresh is False and "no sessions" in r.detail


# -- command ---------------------------------------------------------------


def test_command_number_and_text(project):
    project.mkdir(parents=True, exist_ok=True)
    n = signals.collect_command(
        {"type": "command", "command": "echo 3", "parse": "number"}, ctx(project)
    )
    assert n.ok and n.fresh is True and n.detail == "3"
    z = signals.collect_command(
        {"type": "command", "command": "echo 0", "parse": "number"}, ctx(project)
    )
    assert z.fresh is False
    t = signals.collect_command(
        {"type": "command", "command": "echo hello"}, ctx(project)
    )
    assert t.detail == "hello"


def test_command_epoch_mtime(project):
    project.mkdir(parents=True, exist_ok=True)
    r = signals.collect_command(
        {"type": "command", "command": f"echo {int(NOW - 60)}", "parse": "epoch_mtime"},
        ctx(project),
    )
    assert r.ok and r.fresh is True and 50 < r.age_s < 70


# -- registry / collect_all -----------------------------------------------


def test_collect_all_unknown_type_is_reading_not_crash(project):
    project.mkdir(parents=True, exist_ok=True)
    out = signals.collect_all([{"type": "wat", "label": "x"}], ctx(project))
    assert len(out) == 1 and not out[0].ok and "unknown signal type" in out[0].error


def test_detect_signals_on_git_python_project(git_repo):
    (git_repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    (git_repo / "logs").mkdir()
    (git_repo / "logs" / "app.log").write_text("")
    got = signals.detect_signals(git_repo)
    types = {s["type"] for s in got}
    assert {"git", "process", "log_tail"} <= types


# -- file_mtime exclusion of the monitor's own output ----------------------


def test_file_mtime_excludes_monitor_output_dirs(tmp_path):
    # A broad "**/*" glob must not count the monitor's own status/state
    # writes as project activity (self-triggering ACTIVE regression).
    (tmp_path / "src").mkdir()
    touch(tmp_path / "src" / "app.py", NOW - 3600)
    out = tmp_path / "reports" / "status"
    out.mkdir(parents=True)
    touch(out / "status.html", NOW - 1)  # fresh: written by the last tick
    state = tmp_path / ".ccdoing"
    state.mkdir()
    touch(state / "state.json", NOW - 1)

    cfg = {"type": "file_mtime", "glob": "**/*", "label": "files"}
    c = Context(
        project_root=tmp_path, now=NOW, window_s=900,
        exclude_dirs=(out.resolve(), state.resolve()),
    )
    r = signals.collect_file_mtime(cfg, c)
    assert r.ok and r.matched is True
    assert r.fresh is False  # only src/app.py (1h old) counts
    assert all("status.html" not in ln and "state.json" not in ln for ln in r.lines)

    # Without exclusions the fresh monitor write would leak through.
    r2 = signals.collect_file_mtime(cfg, ctx(tmp_path))
    assert r2.fresh is True
