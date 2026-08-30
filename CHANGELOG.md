# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-08-30

Second live-install round: fixed the TUI arrow keys (escape sequences
were split by buffered stdin reads - raw-fd reads + a pure decoder now;
j/k work too), renamed the side-by-side web feature to Multi-view
(`/multi`), added daemonized serving (`ccdoing serve --all --daemon`,
`serve stop`, `serve status`, pidfile-tracked with restart semantics and
prominent URLs), a `python -m whats_cc_doing` entry point, a setup-skill
phase that offers to start the web server, new `/ccdoing:serve` and
`/ccdoing:help` skills, and a skill-level CLI-vs-plugin version check
(a stale installed build bit the second live test).

Publish-readiness pass from an independent code review: `serve --daemon`
forwards the resolved `--config` to its respawned child (it previously
resolved config from the child's cwd); escalation-tier retry semantics
are a structured `retryable` flag on ActionResult instead of matching
detail-string wording (a transient nudge launch failure no longer
consumes the tier for the whole quiet episode); quiet duration persists
correctly across ticks; drift bookkeeping keys can no longer collide for
duplicate type:label signal pairs; concurrent ticks write unique temp
files; cron/systemd install output quotes spaced paths; sdist excludes
keep dev files, lockfiles, and the author's local config out of the
published tarball; README links work on PyPI; the setup skill installs
the CLI from the plugin root first (works before and after PyPI
publication).

## [0.1.0] - 2026-08-29

E2E dry-run polish (pre-release): manifests corrected to the nudge-only
design (no more "resume-first" phrasing anywhere), local-checkout install
path documented, `init` suggests a masquerade-proof `min_items` for
json_headline, unambiguous notify dry-run wording, doctor tick labels,
and relative-path/pgrep-self-match cautions in the skills and example
config.

First release. Extracted and generalized from a larger private
project's internal status page, then hardened by a five-agent review
round (adversarial code review, real-project field test, release audit)
before ever being published - see BUILD-LOG.md for that story.

### Added

- Persistent notify targets (2026-08-30, from the first live install
  test): notification URLs now live in `.ccdoing/notify.urls` (one
  apprise URL per line, `#` comments), read on every tick so
  cron/systemd watchdogs need no environment plumbing;
  `$CCDOING_NOTIFY_URLS` overrides the file when set. `ccdoing init`
  scaffolds the file and suggests `.gitignore` coverage;
  `test-escalation --tier notify` prints the resolved source, target
  URLs, and ntfy subscribe links. The setup skill now shows the
  topic/subscribe link BEFORE any test notification and prints the full
  nudge message before asking for approval, offers to start
  not-yet-running services before the verification tick, and offers
  "browser notifications" as a transport.
- Browser notifications (2026-08-30): opt-in, pure viewer-side toggle on
  the status page and the dashboard - fires on verdict transitions
  (into QUIET/DOWN/STUCK and on recovery to ACTIVE) while a page is
  open; per-project last-seen state in localStorage, feature-detected
  (hidden on `file://`).
- All-projects dashboard (2026-08-30): `ccdoing view` outside a project
  (or `--dash`) lists every registered project with recent activity -
  title, colored verdict, "Last signal: X ago", `[stale]` when the
  generator stopped - with arrow/Enter navigation into any project's
  live view and back, a live day-range control (`d`, default 4 days),
  and split panes on wide terminals; `ccdoing serve --all` is the web
  twin - cards linking to each project's real page at `/p/<name>/`, a
  client-side "active within N days" filter, and `/multi` (Multi-view) for 2-4
  status pages side by side. Registered names only; per-project output
  dirs only; no-store everywhere.
- Ten passive signal types (git, process, file_mtime, http, log_tail,
  jsonl_log, claude_session, json_headline, ci, command) with a
  never-raise contract. `json_headline` (added 2026-08-30, straight from
  a staleness hit in the original internal version) reads headline metrics from
  result JSONs by glob pattern - newest full run wins, `min_items` keeps
  partial saves from masquerading as the battery.
- Config-drift detection: per-signal `state` (ok / no-match / stale) in
  status.json and on the page, a `maintenance` summary, `ccdoing doctor
  --drift` (inventory re-diff + dead-signal report; `--quiet` one-liner
  wired into the session-start hook), and the `/ccdoing:tune` skill that
  turns findings into approved config deltas.
- The harness adapter: semantic classification of Claude Code sessions
  (WORKING / WAITING_ON / DEAD_WAIT / ABANDONED / IDLE) from on-disk
  artifacts - timestamps, types, and ids only, never transcript content.
- ACTIVE / QUIET / DOWN / STUCK verdict with cause attribution;
  status.json as the machine interface beside the auto-refreshing
  status.html.
- Watchdog escalation ladder (log -> notify via apprise -> nudge), with
  rails enforced in code: cooldown, daily cap, single-flight lock,
  fenced-untrusted evidence bundles, hostile-session-id rejection.
  Tier 3 is a nudge, never a resume: one informational cross-session
  message (user-approved template) into a session that is provably
  parked on dead work AND provably still running - the session decides;
  finished sessions sitting open overnight are healthy and left alone.
  Before any nudge, an idle probe (a pure notify_when_idle
  subscription, zero cost to the watched session) checks whether the
  session is simply finished - idle means healthy, stand down, budget
  untouched.
  (Redesigned 2026-08-30 from the original resume-first approach after
  Nick's feedback; the healthchecks.io relay was dropped the same day -
  the page now self-detects a stopped generator with a stale banner, and
  systemd Restart=/cron re-entry recover the loop itself.)
- Page UI (2026-08-30): configurable `title:` (setup picks it; plain
  "What's CC Doing" fallback), local human-readable times by default
  (`timezone:` option), ages like "19h 8m", an at-a-glance activity
  table of primary signals (ACTIVE green / inactive grey, drift-marked)
  with the full weight-sorted table behind an All-signals expander.
- Session discovery by recorded working directory (2026-08-30): sessions
  launched in a parent directory that work on this project are found;
  session names (/rename), pid-reuse-safe process liveness, and active
  subagent labels shown from dedicated metadata stores. No todo/progress
  bars - Claude Code persists no todo store to disk, so none is faked.
- Terminal + serving story (2026-08-30): `ccdoing view` (ANSI live
  viewer for ssh/headless, --fresh/--once), `ccdoing serve` (localhost
  static server, Cache-Control: no-store on every response), and a
  multi-project registry (`ccdoing projects`, `view --project NAME`).
- CLI: init (with --write-nudge-message), tick, run, status, view,
  serve, projects, doctor (--arm-check), test-escalation, install
  (systemd/cron with absolute paths).
- Claude Code plugin: setup / status / live skills, SessionStart
  arm-check hook, self-hosting marketplace manifest.
- Privacy defaults: process command lines redacted on rendered pages;
  path-scoped process patterns generated by init.
- CI: 3.11-3.13 test matrix. (The self-monitoring GitHub Pages
  workflow was removed 2026-08-30 - a live-published status page makes
  no sense for installers; a committed example + screenshot replace it.)
