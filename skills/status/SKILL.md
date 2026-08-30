---
name: status
description: Check what's happening in this project right now - read the ccdoing status snapshot, explain the verdict and any stalls, and say what (if anything) needs attention.
---

# ccdoing status check

1. Run `ccdoing status` (add `--fresh` if the last snapshot is older
   than a couple of minutes - check `generated_at`). If it errors with
   "config not found", tell the user to run the setup skill first.
2. Read the JSON. Report, in plain language:
   - the verdict (ACTIVE / QUIET / DOWN / STUCK) and its cause line
   - for QUIET: how long, and which primaries went quiet when
   - for STUCK: which session is dead-waiting and on what evidence
   - for DOWN: which health check is failing
   - anything weight=alert that is firing
3. If STUCK and the user asks you to act: the stuck session id is in
   `stuck_session_ids`. Prefer delivering the evidence to that session
   as an informational message (what the watchdog's nudge tier
   automates - it never resumes or restarts anything; the parked
   session decides) over doing the stalled work yourself blind.
4. Never scrape status.html; status.json is the interface.
