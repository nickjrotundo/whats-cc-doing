from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from whats_cc_doing.config import Config, EscalationTier, VerdictConfig, WatchdogConfig


NOW = 1_800_000_000.0  # fixed 'now' for deterministic tests


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path_factory, monkeypatch):
    """Keep every test's project registry out of the user's real XDG state.

    cmd_init registers projects; without this, running the suite would
    litter ~/.local/state/ccdoing/projects.json with pytest tmp dirs.
    """
    monkeypatch.setenv(
        "XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state"))
    )


@pytest.fixture
def now() -> float:
    return NOW


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path / "proj"


@pytest.fixture
def cfg(project: Path) -> Config:
    project.mkdir(parents=True, exist_ok=True)
    return Config(
        project_name="testproj",
        project_root=project,
        output_dir=project / "reports" / "status",
        verdict=VerdictConfig(active_window_minutes=15),
        watchdog=WatchdogConfig(
            enabled=True,
            escalation=[
                EscalationTier(after_quiet_minutes=15, action="log"),
                EscalationTier(after_quiet_minutes=30, action="notify"),
                EscalationTier(
                    after_quiet_minutes=45, action="nudge",
                    cooldown_minutes=60, max_per_day=2,
                ),
            ],
        ),
    )


def git(repo: Path, *args: str, env_extra: dict | None = None) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(repo), "PATH": "/usr/bin:/bin",
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "first")
    return repo


def touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("")
    import os

    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def claude_tree(tmp_path: Path):
    """Fabricated Claude Code artifact tree (invented content only).

    Returns (claude_home, task_root, project_dir, slug).
    """
    from whats_cc_doing.harness import slug_for

    project_dir = tmp_path / "work" / "myproj"
    project_dir.mkdir(parents=True)
    slug = slug_for(project_dir)
    claude_home = tmp_path / "dot-claude"
    (claude_home / "projects" / slug).mkdir(parents=True)
    task_root = tmp_path / "tasks-root"
    (task_root / slug).mkdir(parents=True)
    return claude_home, task_root, project_dir, slug


def write_transcript(claude_home: Path, slug: str, session_id: str, mtime: float,
                     last_type: str = "assistant", cwd: str | None = None) -> Path:
    import json

    p = claude_home / "projects" / slug / f"{session_id}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    first: dict = {"type": "user", "message": "invented fixture line"}
    last: dict = {"type": last_type, "message": "invented fixture line"}
    if cwd is not None:
        first["cwd"] = cwd
        last["cwd"] = cwd
    lines = [json.dumps(first), json.dumps(last)]
    p.write_text("\n".join(lines) + "\n")
    touch(p, mtime)
    return p
