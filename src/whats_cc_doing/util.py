"""Small shared helpers (no package-internal imports; safe everywhere)."""

from __future__ import annotations

import time


def age_str(seconds: float | None) -> str:
    """Humanize an age in seconds, two units max, space-separated, no
    zero-padding: '42s', '5m 12s', '19h 8m', '3d 4h'. None -> '-'."""
    if seconds is None:
        return "-"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, rem = divmod(s, 60)
        return f"{m}m {rem}s" if rem else f"{m}m"
    if s < 86400:
        h, rem = divmod(s, 3600)
        m = rem // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"{d}d {h}h" if h else f"{d}d"


def fmt_timestamp(epoch: float, tz: str = "local") -> str:
    """Human-readable wall-clock time: '2026-08-30 9:41 AM'.

    tz='local' uses the machine's timezone (the default - the person
    reading the page lives in local time); tz='utc' appends ' UTC'.
    Hand-built 12-hour format because %-I is glibc-only.
    """
    t = time.gmtime(epoch) if tz == "utc" else time.localtime(epoch)
    hour = t.tm_hour % 12 or 12
    ampm = "AM" if t.tm_hour < 12 else "PM"
    out = f"{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d} {hour}:{t.tm_min:02d} {ampm}"
    return f"{out} UTC" if tz == "utc" else out
