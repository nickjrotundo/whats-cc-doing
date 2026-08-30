---
name: tune
description: Re-tune the What's CC Doing status monitor after the project changed - review config drift, propose signal deltas, apply approved changes, verify.
---

# ccdoing tune

Keep the status page honest as the project grows and changes. Projects
gain eval suites, move result files, rename log paths, add services;
signals configured at setup time quietly stop matching anything. This
skill turns the tool's deterministic drift findings into approved config
deltas. It NEVER regenerates ccdoing.yaml wholesale.

## Phase 1 - gather the evidence

1. Run `ccdoing doctor --drift` and read the output:
   - "configured signals whose targets are not matching" - each is a
     signal whose glob/path/pattern found nothing (`no-match` this tick,
     `stale` when it has matched nothing for `drift.stale_after_days`,
     default 7).
   - "detectable in this project but not configured" - the inventory
     re-run found signal types the config lacks.
2. Read `status.json` under the configured `output_dir` (default
   `reports/status/`; check ccdoing.yaml): the `maintenance` list and each
   signal's `state` field carry the same findings with detail strings.
3. Investigate each finding in the repo before proposing anything:
   - A no-match glob: did the output move? (`git log --stat`, look for
     the new location of e.g. eval results, build dirs, logs.)
   - A stale process pattern: did the runner change (pytest -> vitest)?
     Did the project path change so the anchored pattern no longer
     matches?
   - An unconfigured candidate: is it real and useful, or noise?
   - Eval/test result JSONs are the classic case: prefer a
     `json_headline` signal with glob `patterns` over any fixed path,
     with `min_items` set high enough that a single-scenario save
     cannot masquerade as the full battery headline (see
     `${CLAUDE_PLUGIN_ROOT}/templates/ccdoing.example.yaml`).

## Phase 2 - propose DELTAS

One AskUserQuestion round, options pre-selected to your recommendations:

1. Which fixes to apply (multiSelect): one option per finding, each a
   concrete delta ("point 'eval results' patterns at eval-results/*.json",
   "remove the dead 'dist' glob", "add json_headline for
   test-results/"). Include a "remove it" option for signals whose
   target is truly gone.
2. Only if thresholds are implicated: whether to change them. RESPECT
   USER-TUNED VALUES - never propose resetting active_window_minutes,
   escalation tiers, or stale_after_days the user chose, unless a
   finding directly implicates one.

## Phase 3 - apply and verify

1. Edit ccdoing.yaml with ONLY the approved deltas.
2. `ccdoing tick` - confirm the changed signals now read `ok`
   (status.json `state` fields) and the maintenance list shrank
   accordingly. A fixed signal that still reads no-match means the new
   target is wrong: investigate again rather than shipping it.
3. `ccdoing doctor --drift` - confirm the findings you addressed are
   gone; report any you deliberately left, with the reason.

## Notes

- Drift findings are deterministic inventory/bookkeeping, not judgment:
  treat them as leads to investigate, not orders to obey.
- If the config has drifted so far a rebuild would genuinely be cleaner,
  say so and hand off to /ccdoing:setup's update mode instead of
  rebuilding here.
