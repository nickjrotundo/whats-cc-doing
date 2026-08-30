"""Load and validate ccdoing.yaml.

The config lives at the monitored project's root. Secrets never go in the
file: notification URLs are named by ENV VAR, resolved at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = "ccdoing.yaml"
STATE_DIRNAME = ".ccdoing"

VALID_WEIGHTS = {"primary", "info", "health", "alert"}
VALID_ACTIONS = {"log", "notify", "nudge"}


class ConfigError(Exception):
    """Raised for a malformed or missing ccdoing.yaml."""


@dataclass
class VerdictConfig:
    active_window_minutes: float = 15.0
    health_failure_is_down: bool = True


@dataclass
class EscalationTier:
    after_quiet_minutes: float
    action: str  # log | notify | nudge
    cooldown_minutes: float = 60.0
    max_per_day: int = 3


@dataclass
class WatchdogConfig:
    enabled: bool = True
    check_interval_seconds: int = 60
    escalation: list[EscalationTier] = field(default_factory=list)
    nudge_message: str = ".ccdoing/nudge-message.md"
    # Persistent notify targets: one apprise URL per line, # comments
    # allowed. Read by every tick, so cron/systemd watchdogs see the URLs
    # without any env plumbing. $CCDOING_NOTIFY_URLS (notify_urls_env)
    # overrides this file when set.
    notify_urls_file: str = ".ccdoing/notify.urls"


@dataclass
class Config:
    project_name: str
    project_root: Path
    output_dir: Path
    # Page title. None -> the page shows plain "What's CC Doing"; setup
    # (the /ccdoing:setup skill) picks something meaningful per project.
    title: str | None = None
    # 'local' (default - the reader lives in local time) or 'utc'.
    timezone: str = "local"
    refresh_seconds: int = 30
    verdict: VerdictConfig = field(default_factory=VerdictConfig)
    signals: list[dict[str, Any]] = field(default_factory=list)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    notify_urls_env: str = "CCDOING_NOTIFY_URLS"
    drift_stale_after_days: float = 7.0
    # Absolute path of the file this config was loaded from, so a daemon
    # respawn can be pointed at the same file regardless of its cwd.
    source_path: Path | None = None

    @property
    def state_dir(self) -> Path:
        return self.project_root / STATE_DIRNAME

    @property
    def active_window_s(self) -> float:
        return self.verdict.active_window_minutes * 60.0

    @property
    def drift_stale_after_s(self) -> float:
        return self.drift_stale_after_days * 86400.0


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d:
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    return d[key]


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config not found: {path} (run 'ccdoing init' to create one)")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    root = path.resolve().parent
    signals = raw.get("signals") or []
    if not isinstance(signals, list):
        raise ConfigError("signals: must be a list")
    for i, sig in enumerate(signals):
        if not isinstance(sig, dict):
            raise ConfigError(f"signals[{i}]: must be a mapping")
        _require(sig, "type", f"signals[{i}]")
        weight = sig.get("weight", "primary")
        if weight not in VALID_WEIGHTS:
            raise ConfigError(
                f"signals[{i}]: weight '{weight}' not one of {sorted(VALID_WEIGHTS)}"
            )

    vraw = raw.get("verdict") or {}
    verdict = VerdictConfig(
        active_window_minutes=float(vraw.get("active_window_minutes", 15)),
        health_failure_is_down=bool(vraw.get("health_failure_is_down", True)),
    )

    wraw = raw.get("watchdog") or {}
    tiers = []
    for i, traw in enumerate(wraw.get("escalation") or []):
        action = _require(traw, "action", f"watchdog.escalation[{i}]")
        if action not in VALID_ACTIONS:
            raise ConfigError(
                f"watchdog.escalation[{i}]: action '{action}' not one of {sorted(VALID_ACTIONS)}"
            )
        tiers.append(
            EscalationTier(
                after_quiet_minutes=float(
                    _require(traw, "after_quiet_minutes", f"watchdog.escalation[{i}]")
                ),
                action=action,
                cooldown_minutes=float(traw.get("cooldown_minutes", 60)),
                max_per_day=int(traw.get("max_per_day", 3)),
            )
        )
    minutes_seen = [t.after_quiet_minutes for t in tiers]
    if len(set(minutes_seen)) != len(minutes_seen):
        # fired_tiers is keyed by the minute value, so two tiers sharing one
        # would mean the second silently never fires - reject early.
        raise ConfigError(
            "watchdog.escalation: duplicate after_quiet_minutes values "
            f"({sorted(minutes_seen)}); each tier needs a distinct threshold"
        )
    tiers.sort(key=lambda t: t.after_quiet_minutes)
    watchdog = WatchdogConfig(
        enabled=bool(wraw.get("enabled", True)),
        check_interval_seconds=int(wraw.get("check_interval_seconds", 60)),
        escalation=tiers,
        nudge_message=str(
            wraw.get("nudge_message", ".ccdoing/nudge-message.md")
        ),
        notify_urls_file=str(
            wraw.get("notify_urls_file", ".ccdoing/notify.urls")
        ),
    )

    out = raw.get("output_dir", "reports/status")
    output_dir = Path(out)
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    draw = raw.get("drift") or {}
    if not isinstance(draw, dict):
        raise ConfigError("drift: must be a mapping")

    tz = str(raw.get("timezone", "local"))
    if tz not in ("local", "utc"):
        raise ConfigError(f"timezone: '{tz}' must be 'local' or 'utc'")

    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        raise ConfigError("title: must be a string")

    return Config(
        project_name=str(raw.get("project_name") or root.name),
        project_root=root,
        output_dir=output_dir,
        title=title,
        timezone=tz,
        refresh_seconds=int(raw.get("refresh_seconds", 30)),
        verdict=verdict,
        signals=signals,
        watchdog=watchdog,
        notify_urls_env=str(raw.get("notify_urls_env", "CCDOING_NOTIFY_URLS")),
        drift_stale_after_days=float(draw.get("stale_after_days", 7)),
        source_path=path.resolve(),
    )
