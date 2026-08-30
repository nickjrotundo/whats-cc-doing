---
name: live
description: Watch this project's ccdoing status live for the rest of the session and react when the verdict changes (goes QUIET, STUCK, or DOWN).
---

# ccdoing live watch

Goal: keep an eye on the status page for the rest of this session and
react on verdict CHANGES.

Preferred mechanism, in order:

1. If this Claude Code build supports background monitors (experimental
   `monitors` plugin feature - interactive CLI sessions only), start one
   running:
   `sh -c 'while true; do ccdoing status | head -c 400; echo; sleep 60; done'`
   and react when the streamed verdict differs from the last one you saw.
2. Otherwise, if a Monitor tool is available in this session, use it
   with an until-condition on `ccdoing status` output changing verdict.
3. Otherwise, degrade gracefully: tell the user live-watching is not
   available in this environment, and that the OS-level watchdog
   (`ccdoing doctor` shows whether it is armed) is already covering
   stalls; offer to check on demand via the status skill instead.

When the verdict changes:
- ACTIVE -> QUIET: note it, no action yet (the watchdog ladder owns
  escalation timing).
- -> STUCK: read status.json, report the dead-wait evidence, and ask the
  user whether to intervene now or let the watchdog's ladder run.
- -> DOWN: report which health check failed immediately.
