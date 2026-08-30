"""Pin the harness adapter's classification semantics with fixture trees.

All fixture content is invented; only the STRUCTURE mirrors Claude Code's
on-disk layout (transcript JSONL per session, task .output files).
"""

from __future__ import annotations

from whats_cc_doing.harness import (
    DEAD_AFTER_S,
    TranscriptSource,
    classify_sessions,
    slug_for,
)

from .conftest import NOW, touch, write_transcript


def make_source(claude_home, task_root):
    return TranscriptSource(
        claude_home=claude_home, task_root_glob=str(task_root).rstrip("/")
    )


def task_file(task_root, slug, session_id, name, mtime):
    return touch(task_root / slug / session_id / "tasks" / f"{name}.output", mtime)


def test_slug_matches_claude_convention(tmp_path):
    assert slug_for("/home/u/my-proj") == "-home-u-my-proj"


def test_working_when_transcript_fresh(claude_tree):
    home, tasks, proj, slug = claude_tree
    write_transcript(home, slug, "sess-a", NOW - 30)
    [s] = classify_sessions(proj, make_source(home, tasks), now=NOW)
    assert s.state == "WORKING"
    assert s.session_id == "sess-a"


def test_waiting_on_when_task_output_moving(claude_tree):
    home, tasks, proj, slug = claude_tree
    write_transcript(home, slug, "sess-a", NOW - 600)  # transcript stale
    task_file(tasks, slug, "sess-a", "agent1", NOW - 60)  # task alive
    [s] = classify_sessions(proj, make_source(home, tasks), now=NOW)
    assert s.state == "WAITING_ON"
    assert "agent1" in s.evidence


def test_dead_wait_when_tasks_stopped_after_transcript(claude_tree):
    """The original pytest-&-subshell incident: session parked on output that stopped moving."""
    home, tasks, proj, slug = claude_tree
    write_transcript(home, slug, "sess-a", NOW - 3000)
    task_file(tasks, slug, "sess-a", "agent1", NOW - 2000)  # newer than transcript, but stale
    [s] = classify_sessions(proj, make_source(home, tasks), now=NOW)
    assert s.state == "DEAD_WAIT"
    assert "agent1" in s.evidence
    assert "stopped moving" in s.evidence


def test_idle_when_transcript_moved_after_tasks_went_quiet(claude_tree):
    home, tasks, proj, slug = claude_tree
    task_file(tasks, slug, "sess-a", "agent1", NOW - 5000)
    write_transcript(home, slug, "sess-a", NOW - 400)  # saw it, moved on; not WORKING
    # transcript newer than dead tasks but older than working window
    [s] = classify_sessions(proj, make_source(home, tasks), now=NOW,
                            working_window_s=180)
    assert s.state == "IDLE"


def test_idle_when_no_tasks_and_stale(claude_tree):
    home, tasks, proj, slug = claude_tree
    write_transcript(home, slug, "sess-a", NOW - 4000)
    [s] = classify_sessions(proj, make_source(home, tasks), now=NOW)
    assert s.state == "IDLE"


def test_sessions_sorted_newest_first_and_limited(claude_tree):
    home, tasks, proj, slug = claude_tree
    for i in range(12):
        write_transcript(home, slug, f"s{i:02d}", NOW - 100 * (i + 1))
    out = classify_sessions(proj, make_source(home, tasks), now=NOW, limit=5)
    assert len(out) == 5
    assert out[0].session_id == "s00"


def test_unparseable_transcript_degrades_not_raises(claude_tree):
    home, tasks, proj, slug = claude_tree
    p = home / "projects" / slug / "bad.jsonl"
    p.write_bytes(b"\x00\xffnot json at all\n{{{{\n")
    touch(p, NOW - 30)
    [s] = classify_sessions(proj, make_source(home, tasks), now=NOW)
    # mtime semantics still classify it; content parsing just yields None
    assert s.state == "WORKING"
    assert s.last_line_type is None


def test_missing_project_dir_returns_empty(claude_tree, tmp_path):
    home, tasks, _, _ = claude_tree
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert classify_sessions(other, make_source(home, tasks), now=NOW) == []


def test_last_line_type_surfaces_type_only(claude_tree):
    home, tasks, proj, slug = claude_tree
    write_transcript(home, slug, "sess-a", NOW - 30, last_type="assistant")
    [s] = classify_sessions(proj, make_source(home, tasks), now=NOW)
    assert s.last_line_type == "assistant"
    # evidence strings never include message content
    assert "invented fixture line" not in s.evidence


def test_dead_after_threshold_is_configurable(claude_tree):
    home, tasks, proj, slug = claude_tree
    write_transcript(home, slug, "sess-a", NOW - 700)
    task_file(tasks, slug, "sess-a", "agent1", NOW - 500)
    # default: 500s-old task is still within DEAD_AFTER_S => WAITING_ON
    [s] = classify_sessions(proj, make_source(home, tasks), now=NOW)
    assert DEAD_AFTER_S > 500 and s.state == "WAITING_ON"
    # tighten the threshold: same tree now reads DEAD_WAIT
    [s] = classify_sessions(proj, make_source(home, tasks), now=NOW, dead_after_s=300)
    assert s.state == "DEAD_WAIT"
