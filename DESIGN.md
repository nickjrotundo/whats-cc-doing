# Design

## Positioning

Existing liveness tooling requires the monitored thing to participate:
healthchecks.io needs a ping, Uptime Kuma needs an endpoint or push
heartbeat, and the agent-observability wave (Langfuse, AgentOps,
OTel-GenAI) needs SDK instrumentation. All of them share a blind spot: a
wedged agent that still pings, or still emits traces from a retry loop,
looks healthy. And none of them can watch an agent you cannot
instrument.

`ccdoing` inverts the contract. The verdict is derived purely from
observed side effects - git log recency, process presence, file mtimes,
the session artifacts Claude Code already writes to disk. Zero
instrumentation, zero cooperation, and therefore zero ability for the
monitored thing to misrepresent itself. The closest prior art
(hook-driven Claude Code monitors) depends on hook wiring inside the
session being watched; the community practice this packages -
`watch git log` and a human eyeball - is exactly the loop that failed by
being manual (see BUILD-LOG.md's origin story).

What it is NOT: a tracing/eval platform (use Langfuse for that), an
uptime service (use healthchecks/Kuma for public endpoints), or a
guarantee of progress (see Limits in ANALYSIS.md).

## Architecture

```
ccdoing.yaml ──> config.py
                    │
      ┌─────────────┼──────────────────────────────┐
      v             v                              v
signals.py     harness.py                    watchdog.py
9 collectors   TranscriptSource (seam)       escalation engine
never raise    WORKING/WAITING_ON/           state.json, rails,
      │        DEAD_WAIT/IDLE                nudge-only escalation
      v             │                              ^
verdict.py <────────┘                              │
ACTIVE/QUIET/DOWN/STUCK + cause                    │
      v                                            │
status_json.py ──> status.json  ───────────────────┘ (evidence bundle)
      └──────────> render.py ──> status.html
```

- **Collect never raises.** A monitoring page that crashes on a missing
  binary is worse than no page; unreadable is itself a reading.
- **One snapshot, two renderings.** status.json is built first and is
  the only interface; the HTML is a projection of it. Nothing scrapes
  HTML.
- **State enables cron.** All episode state (quiet_since, fired tiers,
  remediation counters) lives in `.ccdoing/state.json`, so a one-shot
  `ccdoing tick` from cron is exactly as capable as the resident loop.

## The harness adapter in depth

The `claude_session` signal's mechanics, beyond the state list in the
README:

- **`DEAD_WAIT` is evidence-based inference** from mtimes and the
  process table - stronger than a bare staleness guess (the session's
  last activity predates its tasks' last output, that output stopped
  moving past threshold, and nothing still holds the file open), but
  inference, not proof. That case - "waiting on a notification that can
  never arrive" - is what heartbeat monitors structurally cannot see.
- **Sessions are matched by where they work, not just where they
  started**: a session launched in a parent directory that operates on
  this project is found via the working directory its transcript
  records.
- When available, the page shows each session's **name** (set with
  `/rename`), whether its **process is still alive** (pid-reuse-safe,
  via Claude Code's own session registry plus process start time), and
  the labels of its active **subagents** - all read from dedicated
  metadata files, never from conversation text.
- One deliberate absence: Claude Code does not persist todo lists to
  disk, so there are no per-task progress bars - activity, subagents,
  and background tasks are what's honestly observable.

## Decision log

| Decision | Why |
|---|---|
| Verdict precedence DOWN > STUCK > ACTIVE > QUIET | loudest real problem wins; a failing health check outranks a stalled session outranks silence |
| DEAD_WAIT is evidence-based inference (transcript-vs-task mtime join, plus an open-file liveness probe and an abandonment cutoff), not a bare staleness guess | the motivating failure left exactly this evidence trail; strong evidence earns the STUCK banner and the right to nudge a session, and the review round added the escape hatches (WAITING_ON when the output file is still held open; ABANDONED past stuck_max_age) that keep the inference honest |
| Nudge, never resume | only the session itself knows whether it still has work. Tier 3 probes first with a zero-cost `notify_when_idle` subscription, then delivers at most one informational message into a provably parked, provably alive session - no `--resume`, no fresh work sessions |
| Rails in code, not prompt | cooldown, daily cap, and refuse-while-running are enforced by the engine; a prompt cannot be trusted to rate-limit itself |
| Nudge message is a user-approved file | the watchdog never invents instructions; what was approved is exactly what runs |
| Tier consumption: once per quiet episode, except retryable skips | prevents notify-spam every tick, while a cooldown-blocked remediation stays armed and can fire when the cooldown lapses |
| OS-level loop primary; plugin hook only re-checks arming | session-lifetime watchdogs demonstrably lapse at handoff (origin story); the hook makes the lapse visible instead of silent |
| Stale-page self-check, no external dead-man relay | "who watches the watchdog" deserves an answer with zero third-party services: the page flags itself red when the generator misses its own refresh, and systemd/cron restart the loop |
| Transcript access behind `TranscriptSource` | (a) format is internal and undocumented - degrade to mtime semantics, never crash; (b) Anthropic's Software Directory Policy 1.F makes direct transcript reads a submission risk, so the data source must be swappable for a hook-event-backed one without touching the classifier |
| Only timestamps/types/ids leave the harness module | transcript CONTENT is never surfaced in pages, evidence, or logs |
| apprise for notify | one dependency covers Slack/ntfy/Telegram/desktop and ~100 more |
| Secrets by env-var NAME in config | ccdoing.yaml stays committable |
| Python >=3.11 | 3.10 EOLs Oct 2026; 3.11 is Debian 12's system Python |

## Naming

PyPI/repo `whats-cc-doing`, import `whats_cc_doing`, CLI + plugin slug
`ccdoing` - long where read and searched, short where typed (the plugin
slug prefixes every skill: `/ccdoing:setup`). "CC" refers to Claude Code
compatibility; this is a third-party tool, not an Anthropic product.
