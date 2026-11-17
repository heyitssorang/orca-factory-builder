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

> **Superseded 2026-11-17:** the worker-cost and token rows above use single-rate cache-write pricing and
> an incomplete job total (spec-stage workers unattributed), and the source runs were not kept. Do not quote them.
> The re-derived comparison on bench4/bench5 is in "Note: Devin vs Sonnet, re-derived" at the end.

Devin was cheaper on 7 of 8 tasks (not 04). The saving is small (6-8% of Claude-side cost; Devin's
own spend is not metered) and rests on one run per task, so treat it as a lean, not a proof.
This corrects Follow-up 2, whose arms were not like for like.

## Errata: cache-write TTL (2026-11-17)

Earlier cost columns priced every cache write at one rate (2x input, the 1h rate). Transcripts
record the 5m/1h split; `bench.py` now keeps it and prices 5m writes at 1.25x input. The 5m
multiplier is now fitted for Sonnet and Haiku (`FORCE_PROMPT_CACHING_5M=1` runs: 1.25x, residual 0.0);
Opus is still assumed. The numbers below are API-equivalent estimates, not billed amounts and not
subscription use.

Recomputed offline from the surviving bench4 (Sonnet builder) and bench5 (Devin builder) transcripts
(no model calls). Token totals are unchanged; no cache-write tokens had an unknown TTL.

| arm | cache write tokens (5m / 1h) | worker $ before | worker $ corrected | coordinator $ (Claude's own) | coordinator recomputed |
|---|---|---|---|---|---|
| bench4 sonnet | 0.88M / 0.52M | 10.85 | 9.01 (-17%) | 1.15 | 1.15 (all 1h, unchanged) |
| bench5 devin | 0.66M / 0.38M | 7.85 | 6.49 (-17%) | 1.20 | 1.20 (unchanged) |

- The sentence "if the coordinator column differs, `prices.json` is wrong and every worker cost is
  off by the same factor" was wrong. The coordinator is a main session on 1h writes; workers write
  a 5m/1h mix, so a coordinator match says nothing about worker pricing.
- Worker costs fell about 17% with identical token counts. The Sonnet-vs-Devin gap is not
  re-derived here: the figures in Follow-up 3 above ($10.69 / $9.80 worker) do not reproduce from
  the current `metrics.json` (old-price recompute: $10.85 / $7.85), so that table should be
  regenerated from one collect run before it is quoted again.

## Errata 3: worker attribution (2026-11-17)

Two collector defects, both fixed in `bench/bench.py` (`attribute_groups`):

- Spec-stage workers run in the repo's main workspace. Their main session's brief never names
  the job, so the old text match missed every one of them (8 main sessions per arm, about $3.0).
  Workers are now grouped (main session plus `<id>/subagents/*`) and assigned to the job whose
  `.factory/jobs/<id>` path they name most. The handoff's "provisional job id" theory was only
  part of it; the dominant cause was the brief not naming the job.
- `take()` never returned True, so matched sessions were never marked claimed and were also
  counted in `unattributed_tokens`. Arm totals in P2 therefore double-counted about $2.4 per arm.

Re-collected offline from the same transcripts, unattributed is now $0.00 in both arms
(API-equivalent, assumed-multiplier pricing unchanged):

| arm | jobs $ before | jobs $ after | arm $ before | arm $ after | delivered | $ per delivered |
|---|---|---|---|---|---|---|
| bench4 sonnet | 9.01 | 11.98 | 15.54 | 13.13 | 5 of 8 | 2.63 (was 3.11) |
| bench5 devin | 6.49 | 9.49 | 13.02 | 10.68 | 4 of 8 | 2.67 (was 3.25) |

Per-job and per-stage worker cost is now readable; the earlier "Devin builder is 6-8% cheaper on
worker cost" comparison used the incomplete job totals and should be re-derived from these.
One run per arm, Devin spend unmetered: directional only.

## Note: worker launch path (P4 experiment 1, 2026-11-17)

Offline diagnosis from the collected transcripts, no model calls. All 48 worker main sessions
(26 in bench4, 22 in bench5) report entrypoint `cli`, CLI 2.1.326; none report `sdk-cli`, and no
helper or title sessions appear. Subagents (58 and 42) are also `cli`. The theory that workers run
on a headless/SDK launch path is not supported by this data, so nothing was changed for it.
Limits: it shows how sessions were launched, not how subscription quota is metered; one CLI
version, one launch mechanism, no non-Orca baseline.

## Note: relay output (P4 experiment 2, dropped, 2026-11-17)

"Relay" is the worker main session re-emitting a subagent's reply verbatim as a `Write` to a job
`.md` file (reviewers and verifiers have no `Write`, so they cannot write it themselves). Measured
offline with `collect_arm`'s session filter: bench4 27,921 output tokens ($0.28, 2.3% of $11.98 job
cost), bench5 14,347 ($0.14, 1.5% of $9.49), Sonnet output at $10/M, API-equivalent. That is under
the 3% bar for running the experiment, and two runs per arm cannot resolve it, so it was not run.

## Note: lean read-only spec worktree (P4 experiment 3, 2026-11-17)

Spec-only runs (`bench.py setup --spec-only`: scout, spec-writer and planner for 8 jobs, no build).
Baseline = spec workers on the repo's main workspace; lean = the run's `factory-spec-<RID>` worktree
with per-worktree lean settings (`references/spec-workdir.md`). Sonnet coordinator, 2 runs per arm,
API-equivalent $, assumed-TTL pricing unchanged.

| run | arm | usable specs | mean criteria | main cache write / session | spec stage | coordinator |
|---|---|---|---|---|---|---|
| 1 | baseline | 8/8 | 9.1 | 65,721 | $5.55 | $0.52 |
| 1 | lean | 8/8 | 8.6 | 29,603 | $3.77 | $0.64 |
| 2 | baseline | 8/8 | 7.9 | 66,720 | $5.27 | $0.55 |
| 2 | lean | 8/8 | 9.0 | 29,537 | $3.64 | $0.70 |

Spec stage averages $5.41 baseline vs $3.71 lean (-31%); with the coordinator, $5.95 vs $4.37 (-27%).
The drop comes from main-session cache writes falling from about 66k to about 29.5k per session
(plugin/MCP prefix no longer loaded). The lean coordinator costs about $0.14 more (extra worktree steps).

Limits: n=2 per arm, directional only. Spec quality is checked structurally only (usable spec, criteria
count; no pattern by arm), not graded for substance, and the effect on build and delivery was not tested.
Subscription 5h/weekly % was not measured; these are API-equivalent dollars. A first lean run (bench6)
was invalid because the arm repo was not registered in Orca (workers ran in the wrong workspace); `setup`
now registers it, and the numbers above come from the valid reruns (bench7, bench8).

## Note: subagent cache TTL (P4 experiment 4, dropped, 2026-11-17)

`subagentPromptCacheTtl` (env `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`) sets the cache TTL for subagents and
other non-main requests; unset means 5m. Step 1 replayed all 577 subagent sessions in the benchmark
project dirs offline (`bench/exp4_ttl_replay.py`, no model calls), API-equivalent $:

- Subagent cache writes: 8.85M tokens, all 5m (Opus 5.06M, Sonnet 2.05M, Haiku 1.74M).
- Cost of writing all of them at 1h instead: +$18.38 (fitted 1h minus 5m rate).
- Re-writes a longer TTL could avoid: 0 gaps over 5m inside any session; 64-66 first requests could
  reuse a sibling's entry that expired 5m-60m earlier, at most $1.35-1.49 (upper bound: assumes the whole
  first write is shared).

Maximum saving is about 8% of the added cost, so step 2 (live runs) was not run and nothing changed.
Limits: replay of existing runs, not a run with the setting on; subscription metering of 1h vs 5m writes
is unmeasured; main sessions (already 1h) were not part of this question.

## Note: Devin vs Sonnet, re-derived (2026-11-17)

From bench4 (Sonnet builder) and bench5 (Devin builder) `metrics.json` after the attribution and TTL fixes;
API-equivalent, Claude side only (Devin spend unmetered), one run per arm.

| view | Sonnet | Devin | Devin vs Sonnet |
|---|---|---|---|
| All 8 jobs, worker cost | $11.98 | $9.49 | -21% |
| 6 jobs not blocked in the Devin arm | $8.45 | $7.91 | -6.4% |
| Jobs merged in both arms (02, 05, 08) | $4.28 | $4.02 | -6.1% |
| Whole arm (with coordinator $1.15 / $1.20) | $13.13 | $10.68 | -19% |
| Cost per delivered job | $2.63 (5) | $2.67 (4) | about equal |

By session kind: subagents $5.76 vs $4.11, main sessions $6.21 vs $5.38. Outcome: accept tests 8/8 (weighted
20/20) vs 6/8 (15/20); Devin had 2 blocked jobs (03, 07: $0.80 and $0.77 vs $1.41 and $2.12), Sonnet none.
The all-jobs -21% is mostly those two blocked jobs doing less work; on comparable jobs the gap is about 6%
(about $0.09 per job), and cost per delivered job is a tie. Not split into build vs review (stored metrics have
no per-stage cost); bench5 was not re-collected with spec-stage fields. One run per arm: directional only.
