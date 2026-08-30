"""Multi-project dashboard: aggregate every registered project's status.

One machine usually monitors several projects (the registry knows them
all); this module turns that into a single at-a-glance surface - a card
per project with its title, verdict, and how long ago its youngest
primary signal last moved - consumed by both the TUI dashboard
(`ccdoing view` outside a project / `--dash`) and the web dashboard
(`ccdoing serve --all`).

Honesty rules:
- "Last signal" is reconstructed from the snapshot's own numbers
  (generated_epoch minus the youngest primary age at generation time),
  so a stopped generator does not freeze the age at whatever it last
  wrote - the age keeps growing in real time.
- A snapshot older than the page's own staleness threshold is flagged
  `generator_stale`; the verdict word is still shown, but marked, since
  a verdict from a dead generator is a claim, not an observation.
"""

from __future__ import annotations

import html
import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import registry
from .config import CONFIG_FILENAME, ConfigError, load_config
from .util import age_str

DEFAULT_DAYS = 4.0
_STALE_FACTOR = 2.5  # same rule as the page's own self-check
_VERDICT_COLOR = {
    "ACTIVE": "#1f8a4c",
    "QUIET": "#888888",
    "DOWN": "#e74c3c",
    "STUCK": "#c97a1a",
}


@dataclass
class ProjectCard:
    root: Path
    name: str                 # directory name: registry identity + URL slug
    title: str                # status-page title (falls back to name)
    verdict: str              # ACTIVE/QUIET/DOWN/STUCK, or "no data"/"unreadable"
    cause: str = ""
    generated_epoch: float | None = None
    last_signal_epoch: float | None = None
    generator_stale: bool = False
    refresh_seconds: int = 30
    output_dir: Path | None = None

    @property
    def has_data(self) -> bool:
        return self.verdict not in ("no data", "unreadable")

    def last_signal_age(self, now: float) -> float | None:
        e = self.last_signal_epoch or self.generated_epoch
        return None if e is None else max(0.0, now - e)


def _card_for(root: Path, now: float) -> ProjectCard:
    name = root.name
    try:
        cfg = load_config(root / CONFIG_FILENAME)
    except ConfigError:
        return ProjectCard(root, name, name, "no data")
    base = ProjectCard(
        root, name, name, "no data",
        refresh_seconds=cfg.refresh_seconds, output_dir=cfg.output_dir,
    )
    snap_p = cfg.output_dir / "status.json"
    if not snap_p.is_file():
        return base
    try:
        snap = json.loads(snap_p.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        base.verdict = "unreadable"
        return base
    base.title = str(snap.get("title") or name)
    base.verdict = str(snap.get("verdict") or "no data")
    base.cause = str(snap.get("cause") or "")
    gen = snap.get("generated_epoch")
    base.generated_epoch = float(gen) if isinstance(gen, (int, float)) else None
    ages = [
        s.get("age_seconds")
        for s in snap.get("signals", [])
        if s.get("weight") == "primary" and isinstance(s.get("age_seconds"), (int, float))
    ]
    if base.generated_epoch is not None:
        base.last_signal_epoch = (
            base.generated_epoch - min(ages) if ages else base.generated_epoch
        )
        threshold = max(_STALE_FACTOR * base.refresh_seconds, 90.0)
        base.generator_stale = (now - base.generated_epoch) > threshold
    return base


def load_cards(now: float | None = None) -> list[ProjectCard]:
    """All registered projects as cards, most recent signal first."""
    now = time.time() if now is None else now
    cards = [_card_for(r, now) for r in registry.load()]
    cards.sort(
        key=lambda c: (-(c.last_signal_epoch or c.generated_epoch or 0.0), c.name)
    )
    return cards


def filter_recent(
    cards: list[ProjectCard], days: float = DEFAULT_DAYS, now: float | None = None
) -> list[ProjectCard]:
    """Cards with any signal (or at least a snapshot) within the window."""
    now = time.time() if now is None else now
    cutoff = days * 86400.0
    out = []
    for c in cards:
        age = c.last_signal_age(now)
        if age is not None and age <= cutoff:
            out.append(c)
    return out


def resolve_name(name: str) -> ProjectCard | None:
    """URL slug -> card; None when unknown OR ambiguous (two registered
    projects can share a directory name under different parents)."""
    matches = [c for c in load_cards() if c.name == name]
    return matches[0] if len(matches) == 1 else None


def verdict_color(verdict: str) -> str:
    return _VERDICT_COLOR.get(verdict, "#666666")


# --------------------------------------------------------------------------
# Web rendering (pure functions; serve.py is transport only)

_PAGE_CSS = """
body { background:#12141a; color:#d7dae0; font: 14px/1.5 system-ui, sans-serif;
       margin: 0 auto; max-width: 1100px; padding: 16px; }
a { color:#7aa2f7; text-decoration: none; }
h1 { font-size: 20px; }
.meta { color:#78808f; font-size: 12px; }
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
         gap:12px; margin-top:16px; }
.card { background:#191c24; border:1px solid #262a33; border-radius:10px;
        padding:14px 16px; }
.card h2 { margin:0 0 6px; font-size:16px; }
.card h2 a { color:#d7dae0; }
.verdict { display:inline-block; padding:1px 10px; border-radius:10px;
           color:#0b0e14; font-weight:600; font-size:12px; }
.lastsig { color:#9aa3b2; font-size:13px; margin:6px 0 0; }
.stale { color:#c97a1a; font-size:12px; }
.cause { color:#78808f; font-size:12px; margin:4px 0 0; overflow:hidden;
         text-overflow:ellipsis; white-space:nowrap; }
.filter { margin-top:10px; font-size:13px; color:#9aa3b2; }
.filter input { width:3.5em; background:#191c24; color:#d7dae0;
                border:1px solid #262a33; border-radius:4px; padding:2px 6px; }
.cmp { margin-left:16px; }
.cmp button { background:#191c24; color:#7aa2f7; border:1px solid #262a33;
              border-radius:4px; padding:2px 10px; cursor:pointer; }
.topbar { padding:8px 16px; background:#191c24; border-bottom:1px solid #262a33;
          font-size:14px; }
.hidden { display:none; }
"""


def render_dashboard_html(
    cards: list[ProjectCard],
    now: float | None = None,
    days: float = DEFAULT_DAYS,
) -> str:
    now = time.time() if now is None else now
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    items = []
    for c in cards:
        age = c.last_signal_age(now)
        age_days = (age / 86400.0) if age is not None else 9e9
        last = f"Last signal: {age_str(age)} ago" if age is not None else "no data yet"
        stale = (
            f"<div class=stale>generator stale ({age_str(now - c.generated_epoch)}"
            " since last update)</div>"
            if c.generator_stale and c.generated_epoch else ""
        )
        cause = f"<div class=cause>{e(c.cause)}</div>" if c.cause else ""
        items.append(
            f'<div class=card data-age-days="{age_days:.4f}" '
            f'data-pname="{e(c.name)}" data-ptitle="{e(c.title)}" '
            f'data-verdict="{e(c.verdict)}" data-cause="{e(c.cause or "")}">'
            f'<h2><a href="/p/{e(c.name)}/">{e(c.title)}</a></h2>'
            f'<span class=verdict style="background:{verdict_color(c.verdict)}">'
            f"{e(c.verdict)}</span>"
            f'<label style="float:right;font-size:12px;color:#78808f">'
            f'<input type=checkbox class=mvbox value="{e(c.name)}"> multi-view</label>'
            f"<div class=lastsig>{last}</div>{stale}{cause}</div>"
        )
    body = "\n".join(items) or "<p class=meta>no projects registered - run `ccdoing init` in a project</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>What's CC Doing - all projects</title>
<style>{_PAGE_CSS}</style></head><body>
<h1>What's CC Doing <span class=meta>- all projects</span></h1>
<div class=filter>active within <input id=days type=number min=0 step=0.5
 value="{days:g}"> days
 <span id=count class=meta></span>
 <span class=cmp><button id=mvbtn title="open 2-4 checked projects side by side">Multi-view checked</button>
 <button id=bn-toggle hidden title="Fires a browser notification when any project's verdict changes while this page is open. Needs the Notification API - works over ccdoing serve (localhost); file:// usually lacks it."></button></span></div>
<div class=cards id=cards>
{body}
</div>
<script>
(function () {{
  try {{
    var input = document.getElementById('days');
    function apply() {{
      var d = parseFloat(input.value); if (isNaN(d)) d = {DEFAULT_DAYS};
      var shown = 0;
      document.querySelectorAll('.card').forEach(function (c) {{
        var age = parseFloat(c.getAttribute('data-age-days'));
        var hide = age > d;
        c.classList.toggle('hidden', hide);
        if (!hide) shown += 1;
      }});
      document.getElementById('count').textContent = '(' + shown + ' shown)';
    }}
    input.addEventListener('input', apply);
    apply();
    document.getElementById('mvbtn').addEventListener('click', function () {{
      var names = Array.prototype.slice.call(
        document.querySelectorAll('.mvbox:checked')).map(function (b) {{ return b.value; }});
      if (names.length < 2 || names.length > 4) {{
        alert('check 2 to 4 projects for multi-view'); return;
      }}
      location.href = '/multi?' + names.map(function (n) {{
        return 'p=' + encodeURIComponent(n); }}).join('&');
    }});
  }} catch (err) {{ /* degrade to an unfiltered list */ }}
}})();
</script>
<script>
(function () {{
  /* Opt-in browser notifications, dashboard-wide: one toggle, per-project
     last-seen verdicts in localStorage (keys shared with the individual
     status pages, so either surface can notice a transition). Hidden
     when the Notification API is absent (file:// usually). */
  try {{
    if (!("Notification" in window)) return;
    var enKey = "ccdoing-bn:__dash__";
    var BAD = ["QUIET", "DOWN", "STUCK"];
    function ls(fn, fallback) {{ try {{ return fn(); }} catch (e) {{ return fallback; }} }}
    function enabled() {{ return ls(function () {{ return localStorage.getItem(enKey) === "1"; }}, false); }}
    var btn = document.getElementById("bn-toggle");
    function paint() {{
      btn.hidden = false;
      btn.textContent = enabled()
        ? "browser notifications: on"
        : "enable browser notifications";
    }}
    btn.addEventListener("click", function () {{
      if (enabled()) {{ ls(function () {{ localStorage.setItem(enKey, "0"); }}); paint(); return; }}
      Notification.requestPermission().then(function (p) {{
        if (p === "granted") ls(function () {{ localStorage.setItem(enKey, "1"); }});
        paint();
      }});
    }});
    paint();
    document.querySelectorAll(".card").forEach(function (c) {{
      var name = c.getAttribute("data-pname");
      if (!name) return;
      var v = c.getAttribute("data-verdict") || "";
      var seenKey = "ccdoing-bn-last:" + name;
      var prev = ls(function () {{ return localStorage.getItem(seenKey); }}, null);
      ls(function () {{ localStorage.setItem(seenKey, v); }});
      if (!enabled() || Notification.permission !== "granted") return;
      if (prev === null || prev === v) return;
      var wasBad = BAD.indexOf(prev) !== -1, isBad = BAD.indexOf(v) !== -1;
      if (isBad || (v === "ACTIVE" && wasBad)) {{
        new Notification(c.getAttribute("data-ptitle") || name,
                         {{ body: v + " - " + (c.getAttribute("data-cause") || "") }});
      }}
    }});
  }} catch (err) {{ /* notifications are a convenience, never a failure */ }}
}})();
</script>
</body></html>"""


def render_wrapper_html(card: ProjectCard) -> str:
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(card.title)}</title>
<style>{_PAGE_CSS}
html,body{{height:100%;max-width:none;padding:0}}
iframe{{border:0;width:100%;height:calc(100% - 41px)}}</style></head><body>
<div class=topbar><a href="/">&#8592; all projects</a>
 &nbsp;&middot;&nbsp; {e(card.title)}</div>
<iframe src="/p/{e(card.name)}/status.html" title="{e(card.title)}"></iframe>
</body></html>"""


def render_multiview_html(cards: list[ProjectCard]) -> str:
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    cols = 1 if len(cards) == 1 else 2
    frames = "\n".join(
        f'<div class=pane><div class=topbar>{e(c.title)}</div>'
        f'<iframe src="/p/{e(c.name)}/status.html" title="{e(c.title)}"></iframe></div>'
        for c in cards
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>What's CC Doing - multi-view</title>
<style>{_PAGE_CSS}
html,body{{height:100%;max-width:none;padding:0}}
.grid{{display:grid;grid-template-columns:repeat({cols},1fr);
      gap:2px;height:calc(100% - 41px)}}
.pane{{display:flex;flex-direction:column;min-height:0}}
.pane iframe{{border:0;flex:1;width:100%}}</style></head><body>
<div class=topbar><a href="/">&#8592; all projects</a>
 &nbsp;&middot;&nbsp; multi-view: {len(cards)} projects</div>
<div class=grid>
{frames}
</div>
</body></html>"""
