from __future__ import annotations

import json

import pytest
import yaml

from whats_cc_doing import cli
from whats_cc_doing.config import ConfigError, load_config


def write_cfg(tmp_path, doc=None):
    doc = doc or {
        "project_name": "p",
        "signals": [{"type": "git", "label": "git", "weight": "primary"}],
        "watchdog": {"escalation": [{"after_quiet_minutes": 15, "action": "log"}]},
    }
    p = tmp_path / "ccdoing.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


# -- config ----------------------------------------------------------------


def test_load_config_defaults(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert cfg.project_name == "p"
    assert cfg.refresh_seconds == 30
    assert cfg.output_dir == tmp_path / "reports" / "status"
    assert cfg.state_dir == tmp_path / ".ccdoing"
    assert cfg.watchdog.escalation[0].action == "log"


def test_missing_config_raises(tmp_path):
    with pytest.raises(ConfigError, match="ccdoing init"):
        load_config(tmp_path / "nope.yaml")


def test_bad_weight_rejected(tmp_path):
    p = write_cfg(tmp_path, {"signals": [{"type": "git", "weight": "wat"}]})
    with pytest.raises(ConfigError, match="weight"):
        load_config(p)


def test_bad_action_rejected(tmp_path):
    p = write_cfg(
        tmp_path,
        {"signals": [], "watchdog": {"escalation": [{"after_quiet_minutes": 1, "action": "explode"}]}},
    )
    with pytest.raises(ConfigError, match="action"):
        load_config(p)


def test_signal_without_type_rejected(tmp_path):
    p = write_cfg(tmp_path, {"signals": [{"label": "x"}]})
    with pytest.raises(ConfigError, match="type"):
        load_config(p)


def test_tiers_sorted(tmp_path):
    p = write_cfg(
        tmp_path,
        {
            "signals": [],
            "watchdog": {
                "escalation": [
                    {"after_quiet_minutes": 45, "action": "log"},
                    {"after_quiet_minutes": 15, "action": "log"},
                ]
            },
        },
    )
    cfg = load_config(p)
    assert [t.after_quiet_minutes for t in cfg.watchdog.escalation] == [15, 45]


# -- cli -------------------------------------------------------------------


def run_cli(args, cwd, monkeypatch):
    monkeypatch.chdir(cwd)
    return cli.main(args)


def test_init_then_tick_then_status(git_repo, monkeypatch, capsys):
    assert run_cli(["init"], git_repo, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "detected signal" in out

    assert run_cli(["tick"], git_repo, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "wrote" in out and "status.json" in out
    snap = json.loads((git_repo / "reports" / "status" / "status.json").read_text())
    assert snap["verdict"] in ("ACTIVE", "QUIET")
    html = (git_repo / "reports" / "status" / "status.html").read_text()
    assert "What&#x27;s CC Doing" in html or "What's CC Doing" in html

    assert run_cli(["status"], git_repo, monkeypatch) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == snap["verdict"]


def test_init_refuses_overwrite(git_repo, monkeypatch, capsys):
    assert run_cli(["init"], git_repo, monkeypatch) == 0
    capsys.readouterr()
    assert run_cli(["init"], git_repo, monkeypatch) == 1
    assert "--force" in capsys.readouterr().err


def test_tick_without_config_errors_cleanly(tmp_path, monkeypatch, capsys):
    assert run_cli(["tick"], tmp_path, monkeypatch) == 2
    assert "ccdoing init" in capsys.readouterr().err


def test_test_escalation_log_and_notify_dry(git_repo, monkeypatch, capsys):
    run_cli(["init"], git_repo, monkeypatch)
    capsys.readouterr()
    assert run_cli(["test-escalation", "--tier", "log"], git_repo, monkeypatch) == 0
    assert "TEST" in (git_repo / ".ccdoing" / "watchdog.log").read_text()
    monkeypatch.setenv("CCDOING_NOTIFY_URLS", "json://localhost/x")
    assert run_cli(["test-escalation", "--tier", "notify"], git_repo, monkeypatch) == 0
    assert "dry-run" in capsys.readouterr().out


def test_test_escalation_nudge_dry(git_repo, monkeypatch, capsys):
    run_cli(["init"], git_repo, monkeypatch)
    p = git_repo / ".ccdoing" / "nudge-message.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text("Ignore this if you are fine.")
    capsys.readouterr()
    assert run_cli(["test-escalation", "--tier", "nudge"], git_repo, monkeypatch) == 0
    # healthy repo: the DEAD_WAIT precondition correctly skips
    assert "no DEAD_WAIT session" in capsys.readouterr().out


def test_install_prints_systemd_and_cron(git_repo, monkeypatch, capsys):
    run_cli(["init"], git_repo, monkeypatch)
    capsys.readouterr()
    assert run_cli(["install"], git_repo, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "systemctl --user" in out and "enable-linger" in out
    assert run_cli(["install", "--mode", "cron"], git_repo, monkeypatch) == 0
    assert "crontab" in capsys.readouterr().out


def test_doctor_reports_and_arm_check(git_repo, monkeypatch, capsys):
    run_cli(["init"], git_repo, monkeypatch)
    capsys.readouterr()
    run_cli(["doctor"], git_repo, monkeypatch)
    out = capsys.readouterr().out
    assert "config ok" in out
    # arm-check: config exists, never ticked -> warns
    assert run_cli(["doctor", "--arm-check"], git_repo, monkeypatch) == 0
    assert "not running" in capsys.readouterr().out
    # after a tick, arm-check is silent
    run_cli(["tick"], git_repo, monkeypatch)
    capsys.readouterr()
    assert run_cli(["doctor", "--arm-check"], git_repo, monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_init_scaffolds_notify_urls_and_gitignore_hint(git_repo, monkeypatch, capsys):
    assert run_cli(["init"], git_repo, monkeypatch) == 0
    out = capsys.readouterr().out
    # config carries the persistent notify file
    cfg_doc = (git_repo / "ccdoing.yaml").read_text()
    assert "notify_urls_file: .ccdoing/notify.urls" in cfg_doc
    # scaffolded with the documentation header (no URLs yet)
    nf = git_repo / ".ccdoing" / "notify.urls"
    assert nf.is_file() and "one apprise URL per line" in nf.read_text()
    assert "notify targets file:" in out
    # gitignore hint printed for a git repo without coverage
    assert ".ccdoing/*" in out and "!.ccdoing/nudge-message.md" in out


def test_test_escalation_notify_names_source_and_subscribe_link(
    git_repo, monkeypatch, capsys
):
    monkeypatch.delenv("CCDOING_NOTIFY_URLS", raising=False)
    assert run_cli(["init"], git_repo, monkeypatch) == 0
    capsys.readouterr()
    (git_repo / ".ccdoing" / "notify.urls").write_text("ntfy://my-test-topic\n")
    assert run_cli(["test-escalation", "--tier", "notify"], git_repo, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "notify targets (" in out and "notify.urls" in out
    assert "ntfy://my-test-topic" in out
    assert "subscribe: https://ntfy.sh/my-test-topic" in out
    assert "dry-run" in out
