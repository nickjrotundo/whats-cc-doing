"""Cross-slug session discovery, registry enrichment, and subagent listing.

All fixtures are invented content with real on-disk structure. The
motivating case (Nick's observed bug): a session started in the PARENT
directory of the monitored project is filed under the parent's slug and
was invisible to a plain slug lookup even while actively working on the
project.
"""
from __future__ import annotations

import json

from whats_cc_doing.harness import (
    SubagentInfo,
    _cwd_relevant,
    _pid_alive,
    classify_sessions,
    slug_for,
)

from .conftest import NOW, touch, write_transcript
from .test_harness import make_source


def make_tree(tmp_path, project_rel="work/myproj"):
    project_dir = tmp_path / project_rel
    project_dir.mkdir(parents=True, exist_ok=True)
    claude_home = tmp_path / "dot-claude"
    (claude_home / "projects" / slug_for(project_dir)).mkdir(parents=True, exist_ok=True)
    task_root = tmp_path / "tasks-root"
    task_root.mkdir(exist_ok=True)
    return claude_home, task_root, project_dir


# -- cwd relevance rule ------------------------------------------------------


def test_cwd_relevant_inside_root():
    assert _cwd_relevant("/a/b/c", "/a/b")


def test_cwd_relevant_ancestor_of_root():
    assert _cwd_relevant("/a", "/a/b/c")


def test_cwd_irrelevant_sibling():
    assert not _cwd_relevant("/a/other", "/a/b")
    # prefix strings must not fool the boundary check
    assert not _cwd_relevant("/a/bb", "/a/b")


# -- discovery ---------------------------------------------------------------


def test_exact_slug_session_included_without_cwd(tmp_path):
    """Legacy behavior: exact-slug transcripts need no parseable cwd."""
    home, tasks, proj = make_tree(tmp_path)
    write_transcript(home, slug_for(proj), "sess-a", NOW - 30)
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert [s.session_id for s in out] == ["sess-a"]
    assert out[0].state == "WORKING"


def test_ancestor_slug_session_with_matching_cwd_included(tmp_path):
    """Nick's case: session filed under the parent dir's slug, cwd shows it
    working inside the monitored project."""
    home, tasks, proj = make_tree(tmp_path)
    parent = proj.parent  # tmp/work
    write_transcript(
        home, slug_for(parent), "sess-parent", NOW - 40,
        cwd=str(proj / "src"),
    )
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert [s.session_id for s in out] == ["sess-parent"]
    assert out[0].state == "WORKING"


def test_ancestor_slug_session_with_ancestor_cwd_included(tmp_path):
    """cwd that is an ancestor of the root also counts (session parked at
    the parent while orchestrating the subproject)."""
    home, tasks, proj = make_tree(tmp_path)
    parent = proj.parent
    write_transcript(
        home, slug_for(parent), "sess-anc", NOW - 40, cwd=str(parent)
    )
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert [s.session_id for s in out] == ["sess-anc"]


def test_ancestor_slug_session_with_unrelated_cwd_excluded(tmp_path):
    home, tasks, proj = make_tree(tmp_path)
    parent = proj.parent
    other = parent / "otherproj"
    other.mkdir()
    write_transcript(
        home, slug_for(parent), "sess-other", NOW - 40, cwd=str(other)
    )
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert out == []


def test_unrelated_slug_dir_never_scanned(tmp_path):
    home, tasks, proj = make_tree(tmp_path)
    write_transcript(home, "-completely-unrelated", "sess-x", NOW - 10,
                     cwd=str(proj))
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    # dir name matches neither the slug, an ancestor slug, nor a
    # descendant prefix -> not a candidate even though cwd would match
    assert out == []


def test_descendant_slug_dir_included_when_cwd_matches(tmp_path):
    """Session started in a subdir of the monitored root."""
    home, tasks, proj = make_tree(tmp_path)
    sub = proj / "sub"
    sub.mkdir()
    write_transcript(home, slug_for(sub), "sess-sub", NOW - 20, cwd=str(sub))
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert [s.session_id for s in out] == ["sess-sub"]


def test_descendant_prefix_collision_without_cwd_excluded(tmp_path):
    """A dir that merely LOOKS like a descendant slug (lossy encoding)
    must prove itself via cwd."""
    home, tasks, proj = make_tree(tmp_path)
    fake = slug_for(proj) + "-imposter"
    (home / "projects" / fake).mkdir(parents=True)
    # transcript with an unrelated cwd
    write_transcript(home, fake, "sess-fake", NOW - 20,
                     cwd=str(proj.parent / "elsewhere"))
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert out == []


def test_cross_slug_session_uses_its_own_slug_for_tasks(tmp_path):
    """Task outputs live under the session's OWN slug, not the monitored
    project's -- WAITING_ON must still be detected."""
    home, tasks, proj = make_tree(tmp_path)
    parent = proj.parent
    pslug = slug_for(parent)
    write_transcript(home, pslug, "sess-p", NOW - 600, cwd=str(proj))
    touch(tasks / pslug / "sess-p" / "tasks" / "agent1.output", NOW - 60)
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert out[0].state == "WAITING_ON"
    assert "agent1" in out[0].evidence


def test_dedupe_prefers_newest_and_respects_limit(tmp_path):
    home, tasks, proj = make_tree(tmp_path)
    slug = slug_for(proj)
    for i in range(10):
        write_transcript(home, slug, f"s{i:02d}", NOW - 100 * (i + 1))
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW, limit=4)
    assert len(out) == 4
    assert out[0].session_id == "s00"


# -- registry enrichment -----------------------------------------------------


def test_registry_name_and_dead_pid(tmp_path):
    home, tasks, proj = make_tree(tmp_path)
    write_transcript(home, slug_for(proj), "sess-a", NOW - 30)
    reg = home / "sessions"
    reg.mkdir()
    (reg / "999.json").write_text(json.dumps({
        "sessionId": "sess-a",
        "name": "fixture session name",
        "pid": 2 ** 22 + 12345,  # beyond default pid_max -> never alive
        "procStart": "1",
    }))
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert out[0].name == "fixture session name"
    assert out[0].alive in (False, None)  # False on Linux, None without /proc


def test_registry_garbage_is_ignored(tmp_path):
    home, tasks, proj = make_tree(tmp_path)
    write_transcript(home, slug_for(proj), "sess-a", NOW - 30)
    reg = home / "sessions"
    reg.mkdir()
    (reg / "1.json").write_text("{not json")
    (reg / "2.json").write_text(json.dumps(["not", "a", "dict"]))
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert out[0].name is None
    assert out[0].alive is None


def test_pid_alive_bad_inputs():
    assert _pid_alive(None) is None
    assert _pid_alive(-4) is None
    assert _pid_alive("12") is None


# -- subagents ---------------------------------------------------------------


def _write_subagent(home, slug, sid, agent_id, mtime, description=None):
    d = home / "projects" / slug / sid / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{agent_id}.jsonl"
    p.write_text(json.dumps({"type": "user", "agentId": agent_id}) + "\n")
    touch(p, mtime)
    if description is not None:
        (d / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose",
                        "description": description})
        )
    return p


def test_subagents_listed_with_description_and_active_flag(tmp_path):
    home, tasks, proj = make_tree(tmp_path)
    slug = slug_for(proj)
    write_transcript(home, slug, "sess-a", NOW - 30)
    _write_subagent(home, slug, "sess-a", "abc123", NOW - 20,
                    description="invented worker task")
    _write_subagent(home, slug, "sess-a", "def456", NOW - 4000)  # stale, no meta
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    subs = out[0].subagents
    assert [a.agent_id for a in subs] == ["abc123", "def456"]
    assert subs[0].description == "invented worker task"
    assert subs[0].active is True
    assert subs[1].active is False


def test_subagents_older_than_cutoff_dropped(tmp_path):
    home, tasks, proj = make_tree(tmp_path)
    slug = slug_for(proj)
    write_transcript(home, slug, "sess-a", NOW - 30)
    _write_subagent(home, slug, "sess-a", "old111", NOW - 7 * 3600)
    out = classify_sessions(proj, source=make_source(home, tasks), now=NOW)
    assert out[0].subagents == []


def test_subagent_info_serializes(tmp_path):
    from dataclasses import asdict

    info = SubagentInfo(agent_id="x", description="d", age_s=1.0, active=True)
    assert asdict(info)["description"] == "d"
