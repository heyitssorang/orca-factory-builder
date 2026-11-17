# Lean spec workdir (spec stage only)

Spec workers (scout, spec-writer, planner) used to run on the repo's main workspace, which has no
lean settings, so every spec worker wrote the user's whole plugin and MCP setup into the cache
(about 67k tokens per session, measured in bench4). They now run in one lean, read-only
worktree per run when this file's steps all succeed; any failure keeps the old behaviour.

**Not used when:** `FACTORY_LEAN=off`, or the job has `after:` set (its base is the run tip, §5
step 1; a worktree of `DREF` would not hold the dependency's code). Those jobs use the main workspace.

`SW` is the run's spec worktree, named `factory-spec-<RID>`; it is found by that name, so nothing
is recorded.

1. **Find or create.** `ORCA worktree list --repo path:<repo> --json`: a worktree named
   `factory-spec-<RID>` exists → use it. Else config-check `<repo>`, fetch (Git hygiene), then
   `ORCA worktree create --repo path:<repo> --name factory-spec-<RID> --base-branch DREF --setup
   skip --json`. Failure → main workspace, stop here.
2. **Make it safe and lean**, in order, never in `<repo>`:
   `python3 S/factory_push.py disable --repo <repo> --worktree SW`, then `mkdir -p
   <home>/runs/<RID>.spec` and `python3 S/factory_lean.py write --worktree SW --job-dir
   <home>/runs/<RID>.spec`. Its `status` must be `lean`; `off` or `skipped` (or a failed disable)
   → remove it as in step 4 and use the main workspace: a worktree without lean settings saves nothing.
3. **Run the spec worker** with `--worktree id:<worktree_id of SW>` instead of the main workspace,
   and in the **spec** task text read `<repo>` as the path of `SW`. Everything else is unchanged:
   same agent, model, task, subagents and output paths (`J/context.md`, `J/spec.md`,
   `J/plan.md`, all outside `SW`). The spec task stays read only: a spec worker that changes
   `SW` (`git -C SW status --porcelain` not empty after it settles) → stage `failed`, say so.
4. **Remove it** after `ship-finish` (§13 step 8): `ORCA worktree rm --worktree id:<worktree_id
   of SW> --json`, then `git -C <repo> branch -D factory-spec-<RID>` with the hygiene prefix (Orca
   names the branch after the worktree). A missing worktree or branch is not an error. A run
   that never ships keeps `SW` until you remove it the same way (`close <id>` does not).

Never edit `<repo>/.claude/settings*.json`; the lean file exists only inside `SW`.
