# Benchmark: old factory skill (main) vs new (feat/devin-builder), 2026-11-16

Synthetic ops-console repo, 8 tasks, both arms in parallel (`--jobs 4` each), one run per task.
Method and harness: `bench/README.md`. Prices fitted by `bench.py calibrate`. Devin is not metered.

## Findings
1. **No token or cost saving on these tasks.** Workers + coordinator: old $21.65, new $22.10 (+2%);
   tokens 3.65M vs 3.73M. Wall time: arm 19m07s vs 19m58s.
2. **Where the money goes.** Old arm, Sonnet: output 134k tokens ($1.3) vs cache writes 2.94M
   ($11.8) and cache reads 14.4M ($1.4). Every Orca worker session writes a ~51k-token prompt
   to cache before it does anything (a trivial `claude -p ok` call writes 51,459), so cost
   scales with the number of worker sessions (about 7 per job), not with code written. Devin
   replaces one session of seven and adds scout, planner and larger Opus reviews.
3. **Quality, hidden accept tests:** old 8/8, new 7/8. The miss is task 03: Devin met all 9
   criteria (18 tests) but the secret guard (rule C4, a `password = "..."` literal in test
   fixtures) blocked the commit. The old arm's builder committed it; the same guard would
   likely stop it at ship (not tested).
4. **Quality, blinded Opus judge** (7 tasks with two diffs, max 25 each): old 153, new 150; per-task
   winners old 3, new 4. Equivalent.
5. **Holds:** old 2 (merge conflicts), new 2 (one conflict, one reviewer check-line format).
6. **Harness defects found by the probe and fixed:** worker isolation, hard-coded script path,
   Devin report glued to narration, bare `lint — not run` lines, missing `.gitignore`.
7. Limits: one run per task; task 07 accept test relaxed after the run (header label) and both
   arms re-graded; `devin_fallback` in the tables also marks a blocked Devin build.

## Per task

| task | arm | engine | accept | suite | rounds | stage | wall | tokens (in+out+cache write) | cost | judge |
|---|---|---|---|---|---|---|---|---|---|---|
| 01-remove-member | old | claude | PASS | ok | 1 | merged | 9m30s | 1,005,159 | $5.47 | 23 |
| 01-remove-member | new | devin | PASS | ok | 1 | merged | 10m30s | 1,142,905 | $6.06 | 23 (win) |
| 02-audit-time-filter | old | claude | PASS | ok | 1 | merged | 9m25s | 333,485 | $1.87 | 22 |
| 02-audit-time-filter | new | devin | PASS | ok | 1 | merged | 11m07s | 354,248 | $1.97 | 24 (win) |
| 03-laps-lookup | old | claude | PASS | ok | 1 | merged | 9m26s | 346,609 | $1.98 | 24 (win) |
| 03-laps-lookup | new | devin | FAIL 5 | ok | 0 | blocked | 10m38s | 133,082 | $0.68 | 7 |
| 04-role-change-audit | old | claude | PASS | ok | 1 | held | 9m26s | 340,761 | $1.90 | 22 |
| 04-role-change-audit | new | devin | PASS | ok | 1 | merged | 6m47s | 370,447 | $2.04 | 23 (win) |
| 05-group-cache | old | claude | PASS | ok | 1 | merged | 7m52s | 345,374 | $1.91 | 22 (win) |
| 05-group-cache | new | devin | PASS | ok | 1 | merged | 4m58s | 366,111 | $1.97 | 21 |
| 06-disable-recover-user | old | claude | PASS | ok | 1 | held | 7m53s | 372,790 | $2.18 | 23 (win) |
| 06-disable-recover-user | new | devin | PASS | ok | 1 | held | 7m38s | 393,817 | $2.22 | 22 |
| 07-audit-viewer-page | old | claude | PASS | ok | 1 | merged | 9m32s | 534,052 | $2.85 | 18 |
| 07-audit-viewer-page | new | devin | PASS | ok | 1 | held | 8m04s | 571,120 | $3.46 | 18 (win) |
| 08-compare-user-and-docs | old | claude | PASS | ok | 1 | merged | 7m54s | 374,076 | $2.18 | 23 (win) |
| 08-compare-user-and-docs | new | devin | PASS | ok | 1 | merged | 7m56s | 398,622 | $2.26 | 19 |

## Totals

| arm | accept pass | weighted pass | sum of job walls | arm wall (parallel) | tokens | cost | coordinator tokens (shared) |
|---|---|---|---|---|---|---|---|
| old | 8/8 | 20/20 | 70m58s | 19m07s | 3,652,306 | $20.33 | 139,011 |
| new | 7/8 | 17/20 | 67m38s | 19m58s | 3,730,352 | $20.66 | 130,761 |

## Session usage (the `/cost` view)

| arm | coordinator session cost (Claude) | coordinator cost from transcripts x prices | worker cost (transcripts x prices) | total | Devin |
|---|---|---|---|---|---|
| old | $1.32 | $1.32 | $20.33 | $21.65 | - |
| new | $1.44 | $1.44 | $20.66 | $22.10 | $0, not metered |

The coordinator column is Claude's own number; if the next column differs, `prices.json` is wrong and every worker cost above is off by the same factor.


## Process and risk signals

| task | arm | files | +lines | -lines | tests added | verified (conf/FP/unv) | reviews | risk | devin fallback | checks |
|---|---|---|---|---|---|---|---|---|---|---|
| 01-remove-member | old | 2 | 38 | 0 | True | 0/0/0 | s:P/c:P/u:P/u:P | FLAGGED | False | - |
| 01-remove-member | new | 2 | 54 | 0 | True | 0/0/0 |  | FLAGGED | False | CHECKS: PASS |
| 02-audit-time-filter | old | 3 | 103 | 2 | True | 0/0/0 | s:P/c:P/u:P/u:P | FLAGGED | False | - |
| 02-audit-time-filter | new | 3 | 134 | 2 | True | 0/0/0 |  | FLAGGED | False | CHECKS: PASS |
| 03-laps-lookup | old | 3 | 116 | 0 | True | 0/0/0 | s:P/c:P/u:P/u:P | FLAGGED | False | - |
| 03-laps-lookup | new | 0 | 0 | 0 | False | 0/0/0 |  | None | True | - |
| 04-role-change-audit | old | 2 | 98 | 4 | True | 0/0/0 | s:P/c:P/u:P/u:P | FLAGGED | False | - |
| 04-role-change-audit | new | 2 | 131 | 4 | True | 0/0/0 |  | FLAGGED | False | CHECKS: PASS |
| 05-group-cache | old | 2 | 180 | 0 | True | 0/0/0 | s:P/c:P/u:P/u:P | FLAGGED | False | - |
| 05-group-cache | new | 2 | 166 | 0 | True | 0/0/0 |  | FLAGGED | False | CHECKS: PASS |
| 06-disable-recover-user | old | 4 | 235 | 0 | True | 0/0/0 | s:P/c:P/u:P/u:P | FLAGGED | False | - |
| 06-disable-recover-user | new | 4 | 221 | 0 | True | 0/0/0 |  | FLAGGED | False | CHECKS: PASS |
| 07-audit-viewer-page | old | 3 | 89 | 1 | False | 0/0/0 | s:P/c:P/u:P/u:P | FLAGGED | False | - |
| 07-audit-viewer-page | new | 3 | 126 | 1 | False | 0/0/0 |  | FLAGGED | False | CHECKS: PASS |
| 08-compare-user-and-docs | old | 5 | 270 | 0 | True | 0/0/0 | s:P/c:P/u:P/u:P | FLAGGED | False | - |
| 08-compare-user-and-docs | new | 4 | 274 | 2 | True | 0/0/0 |  | FLAGGED | False | CHECKS: PASS |

Notes: tokens exclude cache reads (they dominate counts and are priced separately); Devin tokens are not visible to Claude transcripts, so the new arm's coding cost is time only. Cost needs `bench/prices.json`. One run per task is a direction, not significance.

## Follow-up: cutting worker session overhead (new arm, tasks 02 and 05, one run each)

| variant | 02 audit-time-filter | 05 group-cache | cache-write tokens (02 / 05) |
|---|---|---|---|
| full benchmark (no lean, 4 review workers) | $1.97 | $1.97 | 331k / 343k |
| + lean worker context (`factory_lean.py`) | $1.44 | $1.28 | 198k / 183k |
| + grouped review workers (security+code, risk folded in) | $1.19 | $1.14 | 151k / 145k |

Worker cost only (the coordinator and the spec worker are not lean). Hidden accept tests and repo suite
passed in every variant. Measured base prompt of a trivial call: 66.4k default, 31.6k with plugins and
unused MCP servers off (plugins 19.5k, claude.ai connectors about 12k). A first reading of the grouped run
($2.63 / $2.42) was wrong: it reused the previous run's job ids, so two runs' transcripts were summed;
`collect` now ignores sessions older than the job.

## Follow-up 2: Sonnet builder vs Devin builder, same skill (lean + grouped reviews + checks)

Same 8 tasks, one run each, both arms on `feat/devin-builder` at 05761d5+; only the builder differs
(`sonnet` = no `builder:` line, `new` = `builder: devin`). Devin saw only the synthetic seed.

| | sonnet builder | Devin builder |
|---|---|---|
| accept tests / suite | 8/8, ok | 8/8, ok |
| worker cost (transcripts x fitted prices) | $9.47 | $9.98 (+5%, Devin itself not metered) |
| coordinator cost | $1.97 | $1.55 |
| total | $11.44 | $11.54 |
| sum of job walls / arm wall | 50m / 14m12s | 57m / 16m27s |
| blinded Opus judge, summed scores | 178 | 177 (4 wins each) |
| jobs held on merge conflict | 2 | 3 |

Result: no cost or speed saving from the Devin builder, and no quality difference. The savings
seen earlier came from the lean worker context, grouped reviews and the whole-repo checks step,
which both arms share. Recommendation: Sonnet builder by default; Devin stays opt-in.
Limits: one run per task; Devin tokens not visible to Claude; held jobs are parallel-job merge
conflicts (tasks touching the same files), not quality failures.

Correction to Follow-up 2: the arms were not identical on tasks 01-04. The Sonnet arm's jobs
001-004 have no `context.md` or `plan.md` (the coordinator skipped the scout and planner steps;
no Haiku sessions, less Opus), while all 8 Devin-arm jobs ran them. On the four jobs where both
arms ran the same spec stage (05-08): Sonnet $6.17 vs Devin $5.63 (-9%, Claude-side cost only;
Devin itself not metered). On 01-04 the Sonnet arm was $3.29 vs $4.36, which is mostly the skipped
planner. So "Devin costs 5% more" is not supported; "no clear saving, small possible saving on
larger tasks" is. Re-run needed with the scout/planner steps forced in both arms.

## Follow-up 3: Sonnet vs Devin builder, scout and planner forced on, medium effort everywhere

Same skill (lean, grouped reviews, whole-repo checks), `effort: medium` on every persona and worker
session, scout and planner mandatory in both arms (all 16 jobs have context.md and plan.md).
One run per task; Devin saw only the synthetic seed.

| | sonnet builder | Devin builder |
|---|---|---|
| accept tests / suite | 8/8, ok | 8/8, ok |
| worker cost | $10.69 | $9.80 (-8%) |
| total with coordinator | $11.98 | $11.28 (-6%) |
| tokens (in+out+cache write) | 1.59M | 1.42M (-11%) |
| sum of job walls / arm wall | 65m / 20m57s | 60m / 20m52s |
| blinded Opus judge, summed | 173 | 179 (5 wins, 1 tie, 2 losses) |
| jobs held on merge conflict | 3 | 3 |

Devin was cheaper on 7 of 8 tasks (not 04). The saving is small (6-8% of Claude-side cost; Devin's
own spend is not metered) and rests on one run per task, so treat it as a lean, not a proof.
This corrects Follow-up 2, whose arms were not like for like.
