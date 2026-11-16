# Factory amendment: per-repo homes, backlog commands, live swim-lane dashboard (2026-10-17)

Amends the factory specs, including `2026-10-17-factory-local-mode-and-init.md`. Where they
disagree, this file wins.

## 1. Home per repo

The single home, `~/.orca-reports/factory`, is replaced by one home per repo:
`<repo>/.factory/`. `<repo>` is the main checkout: the parent of
`git rev-parse --git-common-dir`, so a linked worktree resolves to it as well.

- `fj.home()`: `FACTORY_HOME` if it is set, else the home of the repo containing the current
  directory. Outside a repo it is an error; there is no central default any more. The skill
  runs every script with `FACTORY_HOME=<repo>/.factory`.
- `fj.prepare_home()` writes `.factory/.gitignore` = `*`, `!.gitignore`, `!backlog.md`.
  The backlog is committed with the repo. Jobs, runs, locks and reports stay local.
- `start-run` refuses a home named `.factory` that belongs to another repo.
- Job numbers count per repo. Run ids stay globally unique (timestamp plus random hex).
- `factory_push.py ship` reads the run file from `fj.home(--repo)`.

**Trust.** The original spec already accepts (§12) that workers have unrestricted Bash. A home
inside the repo is therefore no more reachable than the central one was. One new path does
exist: the tracked `backlog.md` reaching `D` through a job's diff. Guard **rule 7** refuses any
job whose diff touches `.factory/`. `git clean -fdx` deletes the local part of a home, and the
user accepted that.

Backlog lines from teammates carry `install:"…"` commands. They have the same trust as
AGENTS.md (the repo's content), and an install still needs the user's approval every run.

## 2. Backlog

Line format: `- [<state>] <feature> [after:] [default:] [wi:] [install:"…"] [→ <job-id> [stage]]`.
The old `<repo> :: ` prefix is gone, because the file's repo is the repo. Items are numbered
from 1 in file order, and other lines are kept.

The script commands are `add`, `list`, `edit <n>` (todo items only), `remove <n>` (claimed items
refused), `claim`, `finish` and `stale`. Text that is empty, spans several lines, or ends in a
job marker is refused. The skill commands are `/factory backlog [status|add|edit|remove]`
(`references/backlog.md`). They never commit.

## 3. Dashboard: live sessions in repo swim lanes

There is one server for all repos. Its pid and log live in `~/.cache/orca-factory`
(`FACTORY_DASHBOARD_STATE`). Repos come from `orca repo list`; `FACTORY_REPOS` (a path list)
overrides that. Every repo with a `.factory/` gets a lane with four stage groups: Queue
(backlog), In flight (spec, build, review, verify), Needs you (held, blocked, failed) and Done
(merged, shipped, then closed; the 3 newest, the rest behind "Show more"). Done cards (merged, shipped, closed) are folded to their title and stage; a click unfolds the details and an "Open job files" button. Each card names its
stage and when the job last changed. Needs you sorts failed, blocked, held, and each card there
says why: the first `hold.reasons` line, the job's `reason`/`error`, or where to look. The page
is a read-only mirror: it never shows or runs `/factory` commands. In-flight jobs with no live
worker and no update for 20 minutes are flagged. When the server stops answering, a banner says
so and the board is dimmed; when Orca is unavailable, the live marks are muted. The job drawer
is a modal dialog: it summarises stage, reasons, paths, branch, worktree and run, groups files by
round, opens the most telling file (report, build or spec), renders Markdown, and refreshes while
open. Lanes with live or in-flight work sort first, then lanes with jobs waiting on the user.

A card is marked live when the dispatch in its `workers` shows `terminalState: active` in
`orca orchestration worker-list --run <job.owner>`. Only jobs in spec, build, review or verify
are looked up, one call per distinct owner run. `--run` is always passed: without it, Orca
limits the list to the run bound to the terminal that started the server, so other sessions'
workers would never show as live. Each run's answer is cached for 5 s, the repo list is
cached for 5 s, and the page polls every 3 s. The API is `/api/lanes`, and files are served at `/files/<lane-key>/<job>/<path>`.
`lane-key` is a hash of the home path, and a file is served only from the home of its own lane.

PNGs under `<job>/round-<n>/screenshots/` are now served as images. That is where reviewers
save them since the live-run fix; before this change only `<job>/screenshots/` was.

## 4. Migration

`factory_init.py migrate [--from ~/.orca-reports/factory] [--dry-run]` copies:

- each job, with its `.n-<n>` marker, to `<job.repo>/.factory/jobs/`;
- each run (its JSON, refs snapshot and lock dir) to `<run.repo>/.factory/runs/`;
- each old backlog line, without its `<repo> ::` prefix, to that repo's `backlog.md`.

It never overwrites an existing target, skips repos that no longer exist, and leaves the old
folder untouched.

## 5. Failure reason

`job.json` has `reason` (null by default). `factory_job.py update` refuses a patch that sets stage
`failed` without a non-empty `reason` string; it keeps the first 1000 characters, stripped. A
patch that moves the job to any other stage without its own `reason` clears it, so rework and
resume start clean. A merge error in `finish-job` records `merge failed: <reasons>`. The
dashboard and `/factory status` show it.
