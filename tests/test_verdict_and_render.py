from __future__ import annotations

from whats_cc_doing import status_json
from whats_cc_doing.harness import SessionState
from whats_cc_doing.render import render_html
from whats_cc_doing.signals import Reading
from whats_cc_doing.verdict import compute_verdict

from .conftest import NOW


def R(**kw) -> Reading:
    base = dict(label="sig", type="git", weight="primary", ok=True)
    base.update(kw)
    return Reading(**base)


def test_active_when_any_primary_fresh(cfg):
    v = compute_verdict([R(fresh=False), R(label="files", fresh=True)], cfg)
    assert v.state == "ACTIVE" and "files" in v.cause


def test_quiet_names_every_primary_with_age(cfg):
    v = compute_verdict(
        [R(fresh=False, age_s=2700), R(label="proc", fresh=False, detail="not running")],
        cfg,
    )
    assert v.state == "QUIET"
    assert "45m ago" in v.cause and "proc" in v.cause


def test_down_beats_everything(cfg):
    v = compute_verdict(
        [
            R(fresh=True),
            R(label="app", weight="health", healthy=False, detail="http://x -> 500"),
        ],
        cfg,
    )
    assert v.state == "DOWN" and "app" in v.cause


def test_down_respects_config_flag(cfg):
    cfg.verdict.health_failure_is_down = False
    v = compute_verdict(
        [R(fresh=True), R(label="app", weight="health", healthy=False)], cfg
    )
    assert v.state == "ACTIVE"


def test_stuck_from_dead_wait_session(cfg):
    sess = SessionState("abcdef123456", "DEAD_WAIT", 3000.0, "parked on task agent1")
    v = compute_verdict([R(fresh=False, sessions=[sess], type="claude_session")], cfg)
    assert v.state == "STUCK"
    assert v.stuck_session_ids == ["abcdef123456"]
    assert "abcdef123456"[:12] in v.cause


def test_stuck_beats_active(cfg):
    sess = SessionState("s1", "DEAD_WAIT", 3000.0, "parked")
    v = compute_verdict(
        [R(fresh=True), R(label="cc", fresh=False, sessions=[sess])], cfg
    )
    assert v.state == "STUCK"


def test_unreadable_primary_shows_in_quiet_cause(cfg):
    v = compute_verdict([R(ok=False, error="boom")], cfg)
    assert v.state == "QUIET" and "unreadable" in v.cause


def test_no_primaries_is_quiet_with_note(cfg):
    v = compute_verdict([R(weight="info", fresh=True)], cfg)
    assert v.state == "QUIET" and "no primary" in v.cause


# -- snapshot + html -------------------------------------------------------


def snap_for(cfg, readings, verdict):
    return status_json.build_snapshot(readings, verdict, cfg, NOW, quiet_since=NOW - 1320)


def test_snapshot_shape(cfg):
    sess = SessionState("s1", "DEAD_WAIT", 3000.0, "parked on agent1")
    readings = [R(fresh=False, sessions=[sess], lines=["a", "b"])]
    v = compute_verdict(readings, cfg)
    snap = snap_for(cfg, readings, v)
    assert snap["verdict"] == "STUCK"
    assert snap["quiet_for_seconds"] == 1320
    assert snap["signals"][0]["sessions"][0]["state"] == "DEAD_WAIT"
    assert snap["stuck_session_ids"] == ["s1"]


def test_html_escapes_and_renders(cfg):
    evil = "<script>alert(1)</script>"
    readings = [R(label=evil, fresh=True, detail=evil, lines=[evil])]
    v = compute_verdict(readings, cfg)
    html = render_html(snap_for(cfg, readings, v))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "ACTIVE" in html
    assert 'http-equiv="refresh"' in html


def test_html_stuck_banner_and_sessions_table(cfg):
    sess = SessionState("sessid-x", "DEAD_WAIT", 3000.0, "parked on agent1")
    readings = [R(label="claude", fresh=False, sessions=[sess])]
    v = compute_verdict(readings, cfg)
    html = render_html(snap_for(cfg, readings, v))
    assert "STUCK" in html and "DEAD_WAIT" in html and "sessid-x" in html
    assert "for 22m" in html  # quiet_for rendering


def test_html_quiet_for_note_absent_when_active(cfg):
    readings = [R(fresh=True)]
    v = compute_verdict(readings, cfg)
    snap = status_json.build_snapshot(readings, v, cfg, NOW, quiet_since=None)
    html = render_html(snap)
    assert "(for" not in html
