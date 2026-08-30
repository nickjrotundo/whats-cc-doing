---
name: serve
description: Start, restart, or stop the ccdoing status web server and hand the user the link - the all-projects dashboard by default.
---

# ccdoing serve

Version sanity first (one line): if `ccdoing --version` disagrees with
`${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`, reinstall the CLI
from the plugin's source (`uv tool install --force <source>`) before
anything else - the setup skill's Phase 0 has the details.

- Default (no argument, or "start" / "restart"): run
  `ccdoing serve --all --daemon`. This stops any previous server and
  starts a fresh one (restart semantics are built in), serving the
  all-projects dashboard with every registered project's card and live
  status pages. Then give the user the URL from its output,
  prominently - the clickable link is the whole point. WSL2: localhost
  forwarding to the Windows browser usually works but not always - if
  not, suggest a Linux browser (e.g. WSLg-launched Chrome); truly remote
  boxes need ssh port-forwarding (`ssh -L 8377:localhost:8377 <host>`)
  or `--bind 0.0.0.0` (an explicit exposure choice - mention, don't
  choose it for them).
- "stop": run `ccdoing serve stop` and report what was stopped.
- Either way, finish with `ccdoing serve status` output so the user
  sees the current state (running + URL, or not running).

If the user asks for just this project's page instead of the dashboard,
use `ccdoing serve --daemon` (no `--all`) from the project directory -
same controls apply.
