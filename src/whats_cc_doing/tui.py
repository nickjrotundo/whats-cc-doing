"""`ccdoing view`: terminal status viewer for ssh / headless / WSL2 use.

The HTML page assumes you have a browser next to the repo; a lot of real
development happens on boxes where you don't. This renders the same
status.json snapshot as a live terminal view (glances-style): verdict
banner, the at-a-glance primary-signal table, session summary, and any
drift notice. Plain ANSI + full-redraw - no curses, no dependencies -
so it works over ssh, in narrow terminals, and under Windows terminals
that speak VT sequences.

By default it only re-reads status.json on the page's own cadence (the
watchdog loop or cron is the generator). With --fresh it becomes its own
generator, recollecting each cycle via the callable the CLI passes in.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from .util import age_str

_RESET = "\x1b[0m"
_BANNER = {
    "ACTIVE": "\x1b[42;30m",   # green bg
    "QUIET": "\x1b[41;97m",    # red bg
    "DOWN": "\x1b[41;97m",
    "STUCK": "\x1b[43;30m",    # amber bg
}
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_AMBER = "\x1b[33m"
_DIM = "\x1b[2m"


def fmt_age(seconds: float | None) -> str:
    """Human age ('42s', '19h 10m'); '?' for unknown. Thin wrapper over
    util.age_str so the TUI and the HTML page always format ages alike."""
    return "?" if seconds is None else age_str(seconds)


def _c(text: str, code: str, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color and code else text


def _clip(line: str, width: int) -> str:
    return line if len(line) <= width else line[: max(0, width - 1)] + "…"


def render_frame(
    snap: dict | None,
    *,
    width: int = 80,
    color: bool = True,
    now: float | None = None,
    source: str = "",
) -> str:
    """Pure renderer: snapshot dict -> one full frame (testable, no I/O)."""
    now = now if now is not None else time.time()
    out: list[str] = []
    if snap is None:
        out.append("What's CC Doing")
        out.append("")
        out.append(f"no status.json yet ({source or 'not found'})")
        out.append("run `ccdoing tick` in the project first")
        return "\n".join(_clip(l, width) for l in out) + "\n"

    gen_age = fmt_age(now - snap["generated_epoch"]) if snap.get("generated_epoch") else "?"
    # Same title rule as the HTML page: the configured title verbatim,
    # else plain "What's CC Doing" - never a redundant slug concatenation.
    title = str(snap.get("title") or "What's CC Doing")
    updated = f"updated {gen_age} ago"
    pad = max(1, width - len(title) - len(updated))
    out.append(_clip(title + " " * pad + updated, width))

    verdict = str(snap.get("verdict", "?"))
    cause = str(snap.get("cause", ""))
    quiet_for = snap.get("quiet_for_seconds")
    banner = f" {verdict} - {cause}"
    if quiet_for:
        banner += f" (for {fmt_age(quiet_for)})"
    banner = _clip(banner, width)
    out.append(_c(banner + " " * max(0, width - len(banner)), _BANNER.get(verdict, ""), color))
    out.append("")

    sigs = snap.get("signals", [])
    primaries = [s for s in sigs if s.get("weight") == "primary"]
    healths = [s for s in sigs if s.get("weight") == "health"]
    label_w = min(36, max((len(s.get("label", "")) for s in primaries + healths), default=10))
    for s in primaries:
        state_note = s.get("state", "ok")
        if s.get("fresh"):
            val = _c("ACTIVE", _GREEN, color)
        else:
            age = fmt_age(s.get("age_seconds")) if s.get("age_seconds") is not None else None
            val = _c(f"inactive ({age} ago)" if age else "inactive", _DIM, color)
        if state_note != "ok":
            val += _c(f"  [{state_note} - config?]", _AMBER, color)
        out.append(_clip(f"  {s.get('label', '?'):<{label_w}}  {val}", width + 16))
    for s in healths:
        if s.get("healthy") is True:
            val = _c("UP", _GREEN, color)
        elif s.get("healthy") is False:
            val = _c("DOWN", _RED, color)
        else:
            val = _c("?", _DIM, color)
        out.append(_clip(f"  {s.get('label', '?'):<{label_w}}  {val}", width + 16))

    counts: dict[str, int] = {}
    for s in sigs:
        for sess in s.get("sessions") or []:
            st = sess.get("state", "?")
            counts[st] = counts.get(st, 0) + 1
    if counts:
        order = ["WORKING", "WAITING_ON", "DEAD_WAIT", "ABANDONED", "IDLE"]
        parts = [f"{counts[k]} {k}" for k in order if k in counts]
        parts += [f"{v} {k}" for k, v in counts.items() if k not in order]
        out.append("")
        out.append(_clip(f"  sessions: {', '.join(parts)}", width))

    maintenance = snap.get("maintenance") or []
    if maintenance:
        out.append("")
        out.append(_c(
            _clip(f"  drift: {len(maintenance)} finding(s) - ccdoing doctor --drift", width),
            _AMBER, color,
        ))
    return "\n".join(out) + "\n"


def _read_snapshot(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None


import contextlib
import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _pad_visible(s: str, width: int) -> str:
    """Pad (or hard-clip) to a visible width, ANSI-aware."""
    vis = _visible_len(s)
    if vis > width:
        s = _clip(_ANSI_RE.sub("", s), width)  # drop color rather than misalign
        vis = _visible_len(s)
    return s + " " * (width - vis)


@contextlib.contextmanager
def _term_session():
    """cbreak + hidden cursor for the interactive loops; always restores."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write("\x1b[?25l")
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\x1b[?25h" + _RESET + "\n")
        sys.stdout.flush()


_ARROWS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}


def decode_keys(buf: bytes, *, flush: bool = False) -> tuple[list[str], bytes]:
    """Pure decoder: raw terminal bytes -> (keys, undecoded remainder).

    Arrow keys arrive as multi-byte escape sequences (CSI `\\x1b[A` or
    application-mode SS3 `\\x1bOA`); this consumes whole sequences so no
    byte of one ever leaks out as a literal character (the original
    per-char reader leaked `[`/`D` and made LEFT open the days prompt).
    A trailing incomplete sequence stays in the remainder for the caller
    to complete on the next read; with flush=True (follow-up read timed
    out) a trailing lone ESC is emitted as "ESC" and any other partial
    sequence is dropped. Unknown complete CSI/SS3 sequences (PgUp, F5,
    ...) are swallowed whole, never surfaced as characters.
    """
    keys: list[str] = []
    i, n = 0, len(buf)
    while i < n:
        b = buf[i]
        if b == 0x1B:
            if i + 1 >= n:  # lone ESC so far - complete or flush
                if flush:
                    keys.append("ESC")
                    i += 1
                    continue
                break
            nxt = buf[i + 1]
            if nxt == ord("["):  # CSI: params, then final byte 0x40-0x7e
                j = i + 2
                while j < n and not (0x40 <= buf[j] <= 0x7E):
                    j += 1
                if j >= n:  # incomplete sequence
                    if flush:
                        i = n
                        continue
                    break
                key = _ARROWS.get(chr(buf[j]))
                if key:
                    keys.append(key)
                i = j + 1
                continue
            if nxt == ord("O"):  # SS3 application cursor keys
                if i + 2 >= n:
                    if flush:
                        i = n
                        continue
                    break
                key = _ARROWS.get(chr(buf[i + 2]))
                if key:
                    keys.append(key)
                i += 3
                continue
            keys.append("ESC")  # ESC followed by an ordinary char
            i += 1
            continue
        if b in (0x0D, 0x0A):
            keys.append("ENTER")
            i += 1
            continue
        if b == 0x7F:
            keys.append("BACKSPACE")
            i += 1
            continue
        if b < 0x20:  # other control chars: swallow
            i += 1
            continue
        if b < 0x80:
            keys.append(chr(b))
            i += 1
            continue
        # multi-byte UTF-8: decode when complete, hold when truncated
        for ln in (2, 3, 4):
            if i + ln <= n:
                try:
                    keys.append(buf[i : i + ln].decode("utf-8"))
                    i += ln
                    break
                except UnicodeDecodeError:
                    continue
        else:
            if i + 4 > n and not flush:
                break  # possibly truncated at chunk boundary
            i += 1  # invalid byte: swallow
    return keys, buf[i:]


_key_queue: list[str] = []
_raw_buf = b""


def _reset_key_state() -> None:  # for tests
    global _raw_buf
    _key_queue.clear()
    _raw_buf = b""


def _read_key(timeout: float = 0.25) -> str | None:
    """One keypress or None: printable char, or UP/DOWN/LEFT/RIGHT/ESC/
    ENTER/BACKSPACE. Reads the raw fd with os.read (never the buffered
    sys.stdin text stream - buffering there is what broke arrow keys:
    select() watches the fd while read(1) hoards the rest of the escape
    sequence inside Python where select can't see it)."""
    global _raw_buf
    if _key_queue:
        return _key_queue.pop(0)
    fd = sys.stdin.fileno()

    def _drain(wait: float) -> bool:
        global _raw_buf
        r, _, _ = select.select([fd], [], [], wait)
        if not r:
            return False
        try:
            chunk = os.read(fd, 64)
        except OSError:
            return False
        _raw_buf += chunk
        return bool(chunk)

    if not _drain(timeout):
        if _raw_buf:  # a partial sequence that never completed
            keys, _ = decode_keys(_raw_buf, flush=True)
            _raw_buf = b""
            _key_queue.extend(keys)
        return _key_queue.pop(0) if _key_queue else None
    keys, _raw_buf = decode_keys(_raw_buf)
    _key_queue.extend(keys)
    if not _key_queue and _raw_buf:
        # Probably a lone ESC: give the rest of a sequence 50ms to arrive.
        _drain(0.05)
        keys, _raw_buf = decode_keys(_raw_buf)
        if not keys:
            keys, _ = decode_keys(_raw_buf, flush=True)
            _raw_buf = b""
        _key_queue.extend(keys)
    return _key_queue.pop(0) if _key_queue else None


def _view_loop(
    status_json: Path,
    *,
    interval: float,
    refresh_fn: Callable[[], None] | None,
    color: bool,
    allow_back: bool = False,
) -> str:
    """Interactive single-project loop (terminal already in cbreak).

    Returns "quit" (q / ctrl-c) or, when allow_back, "back" (b / Esc / Left).
    """
    while True:
        if refresh_fn:
            refresh_fn()
        size = shutil.get_terminal_size((80, 24))
        frame = render_frame(_read_snapshot(status_json), width=size.columns,
                             color=color, source=str(status_json))
        footer = f"  refresh {interval:g}s · q quit"
        if allow_back:
            footer += " · b/esc back to projects"
        sys.stdout.write("\x1b[H\x1b[J" + frame + "\n" + _c(footer, _DIM, color) + "\n")
        sys.stdout.flush()
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline:
            key = _read_key()
            if key is None:
                continue
            low = key.lower() if len(key) == 1 else key
            if low == "q":
                return "quit"
            if allow_back and (key in ("ESC", "LEFT") or low == "b"):
                return "back"


def view(
    status_json: Path,
    *,
    interval: float = 30.0,
    refresh_fn: Callable[[], None] | None = None,
    once: bool = False,
    color: bool | None = None,
) -> int:
    """Live loop: (optionally regenerate,) read, render, wait; q/ctrl-c exits."""
    is_tty = sys.stdout.isatty() and sys.stdin.isatty()
    if color is None:
        color = is_tty and not sys.platform.startswith("win")
    if once or not is_tty:
        if refresh_fn:
            refresh_fn()
        cols = shutil.get_terminal_size((80, 24)).columns
        sys.stdout.write(render_frame(_read_snapshot(status_json), width=cols,
                                      color=color, source=str(status_json)))
        return 0
    try:
        with _term_session():
            _view_loop(status_json, interval=interval, refresh_fn=refresh_fn,
                       color=color)
    except KeyboardInterrupt:
        pass
    return 0


# --------------------------------------------------------------------------
# Multi-project dashboard (`ccdoing view` outside a project / `--dash`).

_VERDICT_TUI = {
    "ACTIVE": _GREEN,
    "DOWN": _RED,
    "STUCK": _AMBER,
    "QUIET": _DIM,
}
_SPLIT_MIN_WIDTH = 110
_QUAD_MIN_WIDTH = 200


def render_dash_frame(
    cards,
    *,
    now: float | None = None,
    selected: int = 0,
    days: float = 4.0,
    total: int | None = None,
    width: int = 80,
    color: bool = True,
) -> str:
    """Pure renderer for the project list (testable, no I/O)."""
    now = now if now is not None else time.time()
    out: list[str] = []
    header = "What's CC Doing - all projects"
    scope = f"active within {days:g}d"
    if total is not None and total > len(cards):
        scope += f" · {total - len(cards)} older hidden"
    pad = max(1, width - len(header) - len(scope))
    out.append(_clip(header + " " * pad + scope, width))
    out.append("")
    if not cards:
        out.append("  no projects with recent activity")
        out.append(_c("  (d changes the day range; ccdoing init registers a project)",
                      _DIM, color))
        return "\n".join(out) + "\n"
    title_w = min(34, max(12, max(len(c.title) for c in cards)))
    for i, c in enumerate(cards):
        cursor = "> " if i == selected else "  "
        verdict = _c(f"{c.verdict:<8}", _VERDICT_TUI.get(c.verdict, _DIM), color)
        age = c.last_signal_age(now)
        last = f"Last signal: {fmt_age(age)} ago" if age is not None else "no data"
        stale = ""
        if c.generator_stale:
            stale = _c("  [stale]", _AMBER, color)
        line = f"{cursor}{c.title:<{title_w}}  {verdict} {last}{stale}"
        if i == selected and color:
            line = f"\x1b[7m{_ANSI_RE.sub('', line)}{_RESET}"  # reverse video row
        out.append(_pad_visible(line, width))
    return "\n".join(out) + "\n"


def _render_split_frame(cards, *, width: int, color: bool) -> str:
    """2-up (or 2x2 on very wide terminals) abridged status panes."""
    n = 4 if width >= _QUAD_MIN_WIDTH and len(cards) >= 3 else 2
    shown = cards[:n]
    per_row = 2
    pane_w = (width - 3 * (per_row - 1)) // per_row
    panes: list[list[str]] = []
    for c in shown:
        snap = _read_snapshot(c.output_dir / "status.json") if c.output_dir else None
        frame = render_frame(snap, width=pane_w, color=color,
                             source=str(c.root)).splitlines()
        panes.append(frame)
    rows_out: list[str] = []
    for r in range(0, len(panes), per_row):
        row = panes[r:r + per_row]
        height = max(len(p) for p in row)
        for p in row:
            p.extend([""] * (height - len(p)))
        for lines in zip(*row):
            rows_out.append(" │ ".join(_pad_visible(l, pane_w) for l in lines))
        if r + per_row < len(panes):
            rows_out.append("─" * min(width, pane_w * per_row + 3))
    return "\n".join(rows_out) + "\n"


def _input_days(current: float, color: bool) -> float | None:
    """Inline numeric prompt on the bottom line; Enter accepts, Esc cancels."""
    buf = ""
    while True:
        prompt = f"  active within days: {buf}_"
        sys.stdout.write("\r\x1b[K" + _c(prompt, "", color))
        sys.stdout.flush()
        key = _read_key(timeout=30.0)
        if key is None or key == "ESC":
            return None
        if key == "ENTER":
            try:
                v = float(buf)
                return v if v >= 0 else None
            except ValueError:
                return None
        if key == "BACKSPACE":
            buf = buf[:-1]
        elif len(key) == 1 and (key.isdigit() or (key == "." and "." not in buf)):
            buf += key


def dashboard(
    *,
    interval: float = 30.0,
    days: float = 4.0,
    once: bool = False,
    color: bool | None = None,
) -> int:
    """All-projects dashboard: arrows + Enter to open a project, b/Esc back,
    d changes the day range, s toggles split panes, q quits."""
    from . import dash  # local import: dash pulls in config/registry

    is_tty = sys.stdout.isatty() and sys.stdin.isatty()
    if color is None:
        color = is_tty and not sys.platform.startswith("win")

    def _load(now: float):
        all_cards = dash.load_cards(now)
        return dash.filter_recent(all_cards, days, now), len(all_cards)

    if once or not is_tty:
        now = time.time()
        cards, total = _load(now)
        cols = shutil.get_terminal_size((80, 24)).columns
        sys.stdout.write(render_dash_frame(
            cards, now=now, selected=-1, days=days, total=total,
            width=cols, color=color,
        ))
        return 0

    selected = 0
    split = False
    note = ""
    try:
        with _term_session():
            while True:
                now = time.time()
                cards, total = _load(now)
                selected = max(0, min(selected, len(cards) - 1))
                size = shutil.get_terminal_size((80, 24))
                if split and size.columns >= _SPLIT_MIN_WIDTH and cards:
                    frame = _render_split_frame(cards, width=size.columns,
                                                color=color)
                    footer = "  s list · q quit"
                else:
                    if split:  # wanted split, can't have it
                        split = False
                        note = "terminal too narrow for split panes"
                    frame = render_dash_frame(
                        cards, now=now, selected=selected, days=days,
                        total=total, width=size.columns, color=color,
                    )
                    footer = ("  ↑/↓ or j/k select · enter open · d days "
                              f"({days:g}) · s multi-view · q quit")
                if note:
                    footer += _c(f"  [{note}]", _AMBER, color)
                    note = ""
                sys.stdout.write("\x1b[H\x1b[J" + frame + "\n"
                                 + _c(footer, _DIM, color) + "\n")
                sys.stdout.flush()
                deadline = time.monotonic() + interval
                while time.monotonic() < deadline:
                    key = _read_key()
                    if key is None:
                        continue
                    low = key.lower() if len(key) == 1 else key
                    if low == "q":
                        return 0
                    if low == "s":
                        split = not split
                        break
                    if split and (key in ("ESC", "LEFT") or low == "b"):
                        split = False
                        break
                    if key == "UP" or low == "k":
                        selected = max(0, selected - 1)
                        break
                    if key == "DOWN" or low == "j":
                        selected = min(len(cards) - 1, selected + 1)
                        break
                    if low == "d":
                        v = _input_days(days, color)
                        if v is not None:
                            days = v
                        break
                    if key == "ENTER" and cards:
                        card = cards[selected]
                        if card.output_dir is None:
                            note = "project has no status output yet"
                            break
                        outcome = _view_loop(
                            card.output_dir / "status.json",
                            interval=float(card.refresh_seconds),
                            refresh_fn=None, color=color, allow_back=True,
                        )
                        if outcome == "quit":
                            return 0
                        break
    except KeyboardInterrupt:
        pass
    return 0
