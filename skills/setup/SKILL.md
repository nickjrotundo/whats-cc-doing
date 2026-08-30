---
name: setup
description: Set up the What's CC Doing status monitor + watchdog for this project - inventory signals, write ccdoing.yaml, install the watchdog loop, verify the alert path end to end.
---

# ccdoing setup

Set up the passive status monitor + watchdog for the CURRENT project.
Follow the phases in order. Idempotent: if `ccdoing.yaml` already exists,
skip to **Update mode** at the bottom.

## Phase 0 - install check

Run `ccdoing --version`. If missing, install it - PRIMARY path first:

- `uv tool install "${CLAUDE_PLUGIN_ROOT}"` - the plugin root IS the
  full installable source tree, so this works regardless of whether the
  package has been published anywhere yet. Prefer it.
- alternatives when the user wants a published build instead:
  `uv tool install whats-cc-doing` (PyPI) or
  `uv tool install git+https://github.com/nickjrotundo/whats-cc-doing`
- or into a project venv: `uv pip install "${CLAUDE_PLUGIN_ROOT}"`

then re-check. If installation genuinely fails, stop and show the user
the error.

VERSION MATCH: compare the installed `ccdoing --version` against this
plugin's own version (`${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`,
"version" field). On mismatch the CLI predates the plugin's docs and
behavior - reinstall it from the same source the plugin came from
BEFORE proceeding (`uv tool install --force <source>`; for a local
marketplace that is the original checkout path, and
`${CLAUDE_PLUGIN_ROOT}` itself is a usable full copy), tell the user
why ("the installed CLI was an older build than this plugin"), then
re-check. A stale CLI once silently ignored newer config keys in live
testing - do not skip this.

## Phase 1 - inventory

Run `ccdoing init` (writes ccdoing.yaml with auto-detected signals),
then read the file. Broaden the detection yourself - `init` is
conservative:

- long-running processes: look at Procfile, docker-compose services,
  package.json scripts, known runners (celery, uvicorn, node) and add
  `process` signals with pgrep patterns. ALWAYS anchor patterns with the
  project's absolute path (e.g. `/path/to/proj.*(vitest|node)`): pgrep
  matches machine-wide command lines, and an unscoped pattern counts
  other checkouts' and other agents' processes as this project's
  activity - false ACTIVE that silently suppresses the watchdog.
  The inverse pitfall: a process launched via a RELATIVE path
  (`.venv/bin/uvicorn app:app`) never matches an absolute-path-anchored
  pattern - prefer anchoring on stable argv content that appears
  regardless of launch style (a module path, an app name). And when
  testing patterns with pgrep/pkill by hand, remember `-f` matches your
  own command line too - quote carefully or you kill your own shell.
- health endpoints: grep routes for /health, /healthz, /ready. Before
  adding an `http` signal, ask (fold into the Phase 2 interview):
  is this endpoint expected to be ALWAYS up? Only then set
  `verdict.health_failure_is_down: true`; for a sometimes-running dev
  server keep it false, or the page screams DOWN whenever the server is
  off AND the quiet ladder resets.
- logs: find log files or logging config; add `log_tail` (weight: alert)
- LLM usage logs, queue depths, or anything project-specific: use the
  `command` escape hatch - and remember command strings in ccdoing.yaml
  execute as the user (config-as-code; say so if the user adds one).
  Every signal type and field is documented in
  `${CLAUDE_PLUGIN_ROOT}/templates/ccdoing.example.yaml`.
- the `claude_session` signal should be present for any project you are
  working on in Claude Code; add it if init missed it

## Phase 2 - one interview round

Ask the user ONE AskUserQuestion round (max 4 questions):

1. Which detected signals to monitor (multiSelect, pre-select your
   recommendations).
2. Active window: 15m default / 5m fast-moving / 60m slow project.
3. Escalation ceiling: "log only" / "notify me" / "notify then nudge
   the parked session" (tier 3, requires Phase 4 approval). Explain the
   nudge honestly: an INFORMATIONAL cross-session message into a session
   the harness proved is parked on a dead wait - it never resumes a
   session, never spawns a work session, and never targets an idle or
   finished session (a session sitting open overnight after finishing
   its work is healthy and is left alone).
4. If notify: transport - ntfy topic / Slack webhook / other apprise
   URL / "browser notifications (when a status page is open)". Browser
   notifications are a page-side toggle on the status page and
   dashboard: no server transport, fires on verdict changes only while
   a page is open in a browser (needs `ccdoing serve` - file:// usually
   lacks the Notification API). They combine fine with a server-side
   transport; choosing ONLY browser notifications means quiet-hours
   escalation reaches nobody unless a page is open - say so.

Apply the answers to ccdoing.yaml. Server-side notification URLs are
PERSISTED in `.ccdoing/notify.urls` (one apprise URL per line, `#`
comments allowed) - the file is read on every tick, so cron/systemd
watchdogs see it with no environment plumbing. The CCDOING_NOTIFY_URLS
environment variable OVERRIDES the file when set. Never put URLs in
ccdoing.yaml itself. If the project is a git repo, make sure .gitignore
covers the state dir (topics/webhooks are effectively secrets) while
keeping the nudge message committable - add this block if missing:

    .ccdoing/*
    !.ccdoing/nudge-message.md

Also choose the page `title:` yourself (no need to ask): a short human
name for what is being watched, from what the project actually is (e.g.
"Acme build" for the Acme project) or the session's name if the
user has set one via /rename. If nothing meaningful suggests itself,
OMIT the key - the page then shows plain "What's CC Doing". Never
concatenate the repo slug with the tool name.

## Phase 3 - first run + verification

1. If any configured process/http signal references a service that is
   not currently running, OFFER to start it for the verification (use
   the project's own documented start command, run it in the
   background, and tell the user exactly what you started and how to
   stop it). If the user declines or nothing is startable, proceed
   anyway and say explicitly that those signals will read "not
   running" / "unreachable" until the services are up - that is
   expected, not a problem. Never present a dead-service reading as a
   setup failure.
2. `ccdoing tick` - confirm it prints a verdict and writes
   status.html + status.json under the configured `output_dir`
   (default `reports/status/`). Open/read status.json and
   sanity-check each signal's reading; fix config mistakes now.
3. If a server-side notify transport was chosen, in THIS order - never
   send a test notification before the user has seen where to look:
   a. Collect (or generate) the topic/webhook URL and write it to
      `.ccdoing/notify.urls`.
   b. SHOW the user, in the conversation: the topic/URL, the subscribe
      link (for ntfy: https://ntfy.sh/<topic>), and the storage path.
      `ccdoing test-escalation --tier notify` (dry-run) prints all
      three for you.
   c. AskUserQuestion: are they subscribed and ready for a test
      notification?
   d. Only then: `ccdoing test-escalation --tier notify --real`.
   e. Confirm they actually received it; offer to resend if not.
4. If browser notifications were chosen: point out the "enable browser
   notifications" toggle on the status page and dashboard, and that it
   needs a served page (`ccdoing serve`) and only fires while a page is
   open.

## Phase 4 - nudge message (only if tier 3 chosen)

1. Add the nudge tier to ccdoing.yaml (cooldown_minutes: 60,
   max_per_day: 3 unless the user changes them), then run
   `ccdoing init --write-nudge-message` - it renders the PACKAGED
   template (ships inside the wheel; no plugin-repo paths needed) into
   .ccdoing/nudge-message.md with the placeholders filled.
2. PRINT the complete rendered message in the conversation (in a fenced
   code block) and state its path (.ccdoing/nudge-message.md) BEFORE
   asking anything - the user cannot approve text they have not seen.
   Only then ask for approval or edits, referencing the text just
   shown. The watchdog only ever sends this file's current contents
   (plus the evidence bundle). The message must keep its "ignore this
   if you are fine" framing - the receiving session decides, not the
   watchdog.
3. `ccdoing test-escalation --tier nudge` (dry-run) - it prints the
   courier argv that would launch AND the full evidence bundle for
   review. Note the two hard preconditions it enforces: a DEAD_WAIT
   session identified by the harness, and a live claude process with
   cwd under this project (so the dry-run may report a skip - that is
   the rails working, not a failure).

## Phase 5 - install the watchdog loop

Try in order; verify, never assume (especially on WSL2):

1. systemd user unit: run `systemctl --user show-environment`. If it
   works, `ccdoing install` prints the unit with the ABSOLUTE ccdoing
   path already resolved (or render
   `${CLAUDE_PLUGIN_ROOT}/templates/ccdoing.service` yourself); save to
   ~/.config/systemd/user/, `systemctl --user daemon-reload && systemctl
   --user enable --now ccdoing-<name>`, then verify `is-active` says
   active. Mention `loginctl enable-linger` for headless boxes.
2. cron fallback: `ccdoing install --mode cron` prints the line (again
   with the absolute path - cron's PATH won't have venv installs); add
   via crontab. Note: crontab edits may trigger a permission prompt in
   Claude Code sessions; that is expected, let the user approve.
3. manual fallback: tell the user to run `ccdoing run` (tmux suggested).

Confirm arming with `ccdoing doctor`: it distinguishes "loop running
(pid N)" from "recurring ticks (cron?)" from NOT running - a single
manual tick during setup does not count as armed on its own, so prefer
seeing the loop pid or a second tick arrive.

## Phase 6 - CLAUDE.md block

Append to the project's CLAUDE.md:

```markdown
## Status monitor (ccdoing)
A passive status monitor runs for this project (config: ccdoing.yaml).
Human view: status.html and machine view status.json under the
output_dir configured in ccdoing.yaml (default reports/status/). When
resuming a session or before starting
long background work, read status.json to see what is running and what
recently moved. Long-running work should touch signals the monitor
watches (commits, watched globs, watched processes) so the watchdog does
not flag it as stalled. Never edit .ccdoing/state.json.
If a session-start notice or the status page reports CONFIG DRIFT
(signals matching nothing, or new detectable signals), offer to run
/ccdoing:tune - it proposes config deltas without rewriting tuned
values.
```

## Phase 7 - offer the web view

Opening a file path is not a real serving story (headless boxes, WSL2,
remote dev), so finish by offering the built-in server. AskUserQuestion:
"Start the local status web server now?" with options like "Yes -
all-projects dashboard (Recommended)" / "Not now".

On yes:

1. Run `ccdoing serve --all --daemon` (the all-projects dashboard - it
   includes this project's card and every other registered project; a
   single-project server exists but the dashboard is the default offer).
2. Print the URL it reports, prominently - that link is the deliverable.
   On WSL2 note that localhost forwarding to the Windows browser
   usually works but not always - if it does not, open the URL in a
   Linux browser instead (e.g. WSLg-launched Chrome). On a truly remote
   box mention ssh port-forwarding
   (`ssh -L 8377:localhost:8377 <host>`).
3. Mention the controls: `ccdoing serve stop` / `ccdoing serve status`,
   and the /ccdoing:serve skill for later.

On no: one line noting `ccdoing serve --all --daemon` (or
/ccdoing:serve) starts it any time.

## Update mode (config already exists)

Re-run the Phase 1 inventory mentally against the current ccdoing.yaml
and propose ONLY deltas (new signals found, dead paths, threshold
drift). Never silently rewrite values the user tuned. Confirm the
watchdog is still armed (`ccdoing doctor`).
