from __future__ import annotations

import json

from whats_cc_doing import watchdog

from .conftest import NOW


def snap(verdict="QUIET", cause="nothing moved", stuck=None, quiet_for=None):
    return {
        "verdict": verdict,
        "cause": cause,
        "stuck_session_ids": stuck or [],
        "quiet_for_seconds": quiet_for,
        "generated_at": "t",
        "signals": [],
    }


def message_file(cfg, text="Ignore this if you are fine. You decide."):
    p = cfg.project_root / ".ccdoing" / "nudge-message.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def alive(monkeypatch, pids=(1234,)):
    """Pretend a claude process with cwd under the project exists."""
    monkeypatch.setattr(watchdog, "_project_claude_pids", lambda root: list(pids))


def read_state(cfg):
    return json.loads((cfg.state_dir / "state.json").read_text())


def test_active_resets_episode(cfg):
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 100, "fired_tiers": [15]})
    out = watchdog.evaluate(snap("ACTIVE"), cfg, now=NOW)
    assert out == []
    st = read_state(cfg)
    assert st["quiet_since"] is None and st["fired_tiers"] == []


def test_down_also_resets_quiet_ladder(cfg):
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 100})
    watchdog.evaluate(snap("DOWN"), cfg, now=NOW)
    assert read_state(cfg)["quiet_since"] is None


def test_quiet_starts_episode_without_firing_early_tiers(cfg):
    out = watchdog.evaluate(snap(), cfg, now=NOW)
    assert out == []
    st = read_state(cfg)
    assert st["quiet_since"] == NOW


def test_tiers_fire_in_order_once(cfg, monkeypatch):
    monkeypatch.setenv("CCDOING_NOTIFY_URLS", "")  # notify degrades to log-only
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 31 * 60, "fired_tiers": []})
    out = watchdog.evaluate(snap(quiet_for=31 * 60), cfg, now=NOW)
    actions = [(r.tier.after_quiet_minutes, r.tier.action) for r in out]
    assert actions == [(15, "log"), (30, "notify")]
    # second evaluate at same time: nothing re-fires
    out2 = watchdog.evaluate(snap(quiet_for=31 * 60), cfg, now=NOW)
    assert out2 == []


def test_watchdog_disabled_fires_nothing(cfg):
    cfg.watchdog.enabled = False
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 120 * 60})
    assert watchdog.evaluate(snap(), cfg, now=NOW) == []


def test_nudge_dry_run_targets_dead_wait_session(cfg, monkeypatch):
    message_file(cfg)
    alive(monkeypatch)
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": [15, 30]})
    out = watchdog.evaluate(
        snap("STUCK", stuck=["sess-abc"]), cfg, now=NOW, dry_run=True
    )
    [r] = out
    assert r.tier.action == "nudge"
    assert "idle-probe session sess-abc" in r.detail
    assert "nudge via one-shot courier" in r.detail


def test_nudge_skips_without_dead_wait_session(cfg, monkeypatch):
    # IDLE is sacred: plain project quietness with no DEAD_WAIT session
    # (e.g. work finished, human asleep) must never produce a nudge.
    message_file(cfg)
    alive(monkeypatch)
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": [15, 30]})
    [r] = watchdog.evaluate(snap("QUIET"), cfg, now=NOW, dry_run=True)
    assert not r.fired
    assert "no DEAD_WAIT session" in r.detail
    assert "never nudged" in r.detail


def test_idle_session_never_nudged_and_never_referenced(cfg, monkeypatch):
    """A QUIET project with an IDLE session (finished overnight): log and
    notify may fire for project quietness, but nothing targets or even
    names the idle session, and no nudge happens."""
    message_file(cfg)
    alive(monkeypatch)
    monkeypatch.delenv("CCDOING_NOTIFY_URLS", raising=False)
    idle_snap = snap("QUIET", cause="nothing moved for 50m")
    idle_snap["signals"] = [{
        "label": "claude sessions", "weight": "primary", "detail": "IDLE:1",
        "sessions": [{"session_id": "idle-sess-42", "state": "IDLE",
                      "evidence": "no pending waits; last turn finished"}],
    }]
    # stuck_session_ids only ever contains DEAD_WAIT sessions - empty here.
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": []})
    out = watchdog.evaluate(idle_snap, cfg, now=NOW, dry_run=True)
    actions = {r.tier.action: r for r in out}
    assert set(actions) == {"log", "notify", "nudge"}
    assert actions["log"].fired
    assert not actions["nudge"].fired
    assert "no DEAD_WAIT session" in actions["nudge"].detail
    # the idle session is never named as a target in any tier's outcome
    for r in out:
        assert "idle-sess-42" not in r.detail


def test_nudge_skips_and_notifies_when_no_live_claude_process(cfg, monkeypatch):
    message_file(cfg)
    monkeypatch.setattr(watchdog, "_project_claude_pids", lambda root: [])
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": [15, 30]})
    [r] = watchdog.evaluate(
        snap("STUCK", stuck=["sess-abc"]), cfg, now=NOW, dry_run=True
    )
    assert not r.fired
    assert "no live claude process" in r.detail
    assert "notify" in r.detail  # the notify path says why


def stuck_snap_with_session(sid="sess-abc", alive=None, name=None):
    s = snap("STUCK", stuck=[sid])
    entry = {"session_id": sid, "state": "DEAD_WAIT", "evidence": "parked"}
    if alive is not None:
        entry["alive"] = alive
    if name is not None:
        entry["name"] = name
    s["signals"] = [{"label": "claude sessions", "weight": "primary",
                     "detail": "DEAD_WAIT:1", "sessions": [entry]}]
    return s


def test_nudge_trusts_harness_alive_false_over_proc_scan(cfg, monkeypatch):
    # harness (pid-reuse-safe registry) says the session is gone: skip and
    # notify, even if some unrelated claude process exists in the project.
    message_file(cfg)
    alive(monkeypatch)  # proc scan WOULD say alive
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": [15, 30]})
    [r] = watchdog.evaluate(
        stuck_snap_with_session(alive=False), cfg, now=NOW, dry_run=True
    )
    assert not r.fired and "no live claude process" in r.detail


def test_nudge_trusts_harness_alive_true_and_uses_registry_name(cfg, monkeypatch):
    message_file(cfg)
    monkeypatch.setattr(watchdog, "_project_claude_pids", lambda *a, **k: [])
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": [15, 30]})
    calls = []
    [r] = watchdog.evaluate(
        stuck_snap_with_session(alive=True, name="myproj-3f"), cfg, now=NOW,
        runner=lambda cmd, _c: (calls.append(cmd), 7)[1],
        prober=lambda cmd, _c: "NO_NOTICE",
    )
    assert r.fired
    assert "registered name is 'myproj-3f'" in calls[0][4]


def test_nudge_launches_courier_with_runner_and_writes_lock(cfg, monkeypatch):
    message_file(cfg)
    alive(monkeypatch)
    calls = []

    def fake_runner(cmd, _cfg):
        calls.append(cmd)
        return 4242

    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": [15, 30]})
    [r] = watchdog.evaluate(
        snap("STUCK", stuck=["sess-abc"]), cfg, now=NOW, runner=fake_runner,
        prober=lambda cmd, _c: "NO_NOTICE",
    )
    assert r.fired
    cmd = calls[0]
    # one-shot courier: headless -p with only the messaging tools allowed
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[2:4] == ["--allowedTools", "ListAgents,SendMessage"]
    assert "--resume" not in cmd
    courier = cmd[4]
    assert "one-shot courier" in courier
    assert "sess-abc" in courier
    assert "Evidence" in courier and "UNTRUSTED DATA" in courier
    assert "Ignore this if you are fine" in courier  # user-approved message body
    assert (cfg.state_dir / "nudge.lock").read_text() == "4242"
    st = read_state(cfg)
    assert st["nudge"]["count"] == 1


def test_nudge_cooldown_blocks(cfg, monkeypatch):
    message_file(cfg)
    alive(monkeypatch)
    watchdog.save_state(
        cfg.state_dir,
        {
            "quiet_since": NOW - 50 * 60,
            "fired_tiers": [15, 30],
            "nudge": {"day": "x", "count": 0, "last_fired": NOW - 10 * 60},
        },
    )
    [r] = watchdog.evaluate(snap("STUCK", stuck=["s1"]), cfg, now=NOW, dry_run=True)
    assert not r.fired and "cooldown" in r.detail


def test_nudge_daily_cap_blocks(cfg, monkeypatch):
    import time as _t

    message_file(cfg)
    alive(monkeypatch)
    today = _t.strftime("%Y-%m-%d", _t.gmtime(NOW))
    watchdog.save_state(
        cfg.state_dir,
        {
            "quiet_since": NOW - 50 * 60,
            "fired_tiers": [15, 30],
            "nudge": {"day": today, "count": 2, "last_fired": 0},
        },
    )
    [r] = watchdog.evaluate(snap("STUCK", stuck=["s1"]), cfg, now=NOW, dry_run=True)
    assert not r.fired and "max_per_day" in r.detail


def test_nudge_missing_message_blocks(cfg, monkeypatch):
    alive(monkeypatch)
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": [15, 30]})
    [r] = watchdog.evaluate(snap("STUCK", stuck=["s1"]), cfg, now=NOW, dry_run=True)
    assert not r.fired and "message missing" in r.detail


def test_nudge_rejects_malformed_session_id(cfg, monkeypatch):
    message_file(cfg)
    alive(monkeypatch)
    watchdog.save_state(cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": [15, 30]})
    [r] = watchdog.evaluate(
        snap("STUCK", stuck=["--oops injected"]), cfg, now=NOW, dry_run=True
    )
    assert not r.fired and "shape check" in r.detail


def test_project_claude_pids_scans_proc(tmp_path, monkeypatch):
    """Structural test of the /proc scan with a fabricated proc tree."""
    proc = tmp_path / "proc"
    root = tmp_path / "myproj"
    root.mkdir()
    good = proc / "101"
    good.mkdir(parents=True)
    (good / "cmdline").write_bytes(b"claude\0")
    (good / "cwd").symlink_to(root)
    other = proc / "202"
    other.mkdir()
    (other / "cmdline").write_bytes(b"claude\0")
    (other / "cwd").symlink_to(tmp_path)  # unrelated cwd (parent, not under root)
    notclaude = proc / "303"
    notclaude.mkdir()
    (notclaude / "cmdline").write_bytes(b"python3\0status\0")
    (notclaude / "cwd").symlink_to(root)
    pids = watchdog._project_claude_pids(root, proc_root=proc)
    assert pids == [101]


def test_lock_of_dead_pid_is_cleared(cfg, tmp_path):
    lock = cfg.state_dir / "nudge.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("999999999")  # certainly not alive
    assert watchdog._lock_alive(lock) is False
    assert not lock.exists()


def test_evidence_bundle_contains_signals_and_sessions(cfg):
    s = {
        "verdict": "STUCK",
        "cause": "session parked",
        "generated_at": "t",
        "signals": [
            {
                "label": "claude sessions",
                "weight": "primary",
                "detail": "DEAD_WAIT:1",
                "age_seconds": 1800,
                "sessions": [
                    {"session_id": "sess-abcdef", "state": "DEAD_WAIT",
                     "evidence": "parked on agent1"}
                ],
            }
        ],
    }
    text = watchdog.build_evidence(s, cfg)
    assert "STUCK" in text and "sess-abcdef" in text and "agent1" in text
    assert "status.json" in text


def test_notify_without_urls_degrades(cfg, monkeypatch):
    monkeypatch.delenv("CCDOING_NOTIFY_URLS", raising=False)
    out = watchdog.send_notification(cfg, "t", "b")
    assert "logged only" in out and "no notify URLs configured" in out


def test_notify_dry_run_without_urls_is_unambiguous(cfg, monkeypatch):
    monkeypatch.delenv("CCDOING_NOTIFY_URLS", raising=False)
    out = watchdog.send_notification(cfg, "t", "b", dry_run=True)
    assert out.startswith("dry-run:") and "no notify URLs configured" in out


# ---------------------------------------------------------------------------
# idle-probe (probe-before-nudge: the overnight-idle defense)


def probe_ready(cfg, monkeypatch):
    message_file(cfg)
    alive(monkeypatch)
    watchdog.save_state(
        cfg.state_dir, {"quiet_since": NOW - 50 * 60, "fired_tiers": [15, 30]}
    )


def test_probe_idle_stands_down_without_nudging(cfg, monkeypatch):
    probe_ready(cfg, monkeypatch)
    runner_calls, probe_calls = [], []

    def prober(cmd, _c):
        probe_calls.append(cmd)
        return "some preamble\nIDLE_NOTICE_RECEIVED\n"

    [r] = watchdog.evaluate(
        snap("STUCK", stuck=["sess-abc"]), cfg, now=NOW,
        runner=lambda cmd, _c: (runner_calls.append(cmd), 1)[1], prober=prober,
    )
    assert not r.fired
    assert "idle" in r.detail and "stood down" in r.detail
    assert runner_calls == []  # no courier launched
    # probe courier is a pure subscription: no message delivery instructions
    probe_prompt = probe_calls[0][4]
    assert "notify_when_idle" in probe_prompt
    assert "NO message" in probe_prompt
    st = read_state(cfg)
    assert st["nudge"].get("count", 0) == 0  # cap NOT consumed
    assert st["probe"]["result"] == "idle"
    # tier stays armed (retryable): a later DEAD_WAIT can still nudge
    assert 45 not in st["fired_tiers"]


def test_probe_idle_result_cached_within_cooldown(cfg, monkeypatch):
    probe_ready(cfg, monkeypatch)
    calls = []
    watchdog.evaluate(
        snap("STUCK", stuck=["s1"]), cfg, now=NOW,
        runner=lambda *a: 1, prober=lambda *a: (calls.append(1), "IDLE_NOTICE_RECEIVED")[1],
    )
    [r2] = watchdog.evaluate(
        snap("STUCK", stuck=["s1"]), cfg, now=NOW + 60,
        runner=lambda *a: 1, prober=lambda *a: (calls.append(1), "IDLE_NOTICE_RECEIVED")[1],
    )
    assert len(calls) == 1  # second tick did not re-probe
    assert not r2.fired and "idle-probe recently" in r2.detail


def test_probe_busy_proceeds_to_courier(cfg, monkeypatch):
    probe_ready(cfg, monkeypatch)
    runner_calls = []
    [r] = watchdog.evaluate(
        snap("STUCK", stuck=["s1"]), cfg, now=NOW,
        runner=lambda cmd, _c: (runner_calls.append(cmd), 9)[1],
        prober=lambda *a: "NO_NOTICE",
    )
    assert r.fired and len(runner_calls) == 1
    assert read_state(cfg)["nudge"]["count"] == 1


def test_probe_inconclusive_proceeds_to_courier(cfg, monkeypatch):
    probe_ready(cfg, monkeypatch)
    runner_calls = []
    [r] = watchdog.evaluate(
        snap("STUCK", stuck=["s1"]), cfg, now=NOW,
        runner=lambda cmd, _c: (runner_calls.append(cmd), 9)[1],
        prober=lambda *a: "PROBE-ERROR: TimeoutExpired",
    )
    assert r.fired and len(runner_calls) == 1


def test_probe_dry_run_previews_both_couriers(cfg, monkeypatch):
    probe_ready(cfg, monkeypatch)
    [r] = watchdog.evaluate(
        snap("STUCK", stuck=["s1"]), cfg, now=NOW, dry_run=True
    )
    assert "idle-probe" in r.detail and "NO_NOTICE" in r.detail
    assert "one-shot courier" in r.detail


def test_probe_state_survives_sane_state():
    st = watchdog._sane_state(
        {"quiet_since": None, "fired_tiers": [],
         "probe": {"last": 5.0, "result": "idle"}}
    )
    assert st["probe"] == {"last": 5.0, "result": "idle"}
    assert watchdog._sane_state({"quiet_since": None, "fired_tiers": [], "probe": "x"}) is None


# ---------------------------------------------------------------------------
# persistent notify storage (.ccdoing/notify.urls)


def _urls_file(cfg):
    p = cfg.project_root / cfg.watchdog.notify_urls_file
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_notify_urls_file_parsed(cfg, monkeypatch):
    monkeypatch.delenv("CCDOING_NOTIFY_URLS", raising=False)
    _urls_file(cfg).write_text(
        "# comment line\n"
        "\n"
        "ntfy://topic-a\n"
        "   ntfy://topic-b   \n"
        "# another comment\n"
    )
    urls, source = watchdog.resolve_notify_urls(cfg)
    assert urls == ["ntfy://topic-a", "ntfy://topic-b"]
    assert source.endswith("notify.urls")


def test_notify_env_overrides_file(cfg, monkeypatch):
    _urls_file(cfg).write_text("ntfy://from-file\n")
    monkeypatch.setenv("CCDOING_NOTIFY_URLS", "ntfy://from-env")
    urls, source = watchdog.resolve_notify_urls(cfg)
    assert urls == ["ntfy://from-env"]
    assert "CCDOING_NOTIFY_URLS" in source


def test_notify_missing_file_names_both_options(cfg, monkeypatch):
    monkeypatch.delenv("CCDOING_NOTIFY_URLS", raising=False)
    urls, source = watchdog.resolve_notify_urls(cfg)
    assert urls == [] and source == ""
    out = watchdog.send_notification(cfg, "t", "b")
    assert "logged only" in out and "no notify URLs configured" in out
    assert "CCDOING_NOTIFY_URLS" in out and "notify.urls" in out


def test_notify_comments_only_file_counts_as_unconfigured(cfg, monkeypatch):
    monkeypatch.delenv("CCDOING_NOTIFY_URLS", raising=False)
    _urls_file(cfg).write_text("# nothing real here\n\n")
    urls, source = watchdog.resolve_notify_urls(cfg)
    assert urls == [] and source == ""


def test_notify_dry_run_names_source(cfg, monkeypatch):
    monkeypatch.delenv("CCDOING_NOTIFY_URLS", raising=False)
    _urls_file(cfg).write_text("ntfy://topic-a\n")
    out = watchdog.send_notification(cfg, "t", "b", dry_run=True)
    assert out.startswith("dry-run: would notify 1 target(s) from ")
    assert "notify.urls" in out


def test_ntfy_subscribe_links():
    assert watchdog.ntfy_subscribe_link("ntfy://mytopic") == "https://ntfy.sh/mytopic"
    assert (watchdog.ntfy_subscribe_link("ntfy://my.host/topic")
            == "https://my.host/topic")
    assert (watchdog.ntfy_subscribe_link("ntfys://my.host/topic")
            == "https://my.host/topic")
    assert watchdog.ntfy_subscribe_link("slack://a/b/c") is None
    assert watchdog.ntfy_subscribe_link("ntfy://") is None


def test_scaffold_notify_urls_file(tmp_path):
    p = watchdog.scaffold_notify_urls_file(tmp_path, ".ccdoing/notify.urls")
    assert p.is_file()
    text = p.read_text()
    assert "one apprise URL per line" in text
    assert "OVERRIDES" in text
    # scaffolding again must not clobber user content
    p.write_text("ntfy://mine\n")
    watchdog.scaffold_notify_urls_file(tmp_path, ".ccdoing/notify.urls")
    assert p.read_text() == "ntfy://mine\n"
