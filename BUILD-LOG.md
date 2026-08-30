# Build log

How this repository was actually built - an AI-driven build, documented
honestly. Kept tutorial-style because "show me how you work with AI
tooling" is a question this repo should answer by existing.

## Provenance (the full origin story)

The core idea and the first implementation come from a larger private
project (a personal multi-agent build): a ~195-line
`status_page.py` regenerating an HTML page every 30s under a systemd
user unit, plus a Monitor armed in one Claude Code session as a
watchdog.

It started smaller than watchdogs: a long-running Claude Code subagent
was off doing testing, and the Claude Code TUI gave no visual feedback
about whether it was working or hung. It *was* working - but the only
way to see that was activity signals the TUI doesn't show: fresh task
output files, transcript growth, processes in the table. The first
version of the page existed purely to make background work visible.

The watchdog grew out of what happened next. A watchdog armed in one
Claude Code session caught stalled agents - it worked. Then that
session ended, its handoff message explicitly said "arm your own
watchdog", and nobody did. The replacement was a human manually
checking git state and the process table whenever an agent sent a
content-free "waiting..." notification.

That manual loop caught real stalls, including the one that shaped this
tool's headline feature: an agent waiting forever on a pytest run it
had launched with a bare `&` subshell and a 3-minute timeout - on a
16-minute suite. The notification it was waiting for could never
arrive. The process table and the file mtimes knew; nothing was
watching them.

Two lessons, baked in as design decisions:

1. **Any arming step someone must remember will eventually be
   skipped.** So the watchdog is an OS-level loop (systemd/cron), and
   the Claude Code plugin re-checks arming at session start.
2. **Self-reported liveness is worthless for stuck agents.** A wedged
   process happily keeps pinging. Side effects don't lie - so nothing
   here is instrumented, subscribed, or self-reported.

## Process

1. **Research phase (multi-agent).** A supervising Claude session ran
   four parallel research agents: full source inventory of the original
   tool (generic-vs-project-specific split), Claude Code
   plugin/distribution mechanics, competitive landscape
   (healthchecks/Kuma/Langfuse/agent-observability wave - the
   zero-instrumentation niche was empty), and a setup-flow design draft.
2. **Decision phase (agent consensus + human veto).** Three decision
   agents with different lenses (client credibility, engineering
   pragmatism, OSS adoption) recommended on names, layout, license,
   Python floor, config conventions; positions were cross-pollinated to
   unanimous consensus. The human overrode one thing - naming - choosing
   CC-branded names over the agents' generic picks, which is exactly the
   kind of call that belongs to a human (recorded in DESIGN.md).
3. **Ideal-architecture pass.** Before building, a "what would ideal
   look like" round added the three features that distinguish v0.1 from
   a packaging job: the harness adapter with DEAD_WAIT detection,
   self-arming checks, and resume-first remediation with evidence
   bundles.
4. **Build phase (this repo).** An orchestrator agent built the package
   module-by-module, verifying ground truth first (real transcript
   structure inspected for LAYOUT only; `claude --resume`/`-p` flags;
   `claude plugin validate` availability), then: core modules -> smoke
   test on the repo itself -> 68-test suite (one real semantics bug
   found and fixed: notify tiers re-fired every tick when no URLs were
   configured; consumption semantics now distinguish retryable skips) ->
   plugin + skills -> CI + self-monitoring example -> docs.
5. **Verification.** `ccdoing init && ccdoing tick` against this repo
   produced the committed examples/; the plugin manifest passes
   `claude plugin validate`; the full suite runs green in under a
   second.

## The review round (what 68 green tests missed)

After the build declared itself done - 68 tests green, plugin
validating, live example committed - a five-agent review round
(adversarial code review, a field test against a real unrelated
project, and a release-readiness audit) found, among ~30 findings:

- **The advertised install path crashed on its first cycle.**
  `ccdoing run` - the exact command in the systemd unit, the setup
  skill, and the arm-check hint - died on an argparse attribute no test
  had ever exercised. Every test used `tick`. Lesson pinned into the
  suite: test the entry points users are told to run, not just the
  internals they share.
- The project-slug computation didn't match Claude Code's real munging
  (spaces/dots/underscores), silently blinding the flagship signal for
  such paths; fixed empirically against real `~/.claude/projects`
  entries.
- A week-old abandoned session could latch a permanent STUCK verdict
  outranking live work - and get auto-resumed three times a day
  forever. The ABANDONED state and the open-file liveness probe came
  out of that finding, and the docs stopped calling DEAD_WAIT
  "deterministic".
- The committed example page had captured a raw process-table line
  full of local machine paths - which produced both a history rewrite
  and the redact-by-default process rendering.

The full fix pass (this commit series) closed the blockers, the majors,
and the minors, taking the suite from 68 to 102 tests. Honest
conclusion for the reader: the review agents earned their tokens.

## The day-two field lesson (2026-08-30): drift is real

One day after the review round, Nick found the ORIGINAL internal status page
lying by omission: its "latest eval headlines" read two hardcoded /tmp
JSON paths, and eval runs had long since moved to ad-hoc `--save` names.
His field patch - glob discovery, newest file by mtime, and a
sessions-count filter so a single-scenario save could not masquerade as
the full battery - became this tool's `json_headline` signal type
verbatim. The generalization: ANY configured path can rot as a project
grows (testing added later, outputs moved, runners swapped), so signals
now track whether their targets match anything (no-match / stale
states), `doctor --drift` re-diffs the inventory, and the /ccdoing:tune
skill turns findings into approved config deltas. The status monitor
monitoring its own configuration was not in the original design; the
field forced it within 24 hours.

## The feedback round (2026-08-30): six critiques, four worktrees

After using the built tool against his own instincts, Nick filed a
six-point critique in one message: the origin story was half-wrong (the
page began as visual proof that a silent testing subagent was working,
not as a watchdog); healthchecks.io was an unwanted external dependency
("can't the page notice it missed two updates itself?"); resume-first
remediation risked waking the wrong session or duplicating a healthy
one ("a finished session sitting open overnight is fine - only the
session knows"); the example page could not even see the very session
that built it; a GitHub-Pages-published status page made no sense for
installers, while headless/remote devs could not easily open the HTML at
all; and the page's most-used feature - the at-a-glance ACTIVE table -
had been buried under an engineer's full-detail table.

Every point was accepted. The work fanned out to four parallel agents in
isolated git worktrees with strict file ownership (UI, harness,
remediation, viewing), each forbidden to touch the docs; an integrator
merged the branches, wired the seams the agents had left each other
notes about (the harness's pid-reuse-safe `alive` field feeding the
nudge precondition), and consolidated the docs. Notable empirical
findings along the way: sessions record their working directory
per-event (fixing the invisible-session bug properly, by where sessions
WORK rather than where they started); ~/.claude/sessions is a live
registry with pid + process start time; and Claude Code persists no todo
store to disk - so the requested progress bars were declined as
unimplementable without faking, and the README says so.

## What the human decided vs. what the model decided

Human: the tool should exist; CC-branded naming; CC-only support
posture; watchdog tiers are opt-in with an approved prompt; the nudge
philosophy (never resume - the session decides); no external monitoring
dependencies; the at-a-glance table as the default view. Model
(with consensus machinery): everything else - architecture, precedence
rules, rails semantics, file formats, test strategy - reviewable in
DESIGN.md's decision log.
