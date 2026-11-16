# Factory skill (P7) — Design Spec

Date: 2026-10-16
Status: Revised after the Opus review (`docs/reviews/2026-10-16-design-review-opus.md`); H1–H5,
M1–M6 and L1–L6 are applied. Decisions in §2 are the user's (2026-10-16).
Amended by `docs/specs/2026-10-16-factory-integration-branch-design.md` (run branch and
`/factory ship` replace per-job PRs); where they disagree, the amendment wins.
Scope: a new skill `factory` that takes one feature request to a reviewed Azure DevOps pull request,
using Orca workers, with several jobs in parallel (§8). Lives in its own repo, `~/projects/orca-factory`;
the P1–P6 packs (`~/projects/e2e-test-harness`) are not modified (§10).

Idea credit: the job/round file layout, verdict-first review files, the four review lenses, the
backlog and the dashboard follow `zazencodes/zazencodes-season-3/src/software-factory-claude-code`
@ `7f8a22b`. That repository has **no licence**, so nothing is copied from it: every agent and the
skill are written here in our own words.

## 1. Goal

`/factory <feature> --repo <path>` produces, without further prompts:

1. a spec with testable acceptance criteria,
2. commits on a branch `factory/<job-id>` in an Orca child worktree,
3. four independent reviews (security, code, UX, UI), with every `CHANGES` finding verified,
4. a pushed branch and an Azure DevOps PR: **draft** unless every gate in §6.3 is clean, then **ready**.

A human reviews and merges the PR. The factory never merges, never completes a PR, never pushes
anything but its own `factory/*` branch.

## 2. Decisions (user, 2026-10-16)

| Topic | Decision |
|---|---|
| P1 | Unchanged. The factory is a separate skill |
| Rework loop | None automatic. Rework only when the user runs `/factory rework` |
| Approver | Replaced by branch + PR. The human approves on the PR |
| PR host | Azure DevOps |
| On `CHANGES` | Still raise the PR, as a draft, with findings in the description |
| Builder model | Sonnet |
| Verifiers | Yes: each `CHANGES` finding is verified before it reaches the PR |
| UX/UI verification | A second browser worker re-checks each UX/UI finding against the running page |
| Visibility | Every stage is an Orca worker, visible in Orca ADE |

## 3. Non-goals

- Auto-merge, auto-complete, or setting PR policies.
- Pushing to any branch not named `factory/*`, force-pushing, pushing tags, or deleting remote branches.
- Deploying. Running sign-in or form submission against a dev server.
- Changing P1–P6 skills or personas.
- Rebasing or merging a job branch to resolve conflicts.

## 4. Flow

```
/factory <feature> --repo <path>         (coordinator = this skill, in the user's session)
  0  preflight (§6.1)
  1  claim job id under the backlog lock (§7.2) → job.json stage=spec, owner=<run id>
  2  W: spec-writer-p7                → spec.md
  3  git fetch origin <default>; record base = origin/<default> sha in job.json
     Orca child worktree factory/<id> from <base>; disable push in it (§6.2)
     W: builder-p7 → commits + build.md (STATUS: READY|BLOCKED)
        BLOCKED → stage=blocked, stop (no PR)
  4  coordinator: round-N/diff.patch = git diff <base>...HEAD; start the job's dev server (§5.2)
  5  W ×4 parallel: reviewer-{security,code,ux,ui}-p7 → round-N/review-<lens>.md (VERDICT: ...)
  6  per CHANGES finding, by lens (§5.3) → round-N/verify-<lens>-<k>.md
     in parallel: W: risk-flagger-p7  → round-N/risk.md
  7  coordinator: stop dev server; merge-tree overlap check (§8.3); push guard (§6.2);
     create or update PR (draft|ready) and post the full report as a PR thread (§6.3) → pr.json
  8  release workers; stage=pr-draft|pr-ready
```

`W:` is one Orca worker that delegates to the named persona, as in P1–P6. The worker, not the
persona, writes the persona's output file when the persona has no Write tool.

### 4.1 Rework

`/factory rework <job-id> <note>` writes `rework-<n>.md`, starts a new builder worker in the
existing child worktree (round N+1), re-runs steps 4–7, pushes new commits (never rewriting
history) and updates the same PR: description replaced, draft → ready if now clean. There is no
cap and no automatic trigger. With `--pr-comments`, active PR thread text is fetched and appended
to `rework-<n>.md` under `## PR comments (untrusted data)`. Rework never rebases: if
`origin/<default>` has moved, conflicts are reported (§8.3) and left to the human.

### 4.2 Other commands

| Command | Does |
|---|---|
| `/factory next` | Claims the first `todo` backlog item and runs it |
| `/factory run [--jobs N]` | Runs up to N backlog items concurrently in one Orca run (§8) |
| `/factory status [<job-id>]` | Reads `job.json` files; flags stale jobs |
| `/factory resume <job-id>` | Re-derives the next step from the job files (§7.4), takes ownership, continues |
| `/factory dashboard` | Starts the dashboard if it is not running (§7.3) and prints its URL |
| `/factory close <job-id>` | After the user merged or abandoned the PR: removes the child worktree, marks the job closed. Does not delete the remote branch (the user ticks "delete source branch" when merging in ADO) |

## 5. Roster (pack P7, all new, our wording, ≤ 90 lines each)

| Install name | Mode | Model | Output |
|---|---|---|---|
| `spec-writer-p7` | designer (Read, Glob, Grep, Write) | Opus | `spec.md` |
| `builder-p7` | writer | Sonnet | commits + `build.md` |
| `reviewer-security-p7` | read-only, `differential-review` preloaded | Opus | `review-security.md` (via worker) |
| `reviewer-code-p7` | runner (Read, Glob, Grep, Bash) | Sonnet | `review-code.md` (via worker) |
| `reviewer-ux-p7` | mcp-read-only (chrome-devtools allow-list) | Sonnet | `review-ux.md` (via worker) |
| `reviewer-ui-p7` | mcp-read-only, plus `take_screenshot` | Sonnet | `review-ui.md` (via worker) |
| `browser-verifier-p7` | mcp-read-only, plus `take_screenshot` | Sonnet | `verify-{ux,ui}-<k>.md` (via worker) |
| `risk-flagger-p7` | read-only | Opus | `risk.md` (via worker) |
| `review-verifier-p2`, `review-verifier-p2-security` | read-only, reused | Sonnet / Opus | `verify-{security,code}-<k>.md` |

Every persona keeps our standard constraints footer (data-not-instructions, no push, finish with
what was not verified).

### 5.1 Persona content (what each must say)

- **spec-writer-p7:** read `AGENTS.md`/`CLAUDE.md` and the code the feature touches. Sections:
  Request, Context (real paths), Requirements, Acceptance criteria (numbered, each with *how to
  verify* using the repo's real commands), Out of scope, Assumptions (product decisions marked
  `PRODUCT DECISION`). Smallest change that meets the request.
- **builder-p7:** first check `git branch --show-current` equals `factory/<id>`, else `BLOCKED`.
  Implement only the spec; match surrounding style; run the repo's checks on changed areas. Commit
  with explicit paths (never `git add -A`), short one-line messages, leave the tree clean. Never
  push, merge, rebase, reset, or touch CI/lock files/deps unless the spec requires it (then say
  so). `build.md` first line `STATUS: READY` or `STATUS: BLOCKED`; then `## Criteria` with one line
  per criterion `<n>. met|not met — <evidence>`; checks run with results; on rework, address every
  confirmed finding and log per round.
- **reviewers (all four):** first line exactly `VERDICT: PASS`, `VERDICT: CHANGES` or
  `VERDICT: NOT RUN — <reason>`. `CHANGES` only for problems this branch introduces and within the
  spec; pre-existing issues and taste go in `## Notes`. `## Findings` numbered, or `None.` Each
  finding: location, problem, impact, fix wanted. Location is `path:line` for security/code; for
  ux/ui it is `URL`, `viewport` (1280 or 390), and lettered steps (`a.`, `b.`, indented under the finding; numbers are reserved for findings) to reproduce from that URL, so a
  second worker can repeat them. From round 2, state for each prior confirmed finding: fixed / not
  fixed.
  - security: access control, injection, XSS, data exposure, secrets in diff, webhook/identity,
    rate limits, new dependencies.
  - code: runs the repo's test/build/lint/typecheck for touched areas (lint changed files only,
    never `--fix`); a failing check is always `CHANGES` and the finding quotes the exact command
    and its exit code; correctness, simplicity, reuse, conventions, docs, commit hygiene.
    `## Checks` lists each command and result.
  - ux: flow vs spec, loading/empty/error states, roles, copy, keyboard and labels, phone width.
  - ui: matches tokens/theme and neighbours, no one-off colours where a token exists, alignment,
    contrast, no overflow at 390 px; screenshots at 1280 and 390 px.
  - ux/ui return `NOT RUN` when given no URL. They never decide "no user-facing change" themselves;
    the coordinator does (§5.2).
- **browser-verifier-p7:** receives one ux/ui finding (URL, viewport, steps, claim) without the
  reviewer's reasoning, repeats the steps on the running page and tries to refute the claim. First
  line `VERIFY: CONFIRMED`, `VERIFY: FALSE POSITIVE` or `VERIFY: UNVERIFIABLE — <what is missing>`;
  evidence is the snapshot excerpt or screenshot path at the step where the claim holds or fails.
  No new findings (one `out of scope` line at most). Same no-sign-in, no-submit rule.
- **risk-flagger-p7:** reads spec, diff, reviews. Lists, with `path:line`, any of: auth/permissions,
  payments, data-security rules, deploy/CI/hosting/build scripts, schema migration, outbound email
  or production data writes, new or changed dependency, `PRODUCT DECISION` assumptions. First line
  `RISK: NONE` or `RISK: FLAGGED`.

### 5.2 How the UX/UI reviewers see the app

- **Applicability (coordinator):** if no file in `diff.patch` matches the repo's UI globs (default
  `*.html, *.css, *.scss, *.sass, *.less, *.jsx, *.tsx, *.js, *.ts, *.mjs, *.cjs, *.vue, *.svelte,
  *.cshtml, *.razor, *.erb, *.hbs, *.ejs, *.svg, templates/**, public/**, src/**/components/**`,
  overridable by `ui_globs:` in `AGENTS.md`), ux and ui are recorded `PASS — no user-facing change`
  without starting workers. Otherwise both run.
- **Dev server (coordinator):** one per job, shared by ux, ui and the browser verifiers, started by
  the coordinator in the child worktree from `AGENTS.md` `dev:` (a command containing `{port}`, also
  given as env `PORT`). Port `4100 + 10 * <job number mod 100>`, checked free first (next free
  of `+1..+9` otherwise, recorded in `job.json`). Ready when an HTTP GET to the URL answers within
  60 s. Stopped (process group) after step 6. No `dev:` command, a failed start, or no display →
  ux and ui are `NOT RUN` with the reason, which forces a draft (§6.3).
- **Browser:** each ux/ui/verifier worker is its own Claude session with its own chrome-devtools MCP
  started with an isolated temporary profile, under `xvfb-run -a` when `$DISPLAY` is unset. The
  worker grants only the allow-listed MCP tools. Probe (docs/reports/2026-10-16-probe.md): `worker-start`
  has no flag for a per-worker MCP config or env vars, so the isolated profile and `xvfb-run` must come
  from a config the worker's Claude session reads (for example a per-job `.mcp.json` in the worktree
  that runs the MCP through `xvfb-run -a`), not from `worker-start`. No sign-in, no form submission (instructed; the
  MCP cannot prevent it).

### 5.3 Verification by lens

| Lens | Who verifies one `CHANGES` finding | `CONFIRMED` when |
|---|---|---|
| security | W: `review-verifier-p2-security` (diff path, `file`, `line`, `claim`) | verifier says so |
| code, logic | W: `review-verifier-p2` | verifier says so |
| code, failing check | coordinator re-runs the quoted command in the child worktree | exit code non-zero; output tail recorded |
| ux, ui | W: `browser-verifier-p7` on the job's dev server | verifier says so |

A finding the coordinator cannot classify, or a verifier output with no recognised first line, is
`UNVERIFIABLE`. The p2 verifiers describe themselves as code-review-pack only; the task text says
"You are reused by the factory pack (P7) as its finding verifier; apply your method unchanged."

## 6. Pull request (coordinator only)

### 6.1 Preflight (before step 1, and again before push)

- Orca runtime ready; `review-verifier-p2`, `review-verifier-p2-security` and the
  `differential-review` skill installed (else stop and say which).
- `git remote get-url origin` host is `dev.azure.com` (or `<org>.visualstudio.com`) and the org is
  in `FACTORY_ADO_ORGS` (env, default `example-org`). Otherwise stop.
- Default branch: `default:` in the backlog line, else `az repos show --query defaultBranch`,
  else `origin/HEAD`; else stop.
- The user's main checkout is **not** required to be clean or on the default branch: jobs branch
  from `origin/<default>` after a fetch, and the main checkout is never modified.
- `az devops` available: the `azure-devops` extension is **not installed** on this box today.
  Installing it is a network install; the coordinator asks the user once, never installs silently.
  Auth: the user's `az login`, or `AZURE_DEVOPS_EXT_PAT` in the coordinator's environment only
  (never passed to workers). The skill never reads `~/.git-credentials` and never prints a token.
- Browser lenses: `AGENTS.md` names `dev:`; `$DISPLAY` set or `xvfb-run` on PATH. If not, the run
  continues and the affected lenses become `NOT RUN` (§5.2).

### 6.2 Push guard (`scripts/factory_push.py`, tested)

Prevention: before the builder starts, the coordinator runs `git config extensions.worktreeConfig
true` (repo, once) and `git -C <worktree> config --worktree remote.origin.pushurl
no-push://factory-disabled`, so a plain `git push` from a worker fails.

The guard refuses unless all hold:

1. branch matches `^factory/[0-9]{3,}-[a-z0-9-]+$` and is the worktree's current branch;
2. worktree clean;
3. `<base>` (from `job.json`) is an ancestor of `HEAD` and of `origin/<default>`; if the remote
   branch exists, it is an ancestor of `HEAD` (fast-forward only);
4. **every commit** in `<base>..HEAD` passes the secret scan: content rules on `+` lines of
   `git log -p --format=%H <base>..HEAD` (private-key headers, `AKIA[0-9A-Z]{16}`, `ghp_`/`github_pat_`,
   `password|secret|token|api_key` assignments to a quoted literal of 8+ chars, and any 52+-char
   base32/base62 run on a line also containing `pat`, `token` or `azure`); filename rules on every
   path touched in any commit (`.env*` except `.env.example`, `*.pem`, `*.pfx`, `*.p12`, `id_rsa*`);
5. diff `<base>..HEAD` under 5000 changed lines and 200 files;
6. trusted origin and config: `origin_url` (recorded in `job.json` by the coordinator before the
   builder starts) is https on `dev.azure.com/<org>/…` or `<org>.visualstudio.com` with the org in
   `FACTORY_ADO_ORGS`, and equals the worktree's `remote.origin.url`; repo-local and worktree git
   config contain none of `url.*`, `credential.*`, `core.{sshcommand,fsmonitor,askpass,
   bigfilethreshold,hookspath,gitproxy}`, `http.*`, `push.*`, `include*`, `filter.*`, `diff.*`,
   `remote.origin.*` (except the disabled `pushurl`, the matching `url`, and `fetch`). Checked
   first, before any command that could run a configured filter or fsmonitor. Global and system
   config are the user's and stay trusted (they hold the ADO credential helper).

The guard resolves `HEAD` and `<base>` to SHAs once and uses them throughout (no re-read between
check and push); merge commits in `<base>..HEAD` are refused (rule 3); the scan uses `--text` and
NUL-separated paths. Then `git push --porcelain --no-follow-tags --recurse-submodules=no
<origin_url> <sha>:refs/heads/<branch>` (no `-f`, no `--tags`, no `--all`, no `--delete`). Prints only the ref result; a refusal prints rule number and path, never
the matched text. Detection stays as a backstop: `git ls-remote origin` (all refs, tags included)
before step 3 and right after the push, before any PR call. `refs/pull/*` (moved by ADO itself)
is ignored, and so are peeled tag entries (`refs/tags/<t>^{}`). A new or changed ref whose new
tip commit exists in the local object store (`git cat-file -e <sha>^{commit}` in the repo) and
is not reachable from the default branch's tip as the server reported it in either snapshot
(never from `refs/remotes/*`, which a worker can rewrite) is **suspicious**: work
from this machine reached the remote outside the guard, and the job is marked `failed` with no
PR call; if the job already has a PR (a rework round), the coordinator sets it back to draft.
Every other change (teammates, other machines, deletions, branches or tags made from the default
branch, the default branch moving to the tip the coordinator fetched) is **foreign**: listed in
the PR description under *Needs your attention*, not a failure. Residual, accepted: a worker
able to push straight to the default branch on the server (normally blocked by ADO branch
policies) could make refs pointing at that commit look foreign.

### 6.3 PR

- Create: `az repos pr create --source-branch factory/<id> --target-branch <default> --draft
  <true|false> --title "<feature>" --description @pr.md` (no `--auto-complete`, no reviewers
  forced, no work-item link unless the backlog item names one). Update on rework:
  `az repos pr update --id <id> --description @pr.md --draft <bool>`.
- Draft when any of: a `CONFIRMED` or `UNVERIFIABLE` finding; a reviewer file missing, malformed
  or `NOT RUN`; any criterion `not met` in `build.md`, or a numbered acceptance criterion of
  `spec.md` that `build.md` does not report; a code review that lists no checks, or a check listed
  in it that was not run; an overlap with `origin/<default>` (§8.3). Ready otherwise. `RISK: FLAGGED` alone does
  not force draft; it heads the description.
- `pr.md` (built by `scripts/factory_pr_body.py`, tested) is **at most 4000 characters** (ADO's
  description limit): Needs your attention (risk flags, overlaps, why draft), Summary, a verdict
  table (lens → verdict with its reason, also for `PASS`, confirmed count), criteria met count, job id/round, and where the full report is (never a local
  absolute path).
  When the cap is hit, lower sections are cut and a `(truncated)` line is added.
- `report.md` (same script): the full report — criteria with evidence, confirmed findings with
  location, false positives, checks, how it was built (models), rework history. Posted as one PR
  thread via `az devops invoke --area git --resource pullRequestThreads` after create/update, and
  replaced (new thread, old one set to `closed`) on rework.
- Both texts come from agent output, which is untrusted: the script strips HTML and ADO mention
  syntax (`@<…>`, `#<id>` work-item links not from the backlog) and caps each finding at 600 characters.

## 7. State, backlog, dashboard

### 7.1 Files (outside every repo)

```
~/.orca-reports/factory/
  backlog.md  backlog.md.lock
  jobs/<NNN-slug>/
    job.json  spec.md  build.md  rework-<n>.md  pr.md  report.md  pr.json  server.log
    round-<N>/diff.patch  review-{security,code,ux,ui}.md  verify-<lens>-<k>.md  risk.md  screenshots/
```

`job.json` (only the owner writes it; whole-file rewrite via temp file + rename): `id, feature,
repo, origin_url, default_branch, base, branch, worktree, port, stage, round, owner, reviews{lens:
PASS|CHANGES|NOT RUN|MISSING}, verified{confirmed, false_positive, unverifiable}, risk, overlaps[],
pr{id, url, draft}, workers[], created, updated`. Stages: `spec, build, review, verify, pr,
pr-draft, pr-ready, blocked, failed, closed`. A write by a process whose run id differs from
`owner` is refused (`scripts/factory_job.py`).

Job id: next 3-digit number + 2–4 word slug, claimed with `mkdir` (no `-p`), retry on collision.

### 7.2 Backlog (`scripts/factory_backlog.py`, tested)

```
- [ ] ~/projects/app :: Add a 404 page for unknown routes
- [ ] ~/projects/app :: Dark footer after:003-footer-links default:main
- [~] ~/projects/app :: Reading time on posts → 004-reading-time
- [x] ~/projects/app :: Footer links → 003-footer-links pr-ready
- [!] ~/projects/app :: Pricing toggle → 005-pricing-toggle blocked
```

`claim` holds an exclusive `flock` on `backlog.md.lock` for the whole of: read, pick the first
`[ ]` line whose `after:` job (if any) has a PR, `mkdir` the job dir, rewrite the line to `[~]`.
`finish` writes the stage back from `job.json`. `stale` lists `[~]` lines whose job has no live
Orca worker and no update for 2 h; it never re-claims them (`/factory resume` does, explicitly).

### 7.3 Dashboard (`dashboard/index.html` in this repo, read-only)

One file, no dependencies. Polls `GET /api/jobs` every 3 s. Columns: Backlog, Spec, Build,
Review, Verify, PR draft, PR ready, Blocked. Card: id, feature, round, lens chips, risk badge, PR
link. Drawer: job files rendered as **escaped text** (no HTML rendering of agent output). Served
by `scripts/factory_dashboard.py`: binds `127.0.0.1` only; `/` serves the page from this repo;
`/api/jobs` returns every `job.json` as one JSON array; `/files/<job>/<path>` serves text files
under `~/.orca-reports/factory/jobs` only, as `text/plain`; GET only; resolved paths must stay
inside the jobs dir. Every response carries `X-Factory-Dashboard: 1`.

**Auto-start.** `/factory <feature>`, `next`, `run`, `rework` and `resume` call
`factory_dashboard.py ensure` before claiming work, and `/factory dashboard` calls it alone.
`ensure` probes `http://127.0.0.1:<port>/api/jobs` (default port 8787, env `FACTORY_DASHBOARD_PORT`):
if the answer carries `X-Factory-Dashboard: 1`, it reuses that server; if nothing answers, it starts
one detached (own session, pid in `dashboard.pid`, log in `dashboard.log`, both under
`~/.orca-reports/factory`) and waits up to 5 s for it to answer; if something else holds the port,
it refuses with a message naming the port and the job run continues without a dashboard. It prints
the URL in every case where a dashboard is up. The server outlives the run; `factory_dashboard.py
stop` ends it (pid file checked against the probe header before killing).

### 7.4 Resume

`/factory resume <job-id>` takes ownership (Orca takeover of the old run if it still exists, else
a new run; `owner` rewritten via `factory_job.py takeover`, which is the one write allowed to a
non-owner and is logged in `job.json`), then derives the next step from files, first match wins:
`pr.json` and stage `pr-*` → nothing to do; all round-N reviews, verifies and `risk.md` present →
step 7; reviews present → step 6 for findings without a verify file; `build.md` READY → step 4;
`spec.md` present → step 3 (reusing the worktree if it exists); else step 2.

## 8. Running several jobs at once

Each job is already isolated: its own Orca child worktree, branch `factory/<id>`, job folder,
dev server, port and browsers. Concurrency is therefore a scheduling problem, not a new model.

### 8.1 One coordinator, many jobs (default)

`/factory run [--jobs N]` (default N = 3, max 5) claims up to N backlog items and drives them in
**one** Orca run. `/factory <f1> ;; <f2> ;; <f3>` does the same for ad-hoc features. The coordinator
keeps a single foreground wait loop over all dispatches (never one background waiter per job; a
second waiter fails with `waiter_exists`). Each dispatch carries its job id, so a completion
advances only its job. Jobs move independently.

Caps, enforced by the coordinator through `scripts/factory_schedule.py` (pure function, tested):

| Cap | Default | Why |
|---|---|---|
| Concurrent jobs | 3 | Each review wave is 4 workers plus verifiers |
| Concurrent workers, all jobs | 8 | Machine load, Orca session count, API rate |
| Concurrent dev servers | 4 | Memory; one per job, shared by its browser workers |

When a cap is hit, the next step waits (FIFO across jobs by request time). When a job finishes,
the next backlog item is claimed until the backlog is empty or the user stops the run.

### 8.2 Several coordinators (also supported)

Separate sessions may each run `/factory next` or `/factory run`. Safe because: backlog claims are
serialised by `flock`; job ids are claimed by `mkdir`; only the `owner` writes `job.json`; ports
come from the job number and are checked free.

### 8.3 Conflicts between jobs

- Each job records its own `base` (the fetched `origin/<default>` sha) at step 3.
- Before each push the coordinator runs `git merge-tree --write-tree` of this branch against every
  other open `factory/*` branch and against the current `origin/<default>`. Conflicts are listed
  under *Needs your attention* ("conflicts with 004-reading-time in src/post.ts"). A conflict with
  `origin/<default>` forces draft; a conflict with another job only informs. The factory never
  rebases or merges to resolve them.
- Backlog items can be marked `after:<job-id>`; the scheduler starts them only when that job has a PR.

### 8.4 Per-worktree setup

A child worktree has no `node_modules`, venv, or build output. Options, chosen per repo in the
backlog line or `AGENTS.md`:

1. `install: <command>` (e.g. `npm ci --prefer-offline`): run once per worktree by the builder
   worker. It is a network install, so the user approves it **once per `/factory run`**, not per job.
2. No install command: the builder stops `BLOCKED` with "dependencies missing" rather than
   improvising (no symlinking the main checkout's `node_modules`, which a worker could then mutate).

### 8.5 Browser isolation

See §5.2. To verify in the first live run: two jobs' ux reviewers at the same time, under `xvfb-run`.

## 9. Visibility in Orca ADE

Each stage is an Orca worker with its own terminal; the builder's child worktree (created through
Orca, never `git worktree add`) appears in the sidebar. Personas show inside their worker's
transcript. Workers are released at the end of each run; history is in the job files and the
dashboard. `/factory close` removes the child worktree through Orca.

## 10. Repository and relationship to P1–P6

This repo (`~/projects/orca-factory`) is self-contained. It does not edit or import from
`~/projects/e2e-test-harness`.

- Own tooling, modelled on the P1–P6 scripts but written for this repo: `scripts/lint_roster.py`
  (modes, tool allow-lists, `take_screenshot` allowed for `reviewer-ui-p7` and `browser-verifier-p7`
  only), `scripts/install.py` (installs `roster/*.md` to `~/.claude/agents/`, `skills/factory/` to
  `~/.claude/skills/`, exact uninstall from its own manifest `~/.claude/.orca-factory-manifest.json`),
  `agents.lock`.
- Runtime dependencies, checked by preflight, not installed by this repo: `review-verifier-p2`,
  `review-verifier-p2-security` and the `differential-review` skill, all installed by the
  e2e-test-harness installer.
- Install names end `-p7` and never collide with P1–P6 names; the installer refuses to overwrite.

## 11. Tests and verification

- Unit (stdlib `unittest`): backlog (claim race with two processes on one `[ ]` line → exactly one
  claim; `after:`; finish; stale), job-id collision, `job.json` owner-only writes and takeover,
  resume step derivation, push guard (refuses `main`, wrong pattern, dirty tree, non-ancestor base,
  non-fast-forward, each secret rule including a secret added then removed in a later commit,
  `--force`/`--tags`/`--delete` never emitted, matched text never printed), PR body (sanitising,
  4000-char cap, draft decision table including `NOT RUN` and `not met`), verification routing,
  UI applicability globs, port choice, scheduler caps and FIFO, dashboard server (binds loopback,
  rejects `..` and symlink escapes, rejects POST, serves text as `text/plain`).
- Lint: roster lint passes; installer tests (install/uninstall exact, refuses overwrite, rollback).
- Live: a `/factory run --jobs 2` with two non-overlapping items and one overlapping item, then one
  run on a small sample app (own fixture with an `AGENTS.md` naming `dev:`) in a scratch ADO repo
  or a throwaway branch in an agreed repo; record in `docs/reports/`. Needs the user to name the
  target repo.

## 12. Risks and open items

- **Token hygiene:** the ADO token printed in an earlier session has been revoked (closed).
- **Top-level worker sessions are not tool-restricted** (as in P1–P6). Push is disabled per
  worktree (§6.2) and `ls-remote` detects a bypass, but a worker with Bash could still re-enable it.
  The residual risk is accepted and reported if detected.
- **Dev servers run builder-written code** with the user's privileges. Acceptable for the user's own
  repos; never point the factory at an untrusted repo.
- `az devops` extension install needs approval; without it, step 7 stops with the push skipped.
- Probed 2026-10-16 (docs/reports/2026-10-16-probe.md): `worker-start` takes `--model <id>` and
  `--effort <level>` (effort requires model) but no MCP or env flag. Still not checked: `az repos pr
  create --draft` and `@file` for `--description`, and the threads call through `az devops invoke`,
  because the `azure-devops` extension is not installed (install needs the user's approval).
