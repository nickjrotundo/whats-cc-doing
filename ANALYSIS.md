# Functional analysis: gaps, limits, roadmap

An honest accounting. Portfolio-grade software earns trust by naming its
own weaknesses before a reviewer does.

## Known limits (v0.1)

1. **Activity is not progress.** The verdict proves something *moved*,
   not that it moved *forward*. An agent committing junk in a loop, or
   rewriting the same file every minute, reads ACTIVE. The v0.2 progress
   tier (below) is the designed answer; v0.1 says so rather than
   pretending a heartbeat is a health check.
2. **The harness adapter is mtime-semantics deep, not intent-deep.** It
   joins transcript mtimes against task-output mtimes plus a best-effort
   open-file liveness probe (`fuser`); it does not parse what the
   session is semantically waiting for. DEAD_WAIT is therefore
   evidence-based INFERENCE, not proof, and it can misread edges:
   - a session whose transcript moved *after* its tasks died reads IDLE
     even if the human would say it "gave up silently";
   - work that buffers all output past `dead_after_s` (default 15m)
     WITHOUT keeping its output file open can still read DEAD_WAIT -
     tune the threshold per project (the open-file probe catches the
     common held-fd buffering case, and needs `fuser` on PATH);
   - sessions inactive past `stuck_max_age_minutes` (default 120) are
     classified ABANDONED and never STUCK, so a laptop closed mid-wait
     cannot latch the banner or get nudged - at the cost that a
     genuinely stuck session older than the cutoff needs a human.
3. **Transcript format is internal.** Claude Code can change its on-disk
   layout at any release. The adapter degrades to plain mtime semantics
   on anything unparseable (pinned by tests), but a layout change could
   silently reduce fidelity until updated.
4. **Nudge delivery is best-effort at the edges.** Cross-session
   messaging is now officially documented
   (https://code.claude.com/docs/en/cross-session-messaging):
   ListAgents/SendMessage, sessions addressed by name, and - the basis
   of the idle probe - `notify_when_idle: true` with no message as a
   pure subscription that costs the watched session nothing and fires
   one notice when it next goes idle (immediately, if already idle).
   What remains undocumented is the specific combination this tool
   uses: a headless `-p` courier doing the subscribing (the docs
   describe main conversations). So both couriers stay best-effort by
   design: dry-run prints the exact argv, an inconclusive probe fails
   open to the nudge (whose message is ignorable by contract), delivery
   failure never consumes the daily cap silently, and the fallback is
   an enriched notify to the human. Session liveness is registry-based
   (~/.claude/sessions pid + procStart vs /proc, pid-reuse-safe) with a
   project-level /proc cwd scan as fallback - liveness of the
   *process*, not attentiveness of the session; the idle probe is what
   distinguishes "open but finished" from "mid-turn".
5. **Process signals are pattern-based and machine-wide.** `pgrep -af`
   sees every process on the box, so an unscoped pattern counts other
   checkouts' (or other agents') work as this project's activity -
   false ACTIVE that suppresses the watchdog. Generated patterns are
   path-anchored to the project root, which trades toward the safe
   failure mode (a bare `pytest` launched with no project path in its
   command line is missed -> false QUIET, which the ladder surfaces).
   Rendered process lines are redacted by default
   (`PID basename (+N args)`) because status pages get published.
6. **Single-host view.** Signals read the local box; apprise gives
   remote *alerting*, not remote *collection* (a multi-host aggregator
   is out of scope for v0.1). The page's stale-banner self-check runs in
   the viewer's browser, so a page nobody has open is a page nobody is
   warned about - the notify tiers and systemd/cron recovery carry that
   case.
7. **1-minute floor under cron.** Without systemd, tick granularity is
   cron's minimum; verdict windows are minutes-scale anyway.
8. **Day-boundary arithmetic is UTC.** `max_per_day` resets at UTC
   midnight (a local evening can therefore see up to 2x the cap across
   the boundary - the wall-clock cooldown still limits pacing), and
   `jsonl_log` "today" counts are UTC-dated. Documented rather than
   localized, deliberately: the watchdog may run under systemd/cron with
   a different TZ than the user's shell.
9. **No todo/progress display, deliberately.** Claude Code persists no
   todo store on disk (verified empirically 2026-08-30); todo state
   lives only inside conversation transcripts, which the no-content rule
   forbids as a source. Sessions show names, liveness, and subagent
   labels instead - all from dedicated metadata stores.
10. **`ccdoing.yaml` is config-as-code.** `command` signals execute
   shell strings as the invoking user, and signal stdout flows into the
   rendered page and (fenced as untrusted data) into the remediation
   evidence bundle. Read any config before running against it.
11. **The all-projects dashboard trusts each project's snapshot.** A
   card's verdict word comes from that project's own status.json; when
   the generator behind it has stopped, the card keeps the word but
   flags it (`[stale]` in the TUI, "generator stale" on the web) and
   the last-signal age keeps growing in real time (reconstructed from
   the snapshot's own epoch arithmetic, not its frozen strings). Other
   dashboard edges: two registered projects sharing a directory name
   are ambiguous as a URL slug and deliberately refuse to resolve;
   TUI split panes show the most recently active projects (not an
   arbitrary selection), need a >=110-column terminal, and drop ANSI
   color on lines they must clip to keep columns aligned; the web
   day-range filter is client-side JS and degrades to an unfiltered
   card list without it.

## Drift detection limits

- **Inventory diffing is heuristic and type-level.** `doctor --drift`
  compares detected signal *types* against configured ones, so it stays
  quiet once any signal of a type exists - it will not notice that a
  second, different process pattern became relevant. Deliberate: the
  conservative diff never nags, at the cost of missing finer drift.
- **Stale thresholds are guesses.** `stale_after_days: 7` is a default,
  not knowledge; a signal that legitimately matches rarely (a quarterly
  report glob) will read stale unless the threshold is raised.
- **no-match is per-tick truth, not diagnosis.** A target can match
  nothing because it moved, because the work has not run yet, or because
  the glob was always wrong; the page can only say "nothing matched" -
  /ccdoing:tune (or a human) supplies the judgment.

## Policy risk (recorded before submission, not after rejection)

Anthropic's Software Directory Policy 1.F prohibits directory-listed
software from extracting Claude's chat history. The harness adapter
reads session transcript JSONL (timestamps/types only, never content -
enforced in code and tests). Distribution via a self-hosted marketplace
is not directory-reviewed; IF this plugin is ever submitted to the
official directory, the `TranscriptSource` seam exists so the adapter
can be re-backed by hook-delivered events instead of file reads.

## v0.2 roadmap (ranked)

1. **Progress tier**: distinct-commit detection, moving test-pass
   counts, output-file content hashing - the "busy but stuck" defense.
2. **History ring buffer (SQLite)** + per-signal sparkline strips and
   "QUIET for 22m (3rd episode today)" phrasing.
3. **SVG status badge** endpoint for READMEs.
4. **Prometheus textfile export** (one gauge per signal age + verdict).
5. **Log-tail capture beside each signal** (last relevant lines inline).
6. **Hook-event-backed TranscriptSource** (also retires the 1.F risk).
7. **Channels as the two-way path**
   (https://code.claude.com/docs/en/channels): the intended long-term
   mechanism for an external service to push events into sessions and
   receive replies - but research-preview, allowlist-gated,
   Bun-dependent, and opt-in per session via `--channels`, so a
   ccdoing channel plugin is explicitly deferred. (The per-session
   inbox socket - CLAUDE_CODE_MESSAGING_SOCKET under /tmp/cc-socks-<uid>
   - is documented but underspecified; we deliberately built on the
   courier, not the raw socket.)
   Also: a project that is never opened in Claude Code carries a
   standing `claude sessions: no-match` drift finding once the signal is
   configured - either omit `claude_session` there or accept the nag.
8. Multi-project index page - shipped as the all-projects dashboard
   (`ccdoing view --dash`, `ccdoing serve --all`).

## Test coverage

201 tests: signal collectors against real temp git repos / live local
HTTP / fabricated file trees; verdict precedence table; HTML escaping;
watchdog episode lifecycle, tier consumption, cooldown/daily-cap/lock
rails, probe-and-nudge courier argv construction; harness classification
pinned by invented fixture trees (including the dead-wait,
moved-on-after-death, abandoned, held-open-file, and threshold-boundary
edges); CLI init/tick/run/status/doctor/test-escalation round-trips;
plus the regression suite from the v0.1 review round
(tests/test_review_fixes.py - run-loop smoke, corrupted-state
resilience, slug munging, concurrency lock, day-boundary arithmetic,
redaction, hostile session ids, evidence fencing). Not covered: real
systemd installation (deliberate - the suite must not touch the host),
real apprise delivery, real `claude` launches (runner is injected),
real `fuser` probing (monkeypatched; degrades to absent).
