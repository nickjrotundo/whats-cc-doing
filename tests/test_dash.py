"""Multi-project dashboard: aggregation, web rendering, routes, TUI frame."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from whats_cc_doing import dash, registry, serve, tui

NOW = 1_760_000_000.0


def make_project(
    base: Path,
    name: str,
    *,
    title: str | None = None,
    verdict: str = "ACTIVE",
    cause: str = "activity on: git commits",
    gen_age_s: float = 10.0,
    primary_age_s: float | None = 60.0,
    refresh: int = 30,
    write_snapshot: bool = True,
    snapshot_text: str | None = None,
) -> Path:
    root = base / name
    (root / "reports/status").mkdir(parents=True)
    (root / "ccdoing.yaml").write_text(
        f"refresh_seconds: {refresh}\n"
        "signals:\n  - {type: git, label: git commits, weight: primary}\n"
    )
    registry.register(root)
    if write_snapshot:
        if snapshot_text is None:
            snap = {
                "title": title,
                "verdict": verdict,
                "cause": cause,
                "generated_epoch": NOW - gen_age_s,
                "signals": (
                    [{"label": "git commits", "weight": "primary",
                      "age_seconds": primary_age_s}]
                    if primary_age_s is not None else []
                ),
            }
            snapshot_text = json.dumps(snap)
        (root / "reports/status/status.json").write_text(snapshot_text)
    return root


# -------------------------------------------------------------- aggregation


def test_cards_read_title_verdict_and_reconstruct_last_signal(tmp_path):
    make_project(tmp_path, "alpha", title="Alpha App", gen_age_s=20,
                 primary_age_s=100)
    cards = dash.load_cards(NOW)
    assert [c.title for c in cards] == ["Alpha App"]
    c = cards[0]
    assert c.verdict == "ACTIVE"
    # last signal = generated_epoch - youngest primary age -> 120s ago NOW
    assert c.last_signal_age(NOW) == pytest.approx(120.0)
    assert not c.generator_stale


def test_missing_and_unreadable_snapshots(tmp_path):
    make_project(tmp_path, "nodata", write_snapshot=False)
    make_project(tmp_path, "garbled", snapshot_text="{not json")
    by_name = {c.name: c for c in dash.load_cards(NOW)}
    assert by_name["nodata"].verdict == "no data"
    assert by_name["nodata"].last_signal_age(NOW) is None
    assert by_name["garbled"].verdict == "unreadable"
    assert not by_name["nodata"].has_data and not by_name["garbled"].has_data


def test_stale_generator_flagged_and_age_keeps_growing(tmp_path):
    # generated 1h ago with a 60s-old primary: the card must say ~1h, not 60s
    make_project(tmp_path, "stale", gen_age_s=3600, primary_age_s=60)
    (c,) = dash.load_cards(NOW)
    assert c.generator_stale  # 3600 > max(2.5*30, 90)
    assert c.last_signal_age(NOW) == pytest.approx(3660.0)


def test_title_falls_back_to_directory_name(tmp_path):
    make_project(tmp_path, "untitled", title=None)
    (c,) = dash.load_cards(NOW)
    assert c.title == "untitled"


def test_filter_recent_day_boundaries_and_sort(tmp_path):
    make_project(tmp_path, "fresh", gen_age_s=60, primary_age_s=0)
    make_project(tmp_path, "threeday", gen_age_s=0, primary_age_s=3 * 86400)
    make_project(tmp_path, "old", gen_age_s=0, primary_age_s=6 * 86400)
    cards = dash.load_cards(NOW)
    assert [c.name for c in cards] == ["fresh", "threeday", "old"]  # recency sort
    kept = dash.filter_recent(cards, days=4.0, now=NOW)
    assert [c.name for c in kept] == ["fresh", "threeday"]
    assert [c.name for c in dash.filter_recent(cards, days=7.0, now=NOW)] \
        == ["fresh", "threeday", "old"]
    # no-data projects have no age and are excluded by any window
    make_project(tmp_path, "nodata2", write_snapshot=False)
    assert "nodata2" not in [c.name for c in
                             dash.filter_recent(dash.load_cards(NOW), 999, NOW)]


def test_resolve_name_unknown_and_ambiguous(tmp_path):
    make_project(tmp_path / "a", "twin")
    assert dash.resolve_name("twin") is not None
    assert dash.resolve_name("nope") is None
    make_project(tmp_path / "b", "twin")  # same dir name, different parent
    assert dash.resolve_name("twin") is None  # ambiguous -> refuse


# -------------------------------------------------------------- web HTML


def test_dashboard_html_cards_data_age_links_and_escaping(tmp_path):
    make_project(tmp_path, "esc", title="<script>alert(1)</script>",
                 gen_age_s=30, primary_age_s=30)
    html = dash.render_dashboard_html(dash.load_cards(NOW), now=NOW, days=4)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'href="/p/esc/"' in html
    assert "data-age-days=" in html
    assert "Last signal:" in html and "ago" in html
    assert 'value="4"' in html  # the day-range input


def test_wrapper_and_multiview_html(tmp_path):
    make_project(tmp_path, "one", title="One")
    make_project(tmp_path, "two", title="Two")
    cards = dash.load_cards(NOW)
    w = dash.render_wrapper_html(cards[0])
    assert "all projects" in w and f"/p/{cards[0].name}/status.html" in w
    mv_html = dash.render_multiview_html(cards[:2])
    assert mv_html.count("<iframe") == 2 and "multi-view: 2 projects" in mv_html
    # user-facing feature name is Multi-view - "compare" must be gone
    dash_html = dash.render_dashboard_html(cards)
    assert "compare" not in dash_html.lower()
    assert "Multi-view checked" in dash_html and "'/multi?'" in dash_html


# -------------------------------------------------------------- server routes


@pytest.fixture
def dash_server(tmp_path):
    make_project(tmp_path, "srv", title="Srv Project", gen_age_s=5,
                 primary_age_s=5)
    httpd = serve.make_all_server(port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, dict(resp.headers), resp.read().decode()


def test_server_routes_and_no_store(dash_server):
    for path, marker in [
        ("/", "Srv Project"),
        ("/p/srv/", "status.html"),
        ("/p/srv/status.json", '"verdict"'),
        ("/p/srv/status.html", ""),  # 404 body checked below separately
    ]:
        try:
            status, headers, body = _get(dash_server + path)
        except urllib.error.HTTPError as err:
            status, headers, body = err.code, dict(err.headers), ""
        assert headers.get("Cache-Control") == "no-store", path
        if marker:
            assert status == 200 and marker in body, path


def test_server_404s_are_no_store_and_safe(dash_server):
    for path in ["/p/unknown/", "/p/srv/../../etc/passwd", "/nope",
                 "/multi?p=srv",  # multi-view needs >= 2
                 "/compare?p=srv&p=srv"]:  # old route is gone, not aliased
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(dash_server + path)
        assert exc.value.code == 404, path
        assert exc.value.headers.get("Cache-Control") == "no-store", path


# -------------------------------------------------------------- TUI frame


def test_tui_dash_frame_lists_verdict_and_age(tmp_path):
    make_project(tmp_path, "alpha", title="Alpha App", gen_age_s=20,
                 primary_age_s=100)
    make_project(tmp_path, "stale", gen_age_s=7200, primary_age_s=60)
    cards = dash.load_cards(NOW)
    frame = tui.render_dash_frame(cards, now=NOW, selected=0, days=4,
                                  total=len(cards), width=100, color=False)
    assert "Alpha App" in frame and "ACTIVE" in frame
    assert "Last signal: 2m ago" in frame          # 120s -> "2m"
    assert "[stale]" in frame                       # dead generator marked
    assert frame.splitlines()[2].startswith("> ")   # selection cursor


def test_tui_dash_frame_empty_and_hidden_note(tmp_path):
    make_project(tmp_path, "old", gen_age_s=0, primary_age_s=10 * 86400)
    cards = dash.load_cards(NOW)
    kept = dash.filter_recent(cards, 4, NOW)
    frame = tui.render_dash_frame(kept, now=NOW, days=4, total=len(cards),
                                  width=80, color=False)
    assert "no projects with recent activity" in frame
    assert "1 older hidden" in frame


def test_split_frame_width_gating_helpers():
    # ANSI-aware padding keeps split columns aligned
    colored = "\x1b[32mACTIVE\x1b[0m"
    padded = tui._pad_visible(colored, 10)
    assert tui._visible_len(padded) == 10 and "ACTIVE" in padded


def test_dashboard_notification_toggle_and_data_attrs(tmp_path):
    make_project(tmp_path, "bnproj", gen_age_s=30, primary_age_s=30)
    html = dash.render_dashboard_html(dash.load_cards(NOW), now=NOW, days=4)
    assert "id=bn-toggle" in html
    assert "requestPermission" in html
    assert 'data-verdict="' in html and 'data-pname="' in html
    assert 'data-ptitle="' in html
    assert "ccdoing-bn:__dash__" in html and "ccdoing-bn-last:" in html
