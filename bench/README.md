# Factory benchmark: old skill (`main`) vs new (`feat/devin-builder`)

Same 8 tasks, same synthetic repo (`seed/`, service-shaped: role store, audit log, tools),
run in two isolated arms in parallel. Nothing from a real project is used; no arm has a remote.

| task | size | models a feature of a typical service backlog |
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

## Known limits
- Devin's tokens are not visible in Claude transcripts; the new arm's coding cost shows as time.
- One run per task is a direction, not significance.
- Whether Orca workers honor `CLAUDE_CONFIG_DIR` is unverified (protocol step 2).
