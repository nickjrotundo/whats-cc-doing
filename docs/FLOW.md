# whats-cc-doing: flow and triggers

One-page map of what triggers ccdoing, what a tick does, and where the
watchdog escalation can end up. Standalone on purpose - safe to delete.

```mermaid
flowchart TD

    subgraph TRIGGERS["Triggers"]
        TIMER["systemd unit / cron line<br>(every refresh_seconds / 1 min)"]
        MANUAL["manual CLI<br>ccdoing tick / run / status"]
        SS["Claude Code SessionStart hook"]
        SKILLS["skills: /ccdoing:setup<br>/ccdoing:status /ccdoing:tune"]
    end

    subgraph TICK["One tick"]
        LOCK["flock .ccdoing/state.json<br>(concurrent tick skips)"]
        COLLECT["collect signals (never raise):<br>git / process / file_mtime / http /<br>log_tail / jsonl_log / ci / command /<br>json_headline / claude_session"]
        HARNESS["harness adapter:<br>transcripts + task files + process table<br>WORKING / WAITING_ON /<br>DEAD_WAIT / ABANDONED / IDLE"]
        VERDICT["verdict + cause attribution<br>DOWN > STUCK > ACTIVE > QUIET"]
        DRIFT["drift bookkeeping:<br>per-signal ok / no-match / stale"]
    end

    subgraph OUT["Outputs"]
        HTML["status.html<br>(humans; file:// or static mount,<br>Cache-Control: no-store)"]
        JSON["status.json<br>(agents and scripts poll this)"]
        STALE["stale-page self-check<br>(page flags itself red when the<br>generator misses its own refresh)"]
        VIEW["ccdoing view / view --dash (terminal, ssh;<br>all-projects dashboard + per-project view)<br>ccdoing serve --all --daemon / stop / status<br>(localhost, no-store; cards, /p/name/, /multi)<br>ccdoing projects (registry)"]
    end

    subgraph WD["Watchdog escalation (per quiet episode)"]
        T1["tier 1: log<br>.ccdoing/watchdog.log"]
        T2["tier 2: notify via apprise<br>(ntfy / Slack / etc, flap-suppressed)"]
        T3{"tier 3: nudge (opt-in;<br>cooldown, max/day, pid lock)"}
        NUDGE["one informational message into the<br>parked session - evidence bundle +<br>'ignore this if you're fine';<br>the session decides. NEVER resume,<br>NEVER a new session"]
        SKIPN["session process not alive?<br>tier skips; notify says why"]
    end

    subgraph CLAUDE["Claude layer"]
        ARM["arm check: watchdog running?<br>warn / self-arm"]
        DRIFTNOTE["doctor --drift --quiet<br>one-line drift notice into session"]
        TUNE["/ccdoing:tune<br>delta-only config repair, user consent"]
    end

    TIMER --> LOCK
    MANUAL --> LOCK
    LOCK --> COLLECT
    COLLECT --> HARNESS
    HARNESS --> VERDICT
    COLLECT --> VERDICT
    VERDICT --> DRIFT
    DRIFT --> HTML
    DRIFT --> JSON
    HTML --> STALE
    JSON --> VIEW

    VERDICT -->|"QUIET too long"| T1
    T1 -->|"still quiet"| T2
    T2 -->|"still quiet"| T3
    T3 -->|"DEAD_WAIT session + process<br>verifiably alive (registry+procStart)"| NUDGE
    T3 -->|"otherwise"| SKIPN

    SS --> ARM
    SS --> DRIFTNOTE
    DRIFTNOTE -->|"drift found"| TUNE
    JSON -->|"maintenance list"| TUNE
    SKILLS --> MANUAL
    TUNE -->|"config deltas + verification tick"| MANUAL
```

Reading notes:

- Everything on the left half is deterministic and LLM-free; Claude
  appears only in the bottom-right (session notices, tune, and the
  opt-in tier-3 nudge).
- There is no timer hook in Claude Code - the periodic loop lives in
  systemd/cron; SessionStart is the only automatic Claude-side trigger.
- ABANDONED sessions (older than stuck_max_age_minutes) never produce
  STUCK and are never nudged; IDLE is sacred - a finished session
  sitting open overnight is healthy and left alone.
- Nothing here resumes or spawns sessions. Tier 3 delivers at most one
  message; only the session itself knows whether it still has work.
