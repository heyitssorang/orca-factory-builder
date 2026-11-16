# Factory amendment: local mode and `/factory init` (2026-10-17)

Amends `2026-10-16-factory-skill-design.md` and `2026-10-16-factory-integration-branch-design.md`.
Where they disagree, this file wins.

## 1. Local mode

Before this change, a repo had to have an Azure DevOps origin. Now every run has a mode,
decided by `factory_push.py mode --repo <repo>` (`repo_info`) and recorded in the run file as
`mode`:

- `ado`: `remote.origin.url` is an allowed Azure DevOps https URL (`validate_origin`,
  `FACTORY_ADO_ORGS`). Behaviour is unchanged.
- `local`: no origin, or any other origin (GitHub, a path, a disallowed org). Nothing fetches
  from or pushes to the remote.

A run file without `mode` is `ado`, so runs that existed before this change keep their
behaviour.

What changes in a local run:

| Step | ado | local |
|---|---|---|
| `start-run` | Fetches `D` and bases `RB` on the server tip; writes `refs-before.json` | Bases `RB` on local `refs/heads/D`; no fetch, no snapshot |
| Rule 6 (`config_check`) | Origin must be allowed and equal to the recorded URL | Origin need not be allowed, but must equal the recorded URL, or be absent when none was recorded |
| Rule 3 (`_check`) | Base must be an ancestor of `refs/remotes/origin/D`; the remote branch must fast-forward | Base must be an ancestor of `refs/heads/D`; no `ls-remote` |
| Merge backstop | Compares remote refs with the snapshot | Skipped |
| Job overlap label | `origin/D` | `local/D` (also a draft reason) |
| `ship-prepare` | Merges onto the server tip | Merges onto local `D`'s tip |
| Ship steps 6–7 | Asks, then `factory_push.py ship` | Skipped. `factory_push.py ship` refuses a local run, and `ship-finish --pushed yes` is refused |
| `ship-status` | `origin_has_merge` from the server | `mode: local`, `origin_has_merge` null |

Rule 6 still requires the recorded origin in local mode. If it did not, a worker could add or
redirect `remote.origin.url`, and the user's own later `git push` would go to the worker's
URL. Every other rule 6 check (hooks, fsmonitor, filters, credential and URL rewrites) applies
unchanged. The mode is recorded once, at `start-run`. Later steps read it from the run file and
never work it out again, so a worker that changes the origin cannot switch a run's mode.

## 2. `/factory init [<path>]`

The skill text is in `skills/factory/references/init.md`; the script is `scripts/factory_init.py`:

- `inspect`: whether the path is a git repo, whether it has commits, its mode, origin, default
  branch, the AGENTS.md keys in the working tree and at `D`, and any tracked browser config.
- `git-init`: creates the directory, runs `git init -b <branch>` and makes one empty commit.
  It refuses a path inside another work tree. Existing files stay untracked.
- `agents`: detects `install`/`test`/`build`/`dev`/`ui_globs` in this order: Makefile targets,
  then node (npm, pnpm, yarn, bun), cargo, go or python, then a static `index.html` for `dev`.
  It proposes lines only for keys that AGENTS.md lacks. `--set k=v` overrides a guess, and an
  empty value drops the key. `--write` appends the lines and never edits existing ones.

Init also registers the repo in Orca (`orca repo add --path`) when it isn't registered yet. It
never commits AGENTS.md. The factory reads AGENTS.md from `D`'s committed tip, so the user
commits it, and pushes it for an `ado` repo.

## 3. Skill length

The lint caps SKILL.md at 500 lines. To stay under it, Resume (§12) and Status/close (§14)
moved to `references/resume.md` and `references/status-close.md`. Their text is unchanged.
