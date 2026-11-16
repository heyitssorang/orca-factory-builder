# Factory Integration Branch and Ship Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-job Azure DevOps PR with a local run branch that ready jobs squash-merge into, a `/factory accept` for held jobs, and a gated `/factory ship` that merges the run into `main` and pushes it after one confirmation.

**Architecture:** One new tested script, `scripts/factory_integrate.py`, owns runs, job merges, accepts and the ship steps, using git plumbing (`merge-tree`, `commit-tree`, compare-and-swap `update-ref`) so no checkout, hook or filter runs while merging. `factory_push.py` loses the `factory/*` push and gains a `ship` push mode for the default branch. Stages, backlog, PR text, dashboard and the coordinator skill follow the amendment.

**Tech Stack:** Python 3.12 stdlib, git ≥ 2.40, unittest.

**Spec:** `docs/specs/2026-10-16-factory-integration-branch-design.md` (amends `docs/specs/2026-10-16-factory-skill-design.md`; the amendment wins).

## Global Constraints

- Python stdlib only; tests `python3 -m unittest discover -s tests -t . -v`; temporary repos with a local bare `origin`; no network; never the real `~/.claude` or `~/.orca-reports` (use `FACTORY_HOME`).
- Every coordinator git call in a repo/worktree uses `factory_push._git` (its prefix carries `--no-replace-objects`, `core.hooksPath=/dev/null`, `core.fsmonitor=false`, `GIT_TERMINAL_PROMPT=0`).
- Push argv never contains `-f`, `--force*`, `--tags`, `--all`, `--mirror`, `--delete`; the only push target is `refs/heads/<default>`.
- Run id `^\d{8}-\d{4}-[0-9a-f]{4}$`; run branch `factory-run/<run-id>`; job squash message subject `<job-id>: <feature>` (sanitised, ≤ 200 chars) with trailers `Factory-Job:` and `Factory-Round:`; ship merge subject `factory: ship run <run-id>`.
- Stages: `spec, build, review, verify, held, merged, shipped, blocked, failed, closed`. `pr`, `pr-draft`, `pr-ready`, `pr.md`, `pr.json`, the 4000-character cap and every `az` call are removed.
- Only the owner writes `job.json` and the run file (temp file + `os.replace`).
- Gate commands: timeout 1800 s each, own process group killed on timeout, environment without `AZURE_DEVOPS_EXT_PAT`, `GIT_ASKPASS`, and any variable whose name contains `TOKEN` or ends `_PAT`.
- Commit messages end with a blank line + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; explicit-path commits. `SKILL.md` ≤ 500 lines; lint passes.

## Review Focus

1. A job branch whose builder changed `AGENTS.md` (e.g. `test: rm -rf ~`): the ship gate must read commands from `origin/<default>`, never from the run or job branch. Test in Task 4.
2. `main` checked out in the user's checkout with uncommitted edits to a file the run also changes: ship must stop without touching the checkout. Test in Task 4.
3. Two jobs of one run editing the same lines: the second `finish-job` holds with kind `conflict` and the run branch is unchanged. Test in Task 3.
4. A held job accepted, then reworked to a new round: the old accept must not count. Test in Task 3.
5. `/factory ship` run twice for the same run: second call refuses ("already shipped") without moving `main`. Test in Task 4.

## File Structure

```
scripts/factory_integrate.py   runs, finish-job, merge-job, accept, status, ship-* (new)
scripts/factory_push.py        remove factory/* push; compare --branch optional; ship push mode
scripts/factory_job.py         stages, new_job fields, next_step
scripts/factory_backlog.py     finish mapping, after: gate
scripts/factory_pr_body.py     report.md only
dashboard/index.html           Held/Merged/Shipped columns, run id, run filter
skills/factory/SKILL.md        §8 replaced, accept/ship commands, az removed
skills/factory/references/worker-tasks.md   PR wording removed
README.md                      status, live-run prerequisites
tests/test_integrate.py        new; other test files updated in their tasks
```

---

### Task 1: Push guard: drop job pushes, optional compare branches, ship mode

**Files:**
- Modify: `scripts/factory_push.py`, `tests/test_push.py`

**Interfaces:**
- Produces:
  - Removed: CLI `push`, `_cmd_push`, `push_argv`. Kept: `check()`, `_check()`, `scan_commits()`, `config_check()`, `config_violations()`, `remote_refs()`, `classify_changes()`, `has_local_commit()`, `reachable_from()`, `validate_origin()`, `_git()`, `SHA_RE`.
  - `compare`: `--branch` optional (`action="append"`, default `[]`).
  - `ship_argv(origin_url: str, default_branch: str, sha: str) -> list[str]` = `["git", "push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", origin_url, f"{sha}:refs/heads/{default_branch}"]`
  - `ship_check(repo: Path, *, default_branch: str, sha: str, base: str, origin_url: str) -> list[str]` — refusal lines: `rule 6: …` (config check on `repo`, first); `ship: sha is not a commit`; `ship: local <D> is not <sha>`; `ship: origin/<D> is not an ancestor of <sha>` (non-fast-forward, using `base` resolved to a commit and compared with `refs/remotes/origin/<D>`); `ship: base is not origin/<D>`; rule 4 lines for `<base>..<sha>` (`scan_commits(repo, base, sha)`).
  - CLI `ship --repo R --default D --sha S --base B --origin-url U`: `validate_origin` first (refuse `rule 6: origin url not allowed`), then `ship_check`; exit 2 with lines on refusal; else push with `ship_argv` from `repo` (env as the old push: `GIT_TERMINAL_PROMPT=0`, `GIT_NO_REPLACE_OBJECTS=1`); exit 1 `push rejected` on non-zero git exit; exit 0 printing only the `refs/heads/<D>` porcelain line.

- [ ] **Step 1: Failing tests** in `tests/test_push.py`: delete tests that only exercise the removed `push` CLI/`push_argv` (keep every `check`/scan/config test). Add:

```python
def test_compare_without_branch(self):  # no --branch: own factory branch change is "foreign"/"suspicious" per class, CLI exit codes as before
def test_ship_argv_safe(self):
    argv = fp.ship_argv("https://x", "main", "a" * 40)
    self.assertEqual(argv[-1], "a" * 40 + ":refs/heads/main")
    for bad in ("-f", "--force", "--force-with-lease", "--tags", "--all", "--mirror", "--delete"):
        self.assertNotIn(bad, argv)
def test_ship_pushes_fast_forward(self):  # local main advanced by one clean commit → exit 0, origin main == sha
def test_ship_refuses_non_fast_forward(self):  # origin main moved elsewhere (second clone pushes), fetched → exit 2 "not an ancestor"
def test_ship_refuses_local_main_mismatch(self):  # --sha differs from local main → exit 2
def test_ship_refuses_secret(self):  # AKIA… in base..sha → exit 2 "rule 4"
def test_ship_refuses_bad_config(self):  # repo-local credential.helper → exit 2 "rule 6" and no push
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_push -v`; new tests fail.
- [ ] **Step 3:** Implement the interfaces above.
- [ ] **Step 4:** Run the full suite; expect PASS.
- [ ] **Step 5:** Commit `feat: push guard ship mode, drop factory branch push`.

---

### Task 2: Stages, job fields, next_step and backlog

**Files:**
- Modify: `scripts/factory_job.py`, `scripts/factory_backlog.py`, `tests/test_job.py`, `tests/test_backlog.py`

**Interfaces:**
- Produces:
  - `factory_job.STAGES = ("spec","build","review","verify","held","merged","shipped","blocked","failed","closed")`
  - `factory_job.DONE_STAGES = ("held","merged","shipped","closed","blocked","failed")` — `next_step` returns `"none"` for these; the `"push"` step name becomes `"finish"` (all round files present → §5 of the amendment). `pr.json` is no longer consulted.
  - `new_job(...)`: drops `pr`; adds `run=None`, `hold=None`, `accepted=None`, `merge_commit=None`; keyword `run: str | None = None`.
  - `factory_backlog`: `MERGED_STAGES = ("merged","shipped","closed")`; `after:` is satisfied only by `merged`/`shipped`; `finish`: `merged|shipped|closed` → `[x] … → id <stage>`, `held|blocked|failed` → `[!] … → id <stage>`; `claim(..., run=None)` passes `run` to `new_job`; CLI `claim --run R`.

- [ ] **Step 1: Failing tests:** `next_step` returns `"finish"` when all round files exist and stage is `verify`; `"none"` for each DONE stage; `new_job` has `run`/`hold`/`accepted`/`merge_commit` and no `pr`; backlog `after:` waits on `held` and passes on `merged`; `finish` maps `held` → `[!]` and `shipped` → `[x]`; `claim --run 20261016-1530-ab12` stores `run`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_job tests.test_backlog -v`; new tests fail.
- [ ] **Step 3:** Implement; update existing tests that assert `pr-draft`/`pr-ready`/`push` to the new names.
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5:** Commit `feat: run-branch job stages and backlog mapping`.

---

### Task 3: factory_integrate: runs, finish-job, merge-job, accept, status

**Files:**
- Create: `scripts/factory_integrate.py`, `tests/test_integrate.py`

**Interfaces:**
- Consumes: `factory_job` (`home`, `jobs_dir`, `read_job`, `update_job`, `OwnerMismatch`, `JOB_ID_RE`), `factory_review` (`load_round`, `draft_reasons`), `factory_push` (`_git`, `check`, `remote_refs`, `classify_changes`, `has_local_commit`, `reachable_from`, `SHA_RE`), `factory_pr_body.sanitize`.
- Produces:
  - `RUN_ID_RE`; `runs_dir(home) -> home/"runs"`; `new_run_id(now: datetime, rand4: str) -> str`; `claim_run(home: Path, now=None) -> str` (`mkdir runs/<id>.lock`, retry with new random suffix up to 20).
  - `load_run(home, run_id) -> dict`; `save_run(home, run_id, data: dict, *, owner: str) -> None` (refuses `OwnerMismatch` if the file's `owner` differs; preserves unknown keys such as the scheduler's `caps/pending/running`).
  - `start_run(home, run_id, *, repo: Path, default_branch: str, origin_url: str, owner: str) -> dict` — spec §4.1 step 3; refs snapshot saved as `runs/<run-id>.refs-before.json`.
  - `@dataclass MergeResult: status: str ("merged"|"held"|"conflict"|"refused"|"error"), commit: str | None, paths: list[str], reasons: list[str]` (`merge_job` never returns `"held"`; `finish_job` returns `"held"` for a draft hold and passes the other statuses through)
  - `merge_job(home, run_id, job_id, *, owner) -> MergeResult` — spec §6 steps 1–7 (rule checks via `factory_push.check(worktree, branch=f"factory/{job_id}", base=job["base"], default_branch=…, origin_url=…)`; backstop via fresh `remote_refs` + `classify_changes` with no expected branches and the default-tip anchors).
  - `finish_job(home, run_id, job_id, *, owner) -> MergeResult` — spec §5: compute `draft_reasons(load_round(job_dir, round), overlaps_default)`; reasons and no current-round `accepted` → set stage `held`, `hold={"kind":"draft","round":N,"reasons":[…]}`, add to run `held[]`, return status `"held"`; else `merge_job`; `merged` → stage `merged`, `merge_commit`, `hold=None`; `conflict` → `hold={"kind":"conflict",…,"paths":[…]}`; `refused` → `hold={"kind":"refused",…}`; `error` → stage `failed`.
  - `accept(home, job_id, *, owner, by: str, note: str) -> dict` — requires stage `held`, `hold.kind == "draft"`, `hold.round == job.round`, and the job's run not shipped; records `accepted={"by","at","round","note","reasons"}`; returns the job. Then the caller runs `finish_job`.
  - `reassign(home, job_id, run_id, *, owner) -> dict` — amendment §9: job stage in `held|blocked|failed`, its current run shipped (or the caller passes `--force-move` for an unshipped run), target run unshipped, same `repo`; moves the job id from the old run's `held[]` to nothing, sets `job.run`, clears `hold`/`accepted`. CLI `reassign --job --run --owner`. Test `test_rework_moves_held_job_after_ship`.
  - `run_status(home, run_id) -> dict` (`merged`, `held`, `shipped`, `ahead` = commits on the run branch over `base`).
  - CLI (exit 0 ok / 2 refused / 3 conflict / 1 error; JSON on stdout): `new-run`, `start-run --repo --run --default --origin-url --owner`, `finish-job --run --job --owner`, `merge-job --run --job --owner`, `accept --job --owner --by --note`, `status --run`.

- [ ] **Step 1: Failing tests** (temp repo + bare origin + a job dir built by helper: branch `factory/001-a` with commits from `origin/main`, round-1 files written in the parser formats, `job.json` with `base`, `run`, `owner`):

```python
def test_start_run_creates_branch_at_origin_main(self): ...
def test_start_run_refuses_existing_branch(self): ...
def test_ready_job_lands_as_one_commit(self):
    r = fi.finish_job(home, run, "001-a", owner="o")
    self.assertEqual(r.status, "merged")
    log = git(repo, "log", "--format=%s%n%b", f"{base}..factory-run/{run}").stdout
    self.assertTrue(log.startswith("001-a: Add a thing"))
    self.assertIn("Factory-Job: 001-a", log); self.assertIn("Factory-Round: 1", log)
    self.assertEqual(git(repo, "rev-list", "--count", f"{base}..factory-run/{run}").stdout.strip(), "1")
def test_merge_touches_no_checkout_and_runs_no_hook(self): # main checkout status unchanged; a post-commit/pre-commit hook marker never created
def test_draft_job_is_held(self): # a CONFIRMED verify → status "held", hold.kind "draft", run held[] has it
def test_accept_then_merge(self): ...
def test_accept_from_old_round_does_not_count(self): # Review Focus 4
def test_accept_refused_for_conflict_and_refused_holds(self): ...
def test_secret_refused_even_when_accepted(self): # hold.kind "refused", run branch unchanged
def test_conflict_holds_and_leaves_run_branch(self): # Review Focus 3: two jobs edit the same line
def test_cas_failure_is_error(self): # move run branch between tree build and update-ref (patch fi._update_ref target) → "error", stage failed
def test_feature_text_sanitised_in_commit(self): # feature "<b>x</b> @alice #12" → no "<", no "@alice", no "#12"
def test_owner_enforced_on_run_file(self): ...
def test_cli_exit_codes(self): # 0 merged, 2 refused, 3 conflict
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_integrate -v`; expect import failure.
- [ ] **Step 3:** Implement. Merge with `git merge-tree --write-tree --merge-base <job base> <run tip> factory/<id>` (exit 1 → conflict, paths from the `--name-only` conflicted list), `git commit-tree <tree> -p <run tip> -F <tmpfile>`, `git update-ref refs/heads/factory-run/<id> <new> <old>`; all via `factory_push._git`.
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5:** Commit `feat: run branches, squash merges and accept`.

---

### Task 4: factory_integrate: ship

**Files:**
- Modify: `scripts/factory_integrate.py`, `tests/test_integrate.py`

**Interfaces:**
- Consumes: Task 1 `factory_push.ship` CLI (the coordinator calls it; tests call `ship_check` + CLI), Task 3 run file helpers.
- Produces:
  - `read_agents_commands(repo: Path, ref: str) -> dict[str, str]` — parses `install:`, `test:`, `build:` lines from `git show <ref>:AGENTS.md` (missing file → `{}`).
  - `ship_prepare(home, run_id, *, owner) -> dict` — spec §8 steps 1–2; refuses (`ShipRefused(reason)`) when no merged jobs, already shipped (Review Focus 5), suspicious refs, rule 6, or conflict (reason lists paths and the merged jobs touching them); records `ship={"merge_sha","base"}` in the run file.
  - `ship_gate(home, run_id, *, allow_install: bool) -> dict` — spec §8 step 3; commands from `origin/<D>` via `read_agents_commands` (Review Focus 1); returns `{"install"|"test"|"build": {"exit": int|None, "tail": str, "status": "pass"|"fail"|"not run"|"timeout"}}` and `scan: []|[lines]`; any fail/timeout/scan line → `ShipRefused`. Temporary worktree in `tempfile.mkdtemp()` removed in `finally` (`git worktree remove --force` + `prune`).
  - `ship_local(home, run_id, *, owner) -> str` — spec §8 step 5; returns `"ff-checkout:<path>"` or `"update-ref"`; refuses on divergence or an ff-only merge failure (Review Focus 2).
  - `ship_finish(home, run_id, *, owner, pushed: bool) -> dict` — spec §8 step 8 (job stages `shipped`, backlog `finish`, `git branch -d factory-run/<id>`, run `shipped={"merge_sha","pushed","at"}`); worktree removal is left to the coordinator (Orca).
  - CLI: `ship-prepare --run --owner`, `ship-gate --run [--allow-install]`, `ship-local --run --owner`, `ship-finish --run --owner --pushed yes|no`; `ShipRefused` → exit 2 with the reason.
  - Constant `GATE_TIMEOUT = 1800`; env scrub as Global Constraints (reuse `factory_review`'s scrub helper if it exposes one; else a local `_scrubbed_env()`).

- [ ] **Step 1: Failing tests:**

```python
def test_ship_refuses_without_merged_jobs(self): ...
def test_ship_conflict_with_main_changes_nothing(self): # origin main edits the same line after run start
def test_gate_reads_agents_from_default_not_run(self): # Review Focus 1: run commit changes AGENTS.md test: to touch a marker; origin main's test: passes; marker never created
def test_gate_failing_test_stops(self): # test: python3 -c "import sys; sys.exit(1)"
def test_gate_timeout_kills_group(self): # patch GATE_TIMEOUT=2; test: sh -c 'sleep 30 & sleep 30'; no sleep survives
def test_gate_env_scrubbed(self): # AZURE_DEVOPS_EXT_PAT and FOO_TOKEN absent in a child that writes os.environ keys to a file
def test_gate_secret_in_run_stops(self): ...
def test_ship_local_updates_unchecked_out_main_by_cas(self): ...
def test_ship_local_ff_in_checkout(self): # main checked out, clean → working tree has the run's files
def test_ship_local_refuses_dirty_overlap(self): # Review Focus 2: uncommitted edit to a file the run changes → refused, file content unchanged
def test_ship_local_refuses_diverged_main(self): ...
def test_ship_twice_refused(self): # Review Focus 5
def test_ship_finish_marks_jobs_and_deletes_run_branch(self): ...
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_integrate -v`; new tests fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5:** Commit `feat: gated ship of a run into the default branch`.

---

### Task 5: Job report only (PR text removed)

**Files:**
- Modify: `scripts/factory_pr_body.py`, `tests/test_pr_body.py`

**Interfaces:**
- Produces: `build J N` writes `J/report.md` only and prints `{draft, reasons}` (unchanged JSON). Removed: `pr_body`, `_fit`, `CAP`, `SUFFIX`, `_foreign_refs` use in `pr.md`. Kept: `sanitize`, `_cut`, `_clean`, `report_body` (gains a "Needs your attention" section at the top with draft reasons, risk lines, overlaps and foreign refs, moved from `pr_body`; no length cap besides `FINDING_CAP`).

- [ ] **Step 1: Failing tests:** `build` writes `report.md` and no `pr.md`; `report.md` starts with "Needs your attention" holding a CONFIRMED finding's reason and a `RISK: FLAGGED` line; no `/home/` path in it. Delete tests that only cover `pr_body`/cap/truncation; keep sanitize tests.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_pr_body -v`; fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5:** Commit `refactor: job report replaces PR description`.

---

### Task 6: Dashboard columns and run filter

**Files:**
- Modify: `dashboard/index.html`, `scripts/factory_dashboard.py`, `tests/test_dashboard.py`

**Interfaces:**
- Produces: columns `backlog, spec, build, review, verify, held, merged, shipped, blocked` (labels Backlog, Spec, Build, Review, Verify, Held, Merged, Shipped, Blocked; `failed` → Blocked); card shows `job.run` when set and `hold.kind` for held cards; a `<select>` run filter populated from `/api/jobs` `runs` (new key: list of run files' `{id, branch, shipped}` read with the same safe-read helper, dot-entries and `.lock` dirs skipped); `pr` link and "PR pending" removed.

- [ ] **Step 1: Failing tests:** `/api/jobs` includes `runs` from `runs/*.json` (a symlinked run file skipped); page source has no `pr-draft`, `pr-ready`, `PR pending`; contains `"held"`, `"merged"`, `"shipped"`; still no `innerHTML`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_dashboard -v`; fail.
- [ ] **Step 3:** Implement; screenshot at 1280 and 390 px with a temp home holding one job per new stage (headless Chrome as before) into the scratchpad; fix overflow.
- [ ] **Step 4:** Full suite PASS.
- [ ] **Step 5:** Commit `feat: dashboard shows held, merged and shipped jobs per run`.

---

### Task 7: Coordinator skill, README, reinstall

**Files:**
- Modify: `skills/factory/SKILL.md`, `skills/factory/references/worker-tasks.md`, `tests/test_lint_roster.py`, `README.md`

**Interfaces:**
- Consumes: every CLI above (exact verbs/flags from each `main()`).
- Produces: SKILL following the amendment: §1 command table adds `/factory accept <job-id> [<note>]` and `/factory ship [<run-id>]`; §2 preflight drops ADO/`az`/`FACTORY_ADO_ORGS` items and adds git ≥ 2.40; run start via `factory_integrate.py new-run` + `start-run` (and `factory_schedule.py init` for the same run file path); per-job end via `factory_pr_body.py build` then `factory_integrate.py finish-job`; §8 (push/PR) replaced by "End of a job" and a new "Ship" section that runs `ship-prepare`, `ship-gate` (asks once to allow `install:` if present), shows the summary, `ship-local`, asks "Push <D> to origin?", runs `factory_push.py ship`, then `ship-finish` and Orca worktree removal; rework rules per amendment §7/§9; rework of a job whose run shipped calls `factory_integrate.py reassign` to the newest unshipped run of the repo (or a new run); `/factory close` deletes the local `factory/<id>` branch (`git branch -D`, via `factory_push._git` hygiene) only when the job is `shipped`; every `az`, PR, `pr.md`, `pr.json`, `--pr-comments` mention removed.
- `test_skill_clean`: command list adds `/factory accept`, `/factory ship`; required strings become `factory_integrate.py finish-job`, `factory_integrate.py ship-gate`, `factory_push.py ship`, `factory_review.py decide`, `factory_pr_body.py build`, `factory_dashboard.py ensure`, `xvfb-run`, `takeover`, `factory_push.py config-check`, `factory_review.py rerun`, `can-claim`, `enabledMcpjsonServers`, `chrome-devtools-mcp@1.10.1`, `core.hooksPath=/dev/null`; and asserts the text contains none of `az repos`, `pr.json`, `--pr-comments`, `factory_push.py push `.

- [ ] **Step 1:** Update `test_skill_clean` as above; run; fails.
- [ ] **Step 2:** Rewrite the affected SKILL sections and worker-tasks wording.
- [ ] **Step 3:** `python3 -m unittest tests.test_lint_roster -v` and `python3 scripts/lint_roster.py` PASS; SKILL ≤ 500 lines.
- [ ] **Step 4:** README: status line; "Before the first live run": keep "rotate the exposed ADO token" (ship pushes with the user's git credentials), drop the `az` extension item, keep the repo-local config decision, add "a scratch repo whose `main` you are happy to have pushed"; Next steps lists the revised live run (amendment §11).
- [ ] **Step 5:** Full suite PASS; commit `feat: coordinator delivers through run branches and /factory ship`; then `python3 scripts/install.py uninstall && python3 scripts/install.py install && python3 scripts/lint_roster.py --installed` (the user approved the earlier install; this refreshes it).

---

### Task 8: Live run (gated on the user)

Not executed without the user: needs a scratch repo with a remote whose `main` may be pushed. Steps as amendment §11 "Live", recorded in `docs/reports/`.
