# Orca Factory Builder

Orca Factory Builder is a Claude Code skill, `/factory`, that turns feature requests into reviewed, locally merged commits.
It supervises a team of Claude workers running in Orca (the agent IDE) worktrees,
so you describe a feature and get back a branch that has been specified, built, checked and
reviewed, without babysitting each step.

It is a coordinator, not a coder: the skill never writes feature code or merges anything itself.
Every deterministic decision (who owns a job, how many workers run, whether a review passed,
whether a push is allowed) is made by a small tested Python script, and the model only acts on
the script's output.

## How a job flows

```
request ─▶ spec ─▶ build ─▶ checks ─▶ review ─▶ verify ─▶ merge into a run branch
```

1. **Spec.** A scout (Haiku) reads the repo and writes `context.md`. A spec writer (Sonnet) writes
   `spec.md` with numbered, checkable acceptance criteria. A planner (Opus) writes `plan.md`, an
   exact step-by-step plan a cheaper coding model can follow.
2. **Build.** A builder implements the plan on a `factory/<job-id>` branch in its own worktree and
   reports per acceptance criterion. The builder is Sonnet by default. Devin can be used as an
   opt-in engine (`builder: devin`), with Sonnet as the fallback.
3. **Checks.** The repo's own install, test and build commands run before any reviewer starts.
4. **Review.** Security and code review run as one grouped worker with risk flagging folded in.
   UX and UI reviewers run only when the change has a UI, against the job's dev server, with
   screenshots. Every `CHANGES` finding is independently verified before it counts.
5. **Merge.** A ready job is squash-merged into the run's local `factory-run/<run-id>` branch. A
   draft job is held until you `/factory accept` or `/factory rework` it.
6. **Ship.** `/factory ship` gates a run (secret scan, install/test/build in a throwaway worktree),
   merges it into the default branch, and asks once before pushing. Pushing is only possible for
   Azure DevOps origins whose org you list in `FACTORY_ADO_ORGS`. Any other repo runs local-only
   and is never pushed.

## Commands

`/factory <feature>` · `init` · `backlog add|status|edit|remove` · `next` · `run --jobs N` ·
`accept` · `rework` · `ship` · `resume` · `status` · `dashboard` · `close` · `migrate` · `help`

Each repo keeps its state in `<repo>/.factory/`. The backlog is committed. Jobs and runs stay
local. `/factory dashboard` serves a live page with one swim lane per repo. Several jobs can run
at once.

## Cost design

The skill is built to spend few tokens:

- Each persona has a pinned model and `effort: medium`: Haiku for reading, Sonnet for spec and
  relay, Opus for planning and review.
- Workers start lean: `scripts/factory_lean.py` writes an untracked per-worktree settings file
  that turns off the user's plugins and unused MCP servers (about -50% on the worker prompt).
- Reviews are grouped into one worker instead of one per lens.

The `bench/` directory holds the measurements:

- **Builder benchmark.** Eight tasks on a synthetic repo, Sonnet builder against Devin builder.
  Both finished 8/8. Devin came out about 6% cheaper on metered cost, but Devin's own spend is
  not metered, so the saving is unproven.
- **Review benchmark.** Six planted defects and two clean controls. Grouped review found 6/6, the
  same as three separate workers, at roughly half the cost.

Reports are in `docs/reports/`.

## Requirements

- Linux with Python 3 and `git`.
- Claude Code and the Orca CLI (the `orchestration` skill documents how to find it).
- The two review-verifier agents and the `differential-review` skill. They are bundled in
  `vendor/` and installed for you if you don't already have them (see below).
- Optional: a Devin CLI for the Devin builder, and Chrome DevTools MCP for the UX and UI reviewers.

## Install

```sh
python3 scripts/lint_roster.py          # check the personas
python3 scripts/install.py install      # copies the skill, agents and bundled dependencies into ~/.claude
python3 scripts/install.py uninstall    # removes only files unchanged since install
```

`install` refuses to run if anything it would write already exists, so uninstall an old copy
first. Bundled dependencies are the exception: if you already have one in `~/.claude`, it is
used as is and left alone, and `uninstall` removes only what `install` added. To tune the lean worker settings for your machine, run
`python3 ~/.claude/skills/factory/scripts/factory_lean.py learn` once. Start a new Claude Code
session afterwards, then run `/factory init <path>` on a repo.

## Layout

| path | what is in it |
|---|---|
| `skills/factory/` | `SKILL.md` (the coordinator's instructions) and worker task texts |
| `roster/` | the ten worker personas (scout, spec writer, planner, builder, reviewers, verifier) |
| `scripts/` | the deterministic scripts: backlog, jobs, scheduling, review routing, merge, push guard, dashboard, lean settings, Devin engine, installer, roster lint |
| `dashboard/` | the swim-lane dashboard page |
| `tests/` | unit tests (`python3 -m unittest discover -s tests`) |
| `vendor/` | bundled dependencies: the two finding-verifier agents and the `differential-review` skill |
| `bench/` | the benchmarks and their reference solutions |
| `docs/` | design specs, plans, review notes and run reports |

## Third-party content

`vendor/skills/differential-review/` is the unmodified
[Trail of Bits](https://github.com/trailofbits/skills) skill of that name (commit `1f68e03`),
licensed CC BY-SA 4.0. Its `LICENSE.upstream` is kept alongside it, and that licence applies to
that directory only. See `vendor/README.md`.

## Tests

```sh
python3 -m unittest discover -s tests
```

## Safety notes

- Nothing is pushed except by `factory_push.py ship`, after you answer yes in that same command.
- Workers have push disabled in their worktrees, and `ship` scans for secrets first.
- `.factory/` stays local and `git clean -fdx` deletes it.

## Status

Built, tested and used on real runs (see `docs/reports/2026-10-17-live-run.md`). The Devin
builder has not had a live run on a real repo. Known open items are listed in the run reports.
