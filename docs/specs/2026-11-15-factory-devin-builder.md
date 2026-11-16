# Factory: Devin SWE-2 High as the builder (amendment)

Latest wins over earlier specs. Changes only who writes the code; spec, review, verify, risk,
merge and ship are unchanged.

## Rules
1. Opt-in per repo: a `builder: devin` line in the repo's AGENTS.md. Devin sends the spec and the
   code it reads to Cognition's service; a repo that must not do that omits the line.
   `FACTORY_BUILDER=claude` turns it off for one run.
2. `factory_devin.py engine --round <job.round>` decides: `devin` for `job.round` 0 (first build)
   and 1 (first rework), `claude` (builder-p7, Sonnet) from round 2 on, or when the CLI is missing.
3. Devin runs `devin --model swe-2-high --permission-mode smart --respect-workspace-trust false -p`
   in the job worktree with token-shaped variables removed. It does not commit (its permission
   layer rejects `git commit` in print mode); `factory_devin.py` commits with explicit paths.
4. The script, not Devin, establishes the facts: branch is `factory/<id>`, tree clean before,
   report parses and covers every acceptance criterion as met, files changed, no secret-shaped
   path or content (`factory_push.scan_commits`), tree clean after. Any miss → `STATUS: BLOCKED`
   with the reason; nothing is merged. A refused build resets the worktree to its pre-build
   commit (it was clean) and keeps what Devin wrote in `J/devin-leftover/` (paths only if
   secret-shaped; a leaked commit is dropped but stays in the local reflog). On timeout the
   whole Devin process group is killed. The report is read from the last `STATUS:` line. Lock files, manifests and CI files that changed are listed
   under `## Touched sensitive files`.
5. Exit 3 (Devin missing, not logged in, failed with no change) → the coordinator starts the
   Sonnet builder instead. A thin Sonnet worker only runs the script, so Orca scheduling, caps,
   `worker_done` and failure handling are unchanged.

## Model tiers (same amendment)
Inside the existing `spec` stage one Sonnet relay runs three subagents in turn: `scout-p7` (Haiku,
`J/context.md`: commands, files, patterns), `spec-writer-p7` (Sonnet, was Opus, `J/spec.md`) and
`planner-p7` (Opus, `J/plan.md`: file-by-file steps, tests, pitfalls). Devin gets `plan.md` in
its brief; the Sonnet builder gets its path. A missing `context.md` or `plan.md` is not a failure.
Coding is Devin; review, verify and risk keep their models.

## Whole-repo checks (same amendment)
Once per round, after the diff and before the reviewers, the coordinator runs `factory_review.py
checks`: the `test` and `build` commands from the branch's AGENTS.md, whole repo, same refusals,
scrubbed environment and process-group kill as `rerun`. Result: `R/checks.md`, first line
`CHECKS: PASS`, `FAIL` or `NOT RUN — <why>`. It never fails the job. Only the code reviewer reads
it: it does not repeat those two commands, and each failing one becomes a `CHANGES` finding that
the existing `rerun` route verifies.

## Known limits
- `--respect-workspace-trust false` (required in print mode) skips Devin's trust check on repo
  config; the per-repo opt-in is the control.
- No sandbox: `--sandbox` needs bubblewrap, not installed on the dev machine. `smart` mode lets
  a fast model approve commands; writes outside the worktree are not prevented.
- Devin's behaviour on large specs is untested in a live factory run.
