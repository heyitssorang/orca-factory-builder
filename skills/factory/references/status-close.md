# Factory status and close

Names as in `SKILL.md` §0; section numbers refer to `SKILL.md`.

- `/factory status [<id>] [--repo <path> | --all]`: a job id → `python3 S/factory_job.py show
  <id>`; a run id → `python3 S/factory_integrate.py status --run <id>`; else every
  `H/jobs/*/job.json` of `REPO` as a table (id, run, feature, stage, round, reviews, risk; a `failed` job's `reason`) and
  every unshipped run's status (merged, held, unshipped). `--all`: the same per repo, for each
  `ORCA repo list` repo that has a `.factory/` (each with its own `FACTORY_HOME`). Stale: build the comma list of job ids with a live worker (`ORCA
  orchestration worker-show --dispatch <id> --json` for the dispatches in `workers`, liveness
  `live`) and run `python3 S/factory_backlog.py stale --live <ids>`; list what it prints and
  suggest `/factory resume <id>`. Never re-claim them yourself.
- `/factory close <id>`: for a job the user is done with (stage `held`, `blocked`, `failed` or
  `shipped`; never `merged`, which waits for its ship). Run setup first (it gives the `RUN` that
  owns the writes). `factory_job.py takeover <id> --owner <RUN> --reason close` if another run
  owns it. Stop its dev server if `server_pgid` is set. Remove the worktree through Orca:
  `ORCA worktree rm --worktree id:<worktree_id> --json`. Only when the stage is `shipped`,
  `git -C <repo> branch -D B` (hygiene prefix); otherwise `B` stays. Stage `closed`; `python3
  S/factory_backlog.py finish <id>`. Nothing remote exists to delete.
