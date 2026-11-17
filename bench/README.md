# Factory benchmark: old skill (`main`) vs new (`feat/devin-builder`)

Same 8 tasks, same synthetic repo (`seed/`, server-tools-shaped: role store, audit log, tools),
run in two isolated arms in parallel. Nothing from server-tools is used; no arm has a remote.

| task | size | models a feature of server-tools' backlog |
|---|---|---|
| 01 remove-member, 02 audit-time-filter | S | role store / audit viewer |
| 03 laps-lookup, 04 role-change-audit, 05 group-cache, 07 audit-viewer-page | M | LAPS tool, G1b audit before/after, DirectMemberOf cache, UI |
| 06 disable-recover-user, 08 compare-user-and-docs | L | Phase 3 tools, CompareUser + docs |

Tasks 03/06/08 all touch `cli.py`, and 01/04 both touch `roles.py`: parallel jobs will conflict,
as in real use. Grading runs on each job's own `factory/<id>` branch, so a conflict hold costs
that arm time, not the grade.

## Protocol
1. `python3 bench/bench.py setup --root ~/projects/factory-bench` (commit this branch first;
   `new` is copied from the working tree, `old` is a detached worktree of `main`).
2. Probe: in each arm run one trivial job and confirm in `<arm>/bench-<arm>/.factory` that the arm's own
   agents were used (new: `context.md` and `plan.md` exist; old: they do not). If Orca workers
   ignore `CLAUDE_CONFIG_DIR`, both arms use `~/.claude`: then run the arms one after the other,
   swapping the install, and say so in the report.
3. Start both `RUN.md` commands at the same time (two terminals). Each arm runs all 8 tasks with
   `--jobs 4`.
4. `bench.py collect`, `grade`, `pack`; judge each `judge/<task>.md` with a model that has not seen
   `KEY.json` (save the JSON reply as `<task>.json`); `bench.py report`.
5. `python3 bench/bench.py calibrate` fits `bench/prices.json` from Claude's own `costUSD` (3 tiny
   `claude -p` calls per model; real tokens, mostly Opus). It refuses to write a model whose fit
   is off by more than 3%; fill that one by hand. Repeat noisy tasks.

## What is measured
- **Quality:** hidden acceptance tests (`tasks/*/accept_test.py`, validated green on `reference/`,
  red on `seed/`), repo suite and build, diff size, tests added, blinded pairwise Opus judge
  (correctness, simplicity, style, tests, safety).
- **Time:** job wall time, arm wall time (parallel), sum of worker spans.
- **Tokens/cost:** summed from each worker transcript, per job and per model, deduplicated by
  message id; coordinator tokens reported separately (shared by all jobs).
- **Process:** review rounds, verdicts per lens, confirmed/false-positive findings, risk flags,
  holds, Devin fallbacks, builder engine, whole-repo checks result.

## Measuring subscription usage (Sonnet vs Devin)

The token numbers above are API-priced estimates and cannot see Devin. To compare what each
builder takes from your subscriptions, read both usage meters before and after a run:

1. Before starting, record the Claude figures (`/usage` in Claude Code, or Settings > Usage on
   claude.ai): the 5-hour and weekly percentages, and any per-model weekly line. Record the Devin
   account's own usage or credits figure from its usage or billing page. The `devin` CLI has no
   usage command.
2. Run one builder arm alone (`BENCH_ARMS=sonnet`), with nothing else using either account.
   Record both meters again, then do the same for the other arm (`BENCH_ARMS=new`). Wait for the
   5-hour window to reset between arms, or take the weekly delta.
3. Per arm, the Claude delta is what that arm took from the Claude subscription. The Devin delta
   is what the Devin builder took. The spec and review stages are Claude workers in both arms, so
   only the builder step differs.
4. Report the deltas next to the metered cost from `bench.py report`. Percentages are coarse and
   the limits weight cached tokens and models differently, so repeat the run when the gap is small.

### Measured: Sonnet arm, 8 tasks (2026-11-16)

One run of the Sonnet builder arm (scout and planner mandatory, medium effort, `--jobs 4`),
nothing else using the account. Coordinator wall time 25 min.

| meter | before | after | change |
|---|---|---|---|
| 5-hour session | 0% | 15% | +15 |
| week, all models | 54% | 56% | +2 (1 to 3 after rounding) |

Tokens in the run window, summed from transcripts (all sessions on the machine, deduplicated):

| model | input | output | cache read | cache write | est. $ (API prices) |
|---|---|---|---|---|---|
| Opus 5.5 | 284 | 84,050 | 1,610,569 | 498,703 | 5.99 |
| Sonnet 5.5 | 681 | 141,780 | 16,621,596 | 1,475,548 | 8.96 |
| Haiku 5.5 | 100 | 48,974 | 643,764 | 160,055 | 0.06 |
| **total** | 1,065 | 274,804 | 18,875,929 | 2,134,306 | **15.01** |

Per 1% of each meter (total tokens in the run divided by the change):

| per 1% of | output | cache read | cache write | all tokens | API-priced $ |
|---|---|---|---|---|---|
| 5-hour session (15%) | 18.3k | 1.26M | 142k | **1.42M** | about 1.0 |
| week (2%; range 1 to 3%) | 137k | 9.4M | 1.07M | **10.6M** (7.1M to 21M) | about 7.5 (5 to 15) |

Reading: one run of this size is about 15% of a 5-hour window and about 2% of a week, so the
weekly limit is roughly 7 to 8 times the 5-hour limit. Cache reads are about 89% of all tokens
but cost little at API prices; output and cache writes carry most of the price. The Opus share
(8% of messages, 40% of the dollars) comes from the planner and review workers. A single run
cannot show whether the meters weight Opus tokens differently from Sonnet tokens; that needs
runs with a different model mix. The weekly figure is the coarsest: a whole-percent meter and a
delta of 2 leave a wide range, so repeat or run larger batches before quoting it. The window
includes a few turns of the interactive session that launched the run (about $0.34).

### Measured: Devin arm, 8 tasks (2026-11-16)

One run of the Devin builder arm (same setup as above, `builder: devin`). Coordinator wall time
38 min. The Devin account is on a free tier and showed 0 usage before the run; no after reading
was available, so Devin's own spend is not measured.

| meter | before | after | change |
|---|---|---|---|
| 5-hour session | 15% | 29% | +14 |
| week, all models | 56% | 57% | +1 (0 to 2 after rounding) |

Tokens in the run window (all Claude sessions on the machine):

| model | output | cache read | cache write | est. $ (API prices) |
|---|---|---|---|---|
| Opus 5.5 | 0.06M | 0.9M | 0.39M | 4.63 |
| Sonnet 5.5 | 0.09M | 14.4M | 1.11M | 6.77 |
| Haiku 5.5 | 0.04M | 0.6M | 0.16M | 0.06 |
| **total** | 0.19M | 15.9M | 1.66M | **11.46** |

Per 1% of session (14%): about 1.27M tokens (Sonnet arm: 1.42M). The weekly change of 1 is too
coarse to divide (0 to 2%).

Sonnet vs Devin builder, Claude side only: est. API cost $15.01 vs $11.46 (-24%); session
+15 vs +14 points. The session meters are within rounding of each other, so on this evidence the
Devin builder saved little Claude quota: the spec, planner and review workers dominate. Outcome
differed: Devin arm merged 4 of 8 jobs, with 2 conflict holds, 1 secret-scan block and 1
malformed output; the Sonnet arm was not graded. Single run each; treat as a direction.

## Known limits
- Devin's tokens are not visible in Claude transcripts; the new arm's coding cost shows as time.
- One run per task is a direction, not significance.
- Whether Orca workers honor `CLAUDE_CONFIG_DIR` is unverified (protocol step 2).
