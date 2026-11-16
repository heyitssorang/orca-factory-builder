# Factory resume (`/factory resume <job-id>`)

Names as in `SKILL.md` §0; section numbers refer to `SKILL.md`.

1. Dashboard `ensure`. Take over in Orca: if `ORCA orchestration run-show --id <job.owner>`
   finds the old run and its coordinator is not active, adopt it as the orchestration guide's
   takeover reference says (load it first); otherwise use a new Orca run (run setup, no new
   factory run). Never take over a run whose coordinator is still working.
2. `python3 S/factory_job.py takeover <id> --owner <RUN> --reason resume` (the one job write
   allowed to a non-owner; it is logged in `job.json`), and `python3 S/factory_integrate.py
   takeover-run --run <job.run> --owner RUN --reason resume`. `RID` = `job.run`.
3. `python3 S/factory_job.py next <id>` prints the next step; act on it:

| Output | Continue at |
|---|---|
| `none` | Stage is `held`, `merged`, `shipped`, `closed`, `blocked` or `failed`; report it (held: suggest accept or rework) |
| `spec` | Step 2 |
| `build` | Step 3. If `job.worktree` exists: keep the recorded `base`, skip create, never `switch -C`; only check the branch is `B` (else `failed`) and run `disable`. With a `rework-<n>.md` newer than the round, use the builder (rework) task |
| `review` | `mark-review` if `job.review_round` is not `job.round`, then step 4 for round `job.round` (run only the §7 step 5 groups with a lens still missing a file; restart the dev server if ux/ui must run) |
| `verify` | Step 6 for findings without a verify file, plus risk if `risk.md` is missing; restart the dev server for `browser` findings. If `job.review_round` is not `job.round`, never `mark-review` (it would bless an unreviewed head): stop and tell the user to rework the job |
| `finish` | §8; `job.review_round` not `job.round` → as for `verify` |
