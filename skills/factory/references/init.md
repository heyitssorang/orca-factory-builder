# Factory init (`/factory init [<path>]`) and migrate (`/factory migrate`)

Names (`S`, `ORCA`, `D`, `H`) as in `SKILL.md` §0. No dashboard, run setup, claim or worker.
`<path>` defaults to the current directory's repo (`REPO`), else the current directory;
resolve it to an absolute path. Never commit or push for the user.

1. `python3 S/factory_init.py inspect <path>` prints `{exists, git, inside_repo, commits, mode,
   origin_url, default, agents_md, browser_config_tracked, factory_home}`. `inside_repo` set: stop (it is a
   subdirectory of that repo; offer to init that one).
2. **Git**: `git` false or `commits` false → `python3 S/factory_init.py git-init <path>
   [--branch <name>]` (default `main`): creates the directory, `git init`, and one empty
   "Initial commit"; files already there stay untracked (it prints how many). Exit 2: report,
   stop. Then `inspect` again.
3. **Factory home**: `python3 S/factory_init.py home <path>` creates `<path>/.factory/` with its
   `.gitignore` (tracks only `backlog.md` and itself) and an empty `backlog.md`; it prints which
   of the two are already tracked.
4. **Orca** (the dashboard finds lanes through it): `ORCA repo list --json`; if no repo has `path` equal to `<path>`, `ORCA repo add
   --path <path> --json`. Report the repo id.
5. **AGENTS.md**: `python3 S/factory_init.py agents <path>` prints `{existing, proposed,
   lines, missing}`. Show `existing` and the proposed `lines`, and name the `missing` keys with
   what each costs: no `test`/`build` → the ship gate shows `not run`; no `dev` → the ux and ui
   reviews are `NOT RUN`; `ui_globs` falls back to the defaults. Ask the user once to approve or
   change the lines. Approved → `python3 S/factory_init.py agents <path> --write [--set
   <key>=<value> …]` (an empty value drops a key). It only appends; it never edits existing lines.
6. **Report**: the mode (`ado`: ship pushes `D` after your yes; `local`: never fetches or
   pushes, ship stops at local `D`), `D`, the Orca repo id, the AGENTS.md lines written, and
   `browser_config_tracked` (tracked `.mcp.json` or `.claude/settings.local.json` turn the
   browser lenses off). The factory reads AGENTS.md from `D`'s committed tip, so tell the user
   to commit it on `D` (ado: and push it) before the first `/factory` run; `agents_md.
   committed_keys` from `inspect` shows what the factory sees now. Also tell them to commit
   `.factory/.gitignore` and `.factory/backlog.md` when they are untracked; the rest of
   `.factory/` stays local (and `git clean -fdx` deletes it). Suggest a first job:
   `/factory <feature> --repo <path>` or `/factory backlog add <feature>`.

## Migrate

`/factory migrate` copies the old central state into the repos. Run `python3
S/factory_init.py migrate --dry-run`, which prints `{copied: {jobs, runs, backlog}, skipped}`
(the default source is `~/.orca-reports/factory`; `--from <dir>` sets another). Show what it would
copy and skip, and ask once. On yes, run it without `--dry-run`. It never overwrites a target,
leaves the old folder as it was, and is safe to run again. Then tell the user which repos have a
new or changed `.factory/backlog.md` to commit, and that the old folder can be deleted once they
are satisfied. Migrate writes each repo's `.factory/.gitignore` too.
