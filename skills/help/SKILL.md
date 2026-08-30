---
name: help
description: Quick ccdoing orientation - current status of the monitor, watchdog, and web server, plus the common commands with one-line descriptions.
---

# ccdoing help

First, gather current state (fast, read-only; skip gracefully past any
command that errors):

1. `ccdoing --version` - installed version (if it disagrees with
   `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`, note that the CLI
   is a stale build and the setup skill's Phase 0 shows the reinstall).
2. `ccdoing doctor` - is the watchdog loop armed (pid) or only ticking?
3. `ccdoing serve status` - is the web server running, and at what URL?
4. `ccdoing status` if a config exists here - current verdict + cause
   (one line; "no config" means setup hasn't run in this project).

Report those four in a compact "current state" block, then this command
table (keep the one-liners; add nothing speculative):

| Command | What it does |
|---|---|
| /ccdoing:setup | full guided setup for this project (signals, watchdog, alerts) |
| ccdoing tick | one collect-render-escalate cycle (what cron/systemd runs) |
| ccdoing run | the same loop in the foreground |
| ccdoing status | print the current verdict JSON |
| ccdoing view | live terminal status view; `--dash` = all-projects dashboard |
| ccdoing serve --all --daemon | background web server for the dashboard (prints the link) |
| ccdoing serve stop / status | stop the web server / show whether it runs |
| ccdoing doctor | environment checks; `--drift` = config-drift report |
| ccdoing test-escalation --tier X | dry-run the log / notify / nudge ladder |
| ccdoing projects | list registered projects (`--unregister` removes one) |
| /ccdoing:serve | start/stop the web server and get the link |
| /ccdoing:status | plain-language read of the current verdict |
| /ccdoing:tune | propose config deltas after the project changed |
| /ccdoing:live | watch verdict changes for the rest of this session |
