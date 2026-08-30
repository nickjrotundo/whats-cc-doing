"""UI layer: titles, human time, the abridged activity table, the full
signals expander, and the stale-page self-check script."""

from __future__ import annotations

import calendar
import time

import pytest

from whats_cc_doing import status_json
from whats_cc_doing.config import ConfigError, load_config
from whats_cc_doing.render import render_html
from whats_cc_doing.signals import Reading
from whats_cc_doing.util import age_str, fmt_timestamp
from whats_cc_doing.verdict import compute_verdict

from .conftest import NOW


def R(**kw) -> Reading:
    base = dict(label="sig", type="git", weight="primary", ok=True)
    base.update(kw)
    return Reading(**base)


def html_for(cfg, readings, states=None):
    v = compute_verdict(readings, cfg)
    snap = status_json.build_snapshot(
        readings, v, cfg, NOW, signal_states=states or {}
    )
    return snap, render_html(snap)


def activity_slice(html: str) -> str:
    start = html.index("class=activity")
    return html[start : html.index("</table>", start)]


# -- age formatting --------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "-"),
        (0, "0s"),
        (42, "42s"),
        (300, "5m"),
        (312, "5m 12s"),
        (3600, "1h"),
        (68880, "19h 8m"),  # never 19h08m
        (86400, "1d"),
        (273600, "3d 4h"),
    ],
)
def test_age_str_human_format(seconds, expected):
    assert age_str(seconds) == expected


def test_age_str_never_zero_pads():
    for s in range(60, 90000, 61):
        out = age_str(s)
        assert "h0" not in out and "d0" not in out and "m0" not in out


# -- timestamps ------------------------------------------------------------


def test_fmt_timestamp_utc():
    epoch = calendar.timegm((2026, 8, 30, 9, 41, 0, 0, 0, 0))
    assert fmt_timestamp(epoch, "utc") == "2026-08-30 9:41 AM UTC"
    pm = calendar.timegm((2026, 8, 30, 21, 5, 0, 0, 0, 0))
    assert fmt_timestamp(pm, "utc") == "2026-08-30 9:05 PM UTC"
    midnight = calendar.timegm((2026, 8, 30, 0, 7, 0, 0, 0, 0))
    assert fmt_timestamp(midnight, "utc") == "2026-08-30 12:07 AM UTC"


def test_fmt_timestamp_local_uses_machine_tz(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        epoch = calendar.timegm((2026, 8, 30, 13, 41, 0, 0, 0, 0))  # 13:41 UTC
        assert fmt_timestamp(epoch, "local") == "2026-08-30 9:41 AM"  # EDT -4
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_page_meta_uses_human_time_not_iso(cfg):
    cfg.timezone = "utc"
    _, html = html_for(cfg, [R(fresh=True)])
    assert " AM UTC" in html or " PM UTC" in html
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW))
    assert iso not in html  # ISO stays in status.json, not the page


# -- title -----------------------------------------------------------------


def test_title_fallback_is_plain(cfg):
    _, html = html_for(cfg, [R(fresh=True)])
    assert "<title>What&#x27;s CC Doing</title>" in html or "<title>What's CC Doing</title>" in html
    assert "testproj - What" not in html and "testproj &middot; What" not in html


def test_title_from_config(cfg):
    cfg.title = "Acme build"
    snap, html = html_for(cfg, [R(fresh=True)])
    assert "<title>Acme build</title>" in html
    assert "<h1>Acme build</h1>" in html
    assert snap["project"] == "testproj"  # machines still key on project


def test_config_loads_title_and_timezone(tmp_path):
    (tmp_path / "ccdoing.yaml").write_text(
        "title: My Watcher\ntimezone: utc\nsignals:\n- type: git\n  label: g\n"
    )
    cfg = load_config(tmp_path / "ccdoing.yaml")
    assert cfg.title == "My Watcher" and cfg.timezone == "utc"


def test_config_rejects_bad_timezone_and_title(tmp_path):
    (tmp_path / "ccdoing.yaml").write_text("timezone: EST\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "ccdoing.yaml")
    (tmp_path / "ccdoing.yaml").write_text("title: [not, a, string]\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "ccdoing.yaml")


# -- abridged activity table ----------------------------------------------


def test_activity_table_primary_rows_only(cfg):
    readings = [
        R(label="git commits", fresh=True),
        R(label="build output", weight="info", fresh=True),
    ]
    _, html = html_for(cfg, readings)
    act = activity_slice(html)
    assert "git commits" in act and "ACTIVE" in act and "#1f8a4c" in act
    assert "build output" not in act


def test_activity_table_inactive_grey(cfg):
    _, html = html_for(cfg, [R(label="proc", fresh=False)])
    act = activity_slice(html)
    assert ">inactive<" in act and "#1f8a4c" not in act


def test_activity_table_health_down_red(cfg):
    cfg.verdict.health_failure_is_down = True
    readings = [R(fresh=True), R(label="api", weight="health", healthy=False)]
    _, html = html_for(cfg, readings)
    act = activity_slice(html)
    assert "api" in act and ">DOWN<" in act and "#e74c3c" in act


def test_activity_table_healthy_health_row_hidden(cfg):
    readings = [R(fresh=True), R(label="api", weight="health", healthy=True)]
    _, html = html_for(cfg, readings)
    assert "api" not in activity_slice(html)


def test_activity_table_drifted_primary_marked_inactive(cfg):
    _, html = html_for(
        cfg, [R(label="evals", fresh=True)], states={"git:evals#0": "no-match"}
    )
    act = activity_slice(html)
    assert ">inactive<" in act.replace("inactive<span", "inactive<") or "inactive" in act
    assert "&#8225;" in act  # drift marker
    assert "ACTIVE" not in act


# -- full table expander ---------------------------------------------------


def test_full_table_collapsed_sorted_no_weight_column(cfg):
    readings = [
        R(label="zz-info", weight="info", fresh=True),
        R(label="aa-primary", fresh=True),
        R(label="mm-health", weight="health", healthy=True),
    ]
    _, html = html_for(cfg, readings)
    assert "<details><summary>All signals</summary>" in html
    details = html[html.index("<details>") : html.index("</details>")]
    assert "<th>weight</th>" not in details
    assert details.index("aa-primary") < details.index("mm-health") < details.index("zz-info")


# -- stale-page self-check -------------------------------------------------


def test_stale_script_embedded(cfg):
    snap, html = html_for(cfg, [R(fresh=True)])
    assert f'GEN = {snap["generated_epoch"]:.3f}' in html
    assert "THRESH = 90" in html  # max(2.5 * 30s, 90) = 90
    assert 'id="stale"' in html
    assert "generator appears down" in html


def test_stale_threshold_scales_with_refresh(cfg):
    cfg.refresh_seconds = 120
    _, html = html_for(cfg, [R(fresh=True)])
    assert "THRESH = 300" in html  # 2.5 * 120


# -- browser notifications -------------------------------------------------


def test_notification_toggle_and_script_present(cfg):
    _, page = html_for(cfg, [R(fresh=True)])
    assert 'id="bn-toggle"' in page
    assert "Notification" in page and "requestPermission" in page
    # per-project localStorage keys and the transition vocabulary
    assert "ccdoing-bn:" in page and "ccdoing-bn-last:" in page
    assert '"QUIET","DOWN","STUCK"' in page
    # feature-detect + hidden by default (no JS -> no toggle)
    assert 'if (!("Notification" in window)) return;' in page
    assert "<button id=\"bn-toggle\" hidden" in page


def test_notification_script_escapes_hostile_text(cfg):
    cfg.title = 'x</script><script>alert(1)'
    _, page = html_for(cfg, [R(fresh=True)])
    # the hostile close-tag must never appear raw inside the page's JS
    assert "x</script><script>alert(1)" not in page
    assert "x\\u003c/script" in page
