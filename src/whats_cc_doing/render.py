"""Render the human status page from the snapshot dict.

One input (the status.json snapshot), one output (a single dark-theme
HTML page, everything escaped, meta-refresh). Serve it with caching
DISABLED - a cached status page is a lying status page.

JS on the page is viewer-side only and degrades silently when absent:
the stale-page self-check (the page knows when it was generated, so it
can notice the generator has stopped updating it), and an opt-in
browser-notification toggle that fires on verdict transitions while the
page is open (needs the Notification API - a secure context such as
`ccdoing serve` on localhost; file:// usually lacks it, so the toggle
hides itself there).
"""

from __future__ import annotations

import html
import json
from typing import Any

from .util import age_str as _age
from .util import fmt_timestamp

_BANNER = {
    "ACTIVE": ("#1f8a4c", "work is moving"),
    "QUIET": ("#b03030", "no activity signals - possibly stalled"),
    "DOWN": ("#7a1f1f", "a health check is failing"),
    "STUCK": ("#c97a1a", "a session appears parked on dead work"),
}

_STATE_COLOR = {
    "WORKING": "#2ecc71",
    "WAITING_ON": "#7aa2f7",
    "DEAD_WAIT": "#e67e22",
    "ABANDONED": "#8a7a55",
    "IDLE": "#888888",
    "UNKNOWN": "#666666",
}

_ACTIVE_GREEN = "#1f8a4c"
_INACTIVE_GREY = "#888"
_DOWN_RED = "#e74c3c"

# Full-table sort: activity drivers first, then health, alert, info.
_WEIGHT_ORDER = {"primary": 0, "health": 1, "alert": 2, "info": 3}


def _e(v: Any) -> str:
    return html.escape(str(v), quote=True)


def render_html(snap: dict[str, Any]) -> str:
    verdict = snap.get("verdict", "QUIET")
    color, blurb = _BANNER.get(verdict, ("#555", ""))
    quiet_for = snap.get("quiet_for_seconds")
    quiet_note = (
        f" (for {_age(quiet_for)})" if quiet_for and verdict in ("QUIET", "STUCK") else ""
    )
    title = snap.get("title") or "What's CC Doing"
    refresh_s = int(snap.get("refresh_seconds", 30))
    epoch = float(snap.get("generated_epoch") or 0)
    tz = snap.get("timezone", "local")
    generated_human = fmt_timestamp(epoch, tz) if epoch else _e(snap.get("generated_at"))

    signals = list(snap.get("signals", []))

    maint = snap.get("maintenance") or []
    maint_html = (
        f"<p class=\"maint\">config drift: {_e('; '.join(maint))} "
        "&middot; run <span class=mono>ccdoing doctor --drift</span></p>"
        if maint else ""
    )

    sections = []
    for sig in signals:
        if sig.get("sessions"):
            sections.append(_sessions_section(sig))
        elif sig.get("lines"):
            body = "\n".join(_e(l) for l in sig["lines"])
            sections.append(
                f"<h2>{_e(sig.get('label'))}</h2><pre>{body}</pre>"
            )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh_s}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
  body {{ background:#12141a; color:#d7dae0; font: 14px/1.5 system-ui, sans-serif;
         max-width: 980px; margin: 0 auto; padding: 16px; }}
  h1 {{ font-size: 20px; margin: 8px 0; }}
  h2 {{ font-size: 14px; color: #9aa3b2; margin: 22px 0 6px;
        text-transform: uppercase; letter-spacing: .05em; }}
  .banner {{ background:{color}; color:#fff; padding:12px 16px; border-radius:8px;
             font-size:17px; font-weight:700; }}
  .stale-banner {{ background:#7a1f1f; color:#fff; padding:12px 16px;
                   border-radius:8px; font-size:15px; font-weight:700;
                   margin-bottom:10px; }}
  .cause {{ color:#b8bfca; margin:8px 2px 0; }}
  .meta {{ color:#78808f; font-size:12px; margin-top:6px; }}
  .maint {{ color:#e0b341; font-size:12px; margin:6px 2px 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid #262a33;
            font-size:13px; vertical-align: top; }}
  th {{ color:#9aa3b2; font-weight:600; }}
  .activity td:last-child {{ font-weight:700; }}
  details {{ margin-top: 10px; }}
  summary {{ color:#9aa3b2; font-size:13px; cursor:pointer; }}
  pre {{ background:#191c24; padding:10px; border-radius:6px; overflow-x:auto;
         font-size:12px; }}
  .mono {{ font-family: ui-monospace, monospace; font-size:12px; }}
  #bn-toggle {{ background:#191c24; color:#9aa3b2; border:1px solid #262a33;
                border-radius:6px; padding:3px 10px; font-size:12px;
                cursor:pointer; }}
  #bn-toggle.on {{ color:#2ecc71; border-color:#1f8a4c; }}
</style></head><body>
<div id="stale" class="stale-banner" hidden></div>
<h1>{_e(title)}</h1>
<div class="banner">{_e(verdict)}{_e(quiet_note)} &mdash; {_e(blurb)}</div>
<p class="cause">{_e(snap.get('cause', ''))}</p>
<p class="meta">generated {_e(generated_human)} &middot;
refreshes every {refresh_s}s &middot;
active window {_e(snap.get('active_window_minutes'))}m &middot;
{_e(snap.get('generator'))} &middot; machine view: status.json</p>
<p class="meta"><button id="bn-toggle" hidden
 title="Fires a browser notification on verdict changes while this page is open. Needs the Notification API - works over ccdoing serve (localhost); file:// usually lacks it."></button>
<span id="bn-state"></span></p>
{maint_html}
<h2>Activity signals</h2>
{_activity_table(signals)}
<details><summary>All signals</summary>
{_full_table(signals)}
</details>
{''.join(sections)}
{_stale_script(epoch, refresh_s)}
{notification_script(snap.get('project') or title, title, verdict,
                     snap.get('cause', ''))}
</body></html>
"""


def _activity_table(signals: list[dict[str, Any]]) -> str:
    """The at-a-glance table (the most-used part of the page): one row per
    primary signal, ACTIVE/inactive; health rows appear only when DOWN."""
    rows = []
    for sig in signals:
        weight = sig.get("weight")
        if weight == "primary":
            drifted = sig.get("state") in ("no-match", "stale")
            active = bool(sig.get("ok")) and bool(sig.get("fresh")) and not drifted
            word = "ACTIVE" if active else "inactive"
            color = _ACTIVE_GREEN if active else _INACTIVE_GREY
            mark = (
                "<span title=\"no recent match - possible config drift; "
                "run ccdoing doctor --drift\">&#8225;</span>"
                if drifted else ""
            )
            rows.append(
                f"<tr><td>{_e(sig.get('label'))}</td>"
                f"<td style='color:{color}'>{word}{mark}</td></tr>"
            )
        elif weight == "health" and sig.get("ok") and sig.get("healthy") is False:
            rows.append(
                f"<tr><td>{_e(sig.get('label'))}</td>"
                f"<td style='color:{_DOWN_RED}'>DOWN</td></tr>"
            )
    if not rows:
        return "<p class=meta>(no primary signals configured)</p>"
    return f"<table class=activity><tbody>{''.join(rows)}</tbody></table>"


def _full_table(signals: list[dict[str, Any]]) -> str:
    ordered = sorted(
        signals, key=lambda s: _WEIGHT_ORDER.get(s.get("weight"), 9)
    )
    rows = []
    for sig in ordered:
        state, scolor = _signal_state(sig)
        detail = sig.get("detail") or sig.get("error") or ""
        if sig.get("state") in ("no-match", "stale"):
            detail = f"{detail} - may be misconfigured; run ccdoing doctor --drift"
        rows.append(
            "<tr>"
            f"<td>{_e(sig.get('label'))}</td>"
            f"<td class=mono>{_e(sig.get('type'))}</td>"
            f"<td style='color:{scolor};font-weight:600'>{_e(state)}</td>"
            f"<td>{_e(_age(sig.get('age_seconds')))}</td>"
            f"<td>{_e(detail)}</td>"
            "</tr>"
        )
    return (
        "<table>\n<tr><th>signal</th><th>type</th><th>state</th>"
        "<th>age</th><th>detail</th></tr>\n" + "".join(rows) + "\n</table>"
    )


BAD_VERDICTS_JS = '["QUIET","DOWN","STUCK"]'


def notification_script(project: str, title: str, verdict: str, cause: str) -> str:
    """Opt-in browser notifications for a single status page. Pure
    viewer-side: enable-flag and last-seen verdict live in the viewer's
    localStorage, keyed per project (the last-seen key is shared with the
    dashboard so either page can notice a transition). Feature-detects
    the Notification API and keeps the toggle hidden when it's absent
    (file:// usually). Meta-refresh reloads re-run this, which is what
    turns 'compare against last seen' into transition detection."""
    # json.dumps gives JS-safe quoted strings (quotes, newlines), but NOT
    # HTML-safe ones: a literal </script> inside the string would close
    # the tag. Escaping every "<" as unicode-escape fixes that class.
    pj, tj, vj, cj = (
        json.dumps(s or "").replace("<", "\\u003c")
        for s in (project, title, verdict, cause)
    )
    return f"""<script>
(function () {{
  try {{
    if (!("Notification" in window)) return;
    var proj = {pj}, title = {tj}, verdict = {vj}, cause = {cj};
    var enKey = "ccdoing-bn:" + proj, seenKey = "ccdoing-bn-last:" + proj;
    function ls(fn, fallback) {{ try {{ return fn(); }} catch (e) {{ return fallback; }} }}
    function enabled() {{ return ls(function () {{ return localStorage.getItem(enKey) === "1"; }}, false); }}
    var btn = document.getElementById("bn-toggle");
    function paint() {{
      btn.hidden = false;
      btn.classList.toggle("on", enabled());
      btn.textContent = enabled()
        ? "browser notifications: on"
        : "enable browser notifications";
    }}
    btn.addEventListener("click", function () {{
      if (enabled()) {{
        ls(function () {{ localStorage.setItem(enKey, "0"); }});
        paint(); return;
      }}
      Notification.requestPermission().then(function (p) {{
        if (p === "granted") ls(function () {{ localStorage.setItem(enKey, "1"); }});
        paint();
      }});
    }});
    paint();
    var prev = ls(function () {{ return localStorage.getItem(seenKey); }}, null);
    ls(function () {{ localStorage.setItem(seenKey, verdict); }});
    if (!enabled() || Notification.permission !== "granted") return;
    if (prev === null || prev === verdict) return;
    var bad = {BAD_VERDICTS_JS};
    var wasBad = bad.indexOf(prev) !== -1, isBad = bad.indexOf(verdict) !== -1;
    if (isBad || (verdict === "ACTIVE" && wasBad)) {{
      new Notification(title, {{ body: verdict + " - " + cause }});
    }}
  }} catch (err) {{ /* notifications are a convenience, never a failure */ }}
}})();
</script>"""


def _stale_script(epoch: float, refresh_s: int) -> str:
    """Self-check: the page flags itself when the generator stops updating
    it. Works from file:// and http, no external requests; meta-refresh
    keeps reloading, so each reload of a stale file re-fires this."""
    threshold = max(2.5 * refresh_s, 90.0)
    return f"""<script>
(function () {{
  try {{
    var GEN = {epoch:.3f}, THRESH = {threshold:.0f};
    function fmt(s) {{
      s = Math.floor(s);
      if (s < 60) return s + "s";
      if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
      return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    }}
    function check() {{
      var age = Date.now() / 1000 - GEN;
      var el = document.getElementById("stale");
      if (!el) return;
      if (age > THRESH) {{
        el.textContent = "status page is stale - generator appears down (last update "
          + fmt(age) + " ago)";
        el.hidden = false;
      }} else {{
        el.hidden = true;
      }}
    }}
    check();
    setInterval(check, 15000);
  }} catch (e) {{ /* degrade silently */ }}
}})();
</script>"""


def _signal_state(sig: dict[str, Any]) -> tuple[str, str]:
    if not sig.get("ok"):
        return "unreadable", "#e74c3c"
    # Drift states outrank freshness display: a target that matches nothing
    # is a config question, not an activity reading. (see drift.py)
    if sig.get("state") == "no-match":
        return "no-match", "#e0b341"
    if sig.get("state") == "stale":
        return "stale (config?)", "#e67e22"
    if sig.get("weight") == "health":
        return ("up", "#2ecc71") if sig.get("healthy") else ("DOWN", "#e74c3c")
    if sig.get("weight") == "alert":
        return ("firing", "#e67e22") if sig.get("fresh") else ("quiet", "#888")
    if any(s.get("state") == "DEAD_WAIT" for s in sig.get("sessions", [])):
        return "DEAD-WAIT", "#e67e22"
    return ("fresh", "#2ecc71") if sig.get("fresh") else ("stale", "#888")


def _session_extra_row(s: dict[str, Any]) -> str:
    """Optional muted sub-row: session name / liveness / recent subagents.

    Self-contained; renders "" when there is nothing extra to show.
    """
    bits: list[str] = []
    if s.get("name"):
        bits.append(f"session name: {s['name']}")
    if s.get("alive") is True:
        bits.append("process: live")
    elif s.get("alive") is False:
        bits.append("process: gone")
    subs = s.get("subagents") or []
    if subs:
        parts = []
        for a in subs[:6]:
            mark = "*" if a.get("active") else ""
            parts.append(f"{a.get('description') or a.get('agent_id')}{mark} ({_age(a.get('age_s'))})")
        bits.append("subagents: " + ", ".join(parts))
    if not bits:
        return ""
    return (
        "<tr><td></td><td colspan=4 style='color:#78808f;font-size:12px'>"
        + _e(" - ".join(bits))
        + "</td></tr>"
    )


def _sessions_section(sig: dict[str, Any]) -> str:
    rows = []
    for s in sig["sessions"]:
        color = _STATE_COLOR.get(s.get("state", ""), "#888")
        tasks = ", ".join(t["path"].rsplit("/", 1)[-1] for t in s.get("tasks", [])[:4])
        rows.append(
            "<tr>"
            f"<td class=mono>{_e(s.get('session_id', '')[:16])}</td>"
            f"<td style='color:{color};font-weight:600'>{_e(s.get('state'))}</td>"
            f"<td>{_e(_age(s.get('transcript_age_s')))}</td>"
            f"<td>{_e(s.get('evidence'))}</td>"
            f"<td class=mono>{_e(tasks)}</td>"
            "</tr>"
        )
        rows.append(_session_extra_row(s))
    return (
        f"<h2>{_e(sig.get('label'))}</h2><table>"
        "<tr><th>session</th><th>state</th><th>age</th><th>evidence</th><th>tasks</th></tr>"
        + "".join(rows)
        + "</table>"
    )
