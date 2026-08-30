"""whats-cc-doing: passive status page + watchdog for Claude Code sessions.

The core inverts the usual monitoring contract: nothing self-reports.
Every reading is derived from observed side effects (git log, the process
table, file mtimes, Claude Code's own on-disk session artifacts), so a
wedged process that would still happily send heartbeats cannot look
healthy here.
"""

__version__ = "0.2.0"
