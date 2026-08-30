"""json_headline signal + config-drift detection.

The json_headline fixtures model the origin case directly: a stale
full eval battery, a newer single-scenario --save file that must NOT
masquerade as the battery, and a newest full run that must win.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from whats_cc_doing import drift, signals
from whats_cc_doing.cli import main
from whats_cc_doing.signals import Context, Reading

from .conftest import NOW


def ctx(root: Path, now: float = NOW, window_s: float = 900) -> Context:
    return Context(project_root=root, now=now, window_s=window_s)


def _eval_json(path: Path, n: int, passed: int, mtime: float, **extra) -> None:
    doc = {
        "sessions": [
            {"passed": i < passed, "skipped": False} for i in range(n)
        ],
        **extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    os.utime(path, (mtime, mtime))


# -- json_headline ---------------------------------------------------------


def test_headline_newest_full_run_wins_over_newer_partial(tmp_path):
    # The origin case: stale full battery, NEWER single-scenario save,
    # newest full battery. min_items must reject the masquerader.
    _eval_json(tmp_path / "cq_old_full.json", 10, 8, NOW - 2 * 86400)
    _eval_json(tmp_path / "cq_targeted_provenance.json", 1, 1, NOW - 600)
    _eval_json(tmp_path / "cq_new_full.json", 10, 9, NOW - 3600,
               overall_mean_score=91.4)
    r = signals.collect_json_headline(
        {"patterns": [str(tmp_path / "cq_*.json")], "min_items": 8,
         "template": "{passed}/{total} passed, {overall_mean_score:.0f}% overall"},
        ctx(tmp_path),
    )
    assert r.ok and r.matched is True
    assert "9/10 passed, 91% overall" in r.detail
    assert r.lines == ["cq_new_full.json"]


def test_headline_partial_wins_when_min_items_allows(tmp_path):
    _eval_json(tmp_path / "a_full.json", 10, 9, NOW - 3600)
    _eval_json(tmp_path / "a_one.json", 1, 1, NOW - 60)
    r = signals.collect_json_headline(
        {"patterns": [str(tmp_path / "a_*.json")]}, ctx(tmp_path)
    )
    assert "1/1 passed" in r.detail  # min_items defaults to 1: newest wins


def test_headline_missing_template_field_renders_placeholder(tmp_path):
    _eval_json(tmp_path / "r.json", 8, 8, NOW - 60)
    r = signals.collect_json_headline(
        {"patterns": [str(tmp_path / "r.json")],
         "template": "{passed}/{total}, {nonexistent_field} extra"},
        ctx(tmp_path),
    )
    assert "8/8, ? extra" in r.detail


def test_headline_results_key_and_skipped_counts(tmp_path):
    doc = {"results": [
        {"passed": True}, {"passed": False}, {"skipped": True},
    ]}
    p = tmp_path / "work.json"
    p.write_text(json.dumps(doc))
    os.utime(p, (NOW - 60, NOW - 60))
    r = signals.collect_json_headline(
        {"patterns": [str(p)], "template": "{passed}/{runnable} passed"},
        ctx(tmp_path),
    )
    assert "1/2 passed" in r.detail


def test_headline_skips_unreadable_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    os.utime(bad, (NOW - 10, NOW - 10))
    _eval_json(tmp_path / "good.json", 8, 7, NOW - 3600)
    r = signals.collect_json_headline(
        {"patterns": [str(tmp_path / "*.json")]}, ctx(tmp_path)
    )
    assert r.matched is True and "7/8 passed" in r.detail


def test_headline_no_match(tmp_path):
    r = signals.collect_json_headline(
        {"patterns": [str(tmp_path / "none_*.json")], "min_items": 8},
        ctx(tmp_path),
    )
    assert r.ok and r.matched is False and r.fresh is False
    assert ">= 8" in r.detail


def test_headline_detect_suggests_conservatively(tmp_path):
    root = tmp_path / "proj"
    _eval_json(root / "eval-results" / "run1.json", 9, 9, NOW - 60)
    suggestions = signals.detect_signals(root)
    heads = [s for s in suggestions if s["type"] == "json_headline"]
    assert len(heads) == 1
    assert heads[0]["patterns"] == ["eval-results/*.json"]
    # min_items defaults toward the masquerade-proof 8, not a toothless 1
    assert heads[0]["min_items"] == 8
    # and nothing suggested for a project without result-shaped JSON
    bare = tmp_path / "bare"
    bare.mkdir()
    assert not [s for s in signals.detect_signals(bare) if s["type"] == "json_headline"]


def test_headline_detect_min_items_capped_by_observed(tmp_path):
    # a small-but-real battery (3 items) must not get a dead-on-arrival
    # min_items of 8; it gets the observed count instead (floor 2)
    root = tmp_path / "proj"
    _eval_json(root / "results" / "small.json", 3, 3, NOW - 60)
    heads = [s for s in signals.detect_signals(root) if s["type"] == "json_headline"]
    assert len(heads) == 1
    assert heads[0]["min_items"] == 3


# -- drift states ----------------------------------------------------------


def _reading(kind="file_mtime", label="build output", matched=None, ok=True):
    return Reading(label=label, type=kind, weight="info", ok=ok, matched=matched)


def test_states_matched_is_ok_and_recorded(tmp_path):
    states = drift.apply_states([_reading(matched=True)], tmp_path, now=NOW)
    assert states == {"file_mtime:build output#0": "ok"}
    data = drift.load_drift(tmp_path)
    assert data["file_mtime:build output#0"]["last_matched"] == NOW


def test_states_nomatch_then_stale(tmp_path):
    r = _reading(matched=False)
    states = drift.apply_states([r], tmp_path, now=NOW, stale_after_s=7 * 86400)
    assert states["file_mtime:build output#0"] == "no-match"
    # eight days of never matching -> stale
    states = drift.apply_states(
        [r], tmp_path, now=NOW + 8 * 86400, stale_after_s=7 * 86400
    )
    assert states["file_mtime:build output#0"] == "stale"


def test_states_match_resets_staleness_clock(tmp_path):
    r = _reading(matched=False)
    drift.apply_states([r], tmp_path, now=NOW)
    drift.apply_states([_reading(matched=True)], tmp_path, now=NOW + 8 * 86400)
    states = drift.apply_states([r], tmp_path, now=NOW + 8 * 86400 + 60)
    assert states["file_mtime:build output#0"] == "no-match"  # not stale: just matched


def test_states_process_absence_is_ok_until_stale(tmp_path):
    r = _reading(kind="process", label="test runner", matched=False)
    states = drift.apply_states([r], tmp_path, now=NOW)
    assert states["process:test runner#0"] == "ok"  # not-running is data, not drift
    states = drift.apply_states(
        [r], tmp_path, now=NOW + 8 * 86400, stale_after_s=7 * 86400
    )
    assert states["process:test runner#0"] == "stale"


def test_states_none_matched_and_failed_probe_stay_ok(tmp_path):
    states = drift.apply_states(
        [_reading(kind="git", matched=None), _reading(matched=False, ok=False)],
        tmp_path, now=NOW,
    )
    assert set(states.values()) == {"ok"}


def test_states_survive_corrupt_drift_file(tmp_path):
    (tmp_path / drift.DRIFT_FILE).write_text('{"file_mtime:build output#0": "garbage"}')
    states = drift.apply_states([_reading(matched=False)], tmp_path, now=NOW)
    assert states["file_mtime:build output#0"] == "no-match"


def test_maintenance_lines(tmp_path):
    r1, r2 = _reading(matched=False), _reading(kind="log_tail", label="logs", matched=True)
    states = drift.apply_states([r1, r2], tmp_path, now=NOW)
    lines = drift.maintenance_lines([r1, r2], states)
    assert len(lines) == 1 and "build output" in lines[0]


def test_inventory_drift_diffs_by_type(git_repo):
    assert any(
        s["type"] == "git" for s in drift.inventory_drift(git_repo, [])
    )
    assert not [
        s for s in drift.inventory_drift(git_repo, [{"type": "git"}])
        if s["type"] == "git"
    ]


# -- doctor --drift --------------------------------------------------------


def _write_config(root: Path, signals_yaml: str) -> Path:
    cfg = root / "ccdoing.yaml"
    cfg.write_text(
        "version: 1\nproject_name: driftproj\noutput_dir: reports/status\n"
        f"signals:\n{signals_yaml}"
    )
    return cfg


def test_doctor_drift_reports_nomatch_and_candidates(git_repo, capsys):
    # configured: a glob that matches nothing; detectable: git (unconfigured)
    cfg = _write_config(
        git_repo, "  - type: file_mtime\n    label: dist\n    glob: 'no_such/**/*'\n"
    )
    assert main(["--config", str(cfg), "doctor", "--drift"]) == 0
    out = capsys.readouterr().out
    assert "not matching" in out and "dist" in out
    assert "not configured" in out and "git" in out
    assert "/ccdoing:tune" in out


def test_doctor_drift_quiet_one_liner_and_silence(git_repo, tmp_path, capsys):
    cfg = _write_config(
        git_repo, "  - type: file_mtime\n    label: dist\n    glob: 'no_such/**/*'\n"
    )
    assert main(["--config", str(cfg), "doctor", "--drift", "--quiet"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "drift detected" in out

    # a clean project+config: silence
    clean = tmp_path / "clean"
    clean.mkdir()
    ccfg = _write_config(clean, "  - type: command\n    label: c\n    command: 'true'\n")
    assert main(["--config", str(ccfg), "doctor", "--drift", "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_tick_snapshot_carries_state_and_maintenance(git_repo, capsys):
    cfg = _write_config(
        git_repo,
        "  - type: git\n    label: git commits\n"
        "  - type: file_mtime\n    label: dist\n    glob: 'no_such/**/*'\n",
    )
    assert main(["--config", str(cfg), "tick", "--no-watchdog"]) == 0
    snap = json.loads((git_repo / "reports/status/status.json").read_text())
    by_label = {s["label"]: s for s in snap["signals"]}
    assert by_label["git commits"]["state"] == "ok"
    assert by_label["dist"]["state"] == "no-match"
    assert any("dist" in m for m in snap["maintenance"])
    html = (git_repo / "reports/status/status.html").read_text()
    assert "no-match" in html and "doctor --drift" in html
