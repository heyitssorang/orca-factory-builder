# Design review (Opus) — Factory skill (P7) spec

Date: 2026-10-16. Reviewed: `docs/specs/2026-10-16-factory-skill-design.md` (304 lines).
Checked against: installed `review-verifier-p2*` personas, the e2e-test-harness pack verification
record (`docs/orchestrator/reports/pack-verification-2026-10-15.md`), the Orca orchestration guide
served by the installed binary, and `fixtures/sample-app`.

Verdict: **sound shape, not ready to plan.** The flow, isolation model and "human merges" boundary
are right. Five issues (H1–H5) would make PRs wrong or unsafe and need a spec decision first. The
rest (M, L) can be settled in the plan.

## High — fix in the spec before planning

**H1. UX/UI can fail open to a ready PR.** §12: no dev command → `VERDICT: PASS` with a note. A PR
can then be marked ready with no UX/UI review. `fixtures/sample-app` (the planned live target) has
no `AGENTS.md`, so this is the case on the first live run. Separately, the verification record says
the chrome-devtools MCP starts a headful browser and fails on this machine without `xvfb-run`, and
headless MCP calls need explicit grants; §8.5 does not cover either.
*Fix:* add a third verdict `VERDICT: NOT RUN` (reason required). It counts like `MISSING` in the
draft table (§6.3). Preflight checks for a dev command and a display (or starts the MCP under
`xvfb-run`). `PASS` stays reserved for "looked and found nothing" or "no user-facing/visual change"
(the latter decided from the diff, by the coordinator, not by the persona).

**H2. The reused verifiers cannot verify half the findings.** `review-verifier-p2` is Read/Glob/Grep
and takes `file`, `line`, `claim` and a diff path. That works for security and code-logic findings.
It cannot check (a) a failing test/lint/build (the code lens makes these always `CHANGES`), which
needs execution, or (b) UX/UI findings, which cite a page and a screenshot, not a line. Both will
come back `UNVERIFIABLE`, which §6.3 turns into a draft, so any UX/UI or check finding forces a
draft whether it is real or not. The spec also never says who produces the diff the verifier reads.
*Fix:* verification by lens:
- failing checks: the coordinator (or a plain runner worker) re-runs the exact command in the child
  worktree; a non-zero exit is `CONFIRMED`, recorded verbatim;
- security/code logic: `review-verifier-p2` / `-p2-security` as planned;
- UX/UI: a second browser worker re-checks one finding against the running page (same allow-list),
  or the spec accepts these as `UNVERIFIED` and lists them as such in the PR without forcing a draft.
  This is a user decision.
The coordinator writes `round-N/diff.patch` (`git diff <base>...HEAD`) and records `<base>` in `job.json`.

**H3. The secret scan checks the wrong thing.** §6.2 scans "files in the diff". A push sends every
commit, so a secret added in commit 2 and deleted in commit 3 leaves no trace in the net diff but is
pushed and stays in the ADO history. "Matches the secret patterns" also mixes filename rules
(`.env*`, `*.pem`) with content rules (`AKIA…`).
*Fix:* scan the added lines of **each** commit in `<base>..HEAD` (`git log -p --format= <base>..HEAD`,
`+` lines only) for content patterns, and every path touched in any commit for filename patterns.
Replace "every commit in `origin/<default>..HEAD` is new on this branch" (not testable as written)
with: `<base>` is an ancestor of `HEAD` and of `origin/<default>`, and the remote branch, if any, is
an ancestor of `HEAD` (fast-forward only).

**H4. Workers can push, and the mitigation only detects it afterwards.** §12 already notes top-level
workers are not tool-restricted. The builder has Bash in a worktree whose `origin` works with the
user's credential helper; `ls-remote` before/after catches a stray push only once it has happened,
and only if the coordinator compares every ref (tags included).
*Fix (prevention, cheap):* enable `extensions.worktreeConfig` and set
`git config --worktree remote.origin.pushurl no-push://disabled` in each child worktree; the push
guard pushes by explicit URL. Keep `ls-remote` (all refs) as the detector. Do not export
`AZURE_DEVOPS_EXT_PAT` into worker environments.

**H5. Backlog claim can start the same item twice.** §7.2: `claim` picks the first `[ ]`, creates
the job dir, *then* takes the `flock` to rewrite the line. Two coordinators can both read the same
`[ ]` line, each `mkdir` a different job id, and both run it.
*Fix:* hold the `flock` across read → select → `mkdir` → rewrite. Job-id `mkdir` stays as a second
guard. Add this exact race to the §11 test.

## Medium — settle in the plan

- **M1. ADO description limit.** ADO PR descriptions are capped at 4000 characters. §6.3 puts
  criteria, all reviews, false positives, checks and rework history in it. Keep the description to
  attention items, summary and a verdict table; post the full report as a PR comment thread
  (`az repos pr` has no comment command; use `az devops invoke` on the threads API) or link the
  dashboard. `factory_pr_body.py` must enforce the cap with a test.
- **M2. No recovery when the coordinator dies.** A multi-job run is one long Claude session; it will
  compact or end. §8.2 makes the Orca run the only writer of `job.json`, and `stale` never re-claims,
  so a crashed run strands its jobs. Add `/factory resume <job-id>`: re-derive the next step from
  the files on disk (each stage's output is already a file), take ownership with Orca's takeover
  path, continue. Add `owner` to the §7.1 field list (it is only mentioned in §8.2).
- **M3. Dev-server contradictions.** §5.2 gives each lens its own port and server; §8.1 says ux and
  ui share one server per job. Pick one: one server per job, started by the coordinator, shared by
  both lenses, port `4100 + 10*job`. Say how the port reaches the app (a `PORT` env convention or a
  `{port}` placeholder in the dev command), and check the port is free first.
- **M4. Draft table gaps.** Also force draft when `build.md` has any criterion "not met", when the
  code lens did not run a check it listed, or when the merge-tree check (§8.3) finds an overlap with
  `origin/<default>` (a PR that cannot merge should not be ready).
- **M5. Preflight is too strict for parallel use.** "Main checkout clean on default branch" blocks
  the user from working while the factory runs, and is not needed: jobs branch from a ref. Instead:
  `git fetch origin <default>` once per claim and branch from `origin/<default>`. §8.3's "all jobs
  branch from the same head" contradicts "recorded at claim time"; keep the per-claim record.
- **M6. Verifier persona scope.** Both p2 verifiers say "Use only inside the code-review pack". Reuse
  is fine, but the task text must say so, or the persona may refuse; better to note this in the
  e2e-test-harness repo later than to fork them here.

## Low

- L1. Risk flagger (step 6) needs only spec, diff and reviews; it can run while verifiers run.
- L2. The dashboard page reads `jobs/*/job.json`, but the server serves files only: add one JSON
  index endpoint, and say where `index.html` is served from (it lives in this repo, not under
  `~/.orca-reports/factory`).
- L3. Rework ingests only the user's note. Optionally pull PR comment threads as untrusted input.
  Say that rework never rebases, so conflicts with a moved `<default>` grow and are left to the human.
- L4. `/factory close` cannot delete the remote `factory/*` branch (a delete is a push). Say so, or
  allow a guarded delete of the job's own branch after merge.
- L5. Default branch: `origin/HEAD` is often unset in fresh clones; read it from
  `az repos show` or require it in the backlog line.
- L6. ADO PAT pattern: PATs are plain base32/base62 strings with no prefix; a shape regex will
  either miss them or flag many hashes. Prefer entropy plus a context keyword, and test both ways.

## Not reviewed / not verified

- Did not run `az repos pr create` or check the installed `az` version's flags (`--draft`,
  `@file` for `--description`); the extension is not installed here.
- Did not confirm Orca accepts a per-worker model choice in `worker-start` beyond the guide naming
  a "launch model/effort" reference.
- The 4000-character ADO limit and the absence of a PR comment command in `az repos pr` are from
  documentation knowledge, not checked against this org or CLI version.
