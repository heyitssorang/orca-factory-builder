# Factory Skill (P7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `factory` skill: tested Python scripts for state, backlog, push guard, verdicts, PR text, scheduling and dashboard; seven P7 personas with a roster lint; the coordinator `SKILL.md`; an installer; then one recorded live run.

**Architecture:** The coordinator is a Claude Code skill that drives Orca workers and calls small stdlib-Python CLIs for every decision that must be deterministic (claims, ownership, push refusal, draft decision, PR text, caps). Agent output only reaches git, ADO or the dashboard through those scripts. State lives as files under `~/.orca-reports/factory`.

**Tech Stack:** Python 3.12 stdlib only (`unittest`, `fcntl`, `http.server`, `subprocess`), git ≥ 2.38 (`merge-tree --write-tree`), Orca CLI, Azure CLI + `azure-devops` extension, chrome-devtools MCP under `xvfb-run`, one plain HTML/JS page.

**Spec:** `docs/specs/2026-10-16-factory-skill-design.md` (revised after `docs/reviews/2026-10-16-design-review-opus.md`).

## Global Constraints

- Python stdlib only; no pip installs. Tests: `python3 -m unittest discover -s tests -t . -v` from the repo root; no network, no real `~/.claude` or `~/.orca-reports` (use temp dirs).
- Every script reads its home from `FACTORY_HOME` (default `~/.orca-reports/factory`); the installer takes `--claude-home` (default `~/.claude`).
- Scripts are run as `python3 <path>/factory_x.py` (not `-I`: they import siblings from their own directory). Tests add `scripts/` to `sys.path`.
- Scripts never print tokens or matched secret text; refusals name a rule number and a path.
- Install names end `-p7`; models `claude-opus-5-5` (spec-writer, reviewer-security, risk-flagger) and `claude-sonnet-5-5` (others). Personas ≤ 90 lines; `SKILL.md` ≤ 500 lines.
- Branch pattern `^factory/[0-9]{3}-[a-z0-9-]+$`. Push: no `-f`, `--force*`, `--tags`, `--all`, `--mirror`, `--delete`.
- PR description ≤ 4000 characters; each finding ≤ 600 characters in PR text.
- Port base `4100 + 10 * (job_number % 100)`. Caps: jobs 3 (max 5), workers 8, dev servers 4.
- Nothing copied from `zazencodes/...`; all text is ours.
- Commit after each task; message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Feature text with no ASCII letters (e.g. `"Ärger über €"`, `"!!!"`) → slug falls back to `job`, id still valid. Test in Task 2.
2. Backlog repo path containing spaces, or feature text containing `::` or `→` → parsed correctly, only a trailing `→ NNN-slug [stage]` is a job marker. Tests in Task 3.
3. Reviewer/verifier file with a UTF-8 BOM, CRLF line endings or leading blank lines → verdict read from the first non-blank line, not `MALFORMED`. Test in Task 5.
4. A binary secret file (`key.pem`, `cert.pfx`) committed then deleted → refused by the filename rule even though `git log -p` shows no text. Test in Task 4.
5. A symlink inside a job folder pointing outside `jobs/` → dashboard returns 404. Test in Task 8.

## File Structure

```
scripts/factory_job.py        job ids, job.json read/write (owner-only), takeover, resume step
scripts/factory_backlog.py    backlog parse, claim (flock), add, finish, stale
scripts/factory_push.py       disable-push, secret scan, guard, push, ls-remote compare
scripts/factory_review.py     parse build/review/verify files, routing, UI applicability, draft decision
scripts/factory_pr_body.py    sanitise, pr.md (≤4000), report.md
scripts/factory_schedule.py   port choice, cap/FIFO admission over a run file
scripts/factory_dashboard.py  loopback read-only server
dashboard/index.html          kanban page
roster/*-p7.md                seven personas
agents.lock                   roster lock
scripts/lint_roster.py        roster + skill lint
skills/factory/SKILL.md       coordinator
scripts/install.py            install/uninstall with manifest
fixtures/sample-app/          tiny no-dependency web app with AGENTS.md
tests/test_*.py               one file per script
docs/reports/                 probe and live-run records
```

---

### Task 1: Probe the unverified external interfaces

No code. Records facts later tasks rely on (spec §12, last bullet).

**Files:**
- Create: `docs/reports/2026-10-16-probe.md`

**Interfaces:**
- Produces: the exact `worker-start` flag for model choice (or "none"), whether `az repos pr create` accepts `--draft` and `--description @file`, the `az devops invoke` arguments for PR threads, and `git --version`.

- [ ] **Step 1:** Resolve the Orca CLI as the `orchestration` skill says; run `<ORCA> orchestration worker-start --help` and `<ORCA> skills get orchestration --reference references/coordinator-loop.md`; note the model/effort flags and how a worker's MCP server and env are set.
- [ ] **Step 2:** `git --version` (need ≥ 2.38). `az version`; if the `azure-devops` extension is missing, **ask the user** before `az extension add --name azure-devops`. If declined, record the ADO items as `NOT CHECKED` and continue.
- [ ] **Step 3:** With the extension: `az repos pr create --help`, `az repos pr update --help`, `az devops invoke --help`; record the flags. Do not create anything.
- [ ] **Step 4:** Write the findings to the report (command, observed output line, conclusion). If a spec assumption is wrong, edit the spec section it affects in the same commit.
- [ ] **Step 5:** Commit `docs: record probe of orca, az and git interfaces`.

---

### Task 2: Job state (`factory_job.py`)

**Files:**
- Create: `scripts/factory_job.py`, `tests/test_job.py`

**Interfaces:**
- Produces:
  - `home() -> Path` (from `FACTORY_HOME`); `jobs_dir(home: Path) -> Path` = `home/"jobs"`
  - `slugify(feature: str) -> str` — lowercase ASCII words `[a-z0-9]+`, first 4 words joined by `-`, at most 40 chars, `"job"` if empty
  - `claim_job_dir(jobs: Path, feature: str) -> tuple[str, Path]` — id `f"{n:03d}-{slug}"`, n = 1 + highest existing number (1 if none); `mkdir` without parents/exist_ok; on `FileExistsError` recompute and retry up to 20 times, then raise `RuntimeError`
  - `job_number(job_id: str) -> int`
  - `new_job(job_dir: Path, *, feature: str, repo: str, owner: str, default_branch: str | None = None) -> dict` — all §7.1 fields present; `stage="spec"`, `round=0`, `reviews={}`, `verified={"confirmed":0,"false_positive":0,"unverifiable":0}`, `overlaps=[]`, `pr=None`, `workers=[]`, `takeovers=[]`, ISO-8601 UTC `created`/`updated`
  - `read_job(job_dir: Path) -> dict`
  - `class OwnerMismatch(Exception)`
  - `update_job(job_dir: Path, patch: dict, *, owner: str) -> dict` — shallow merge, refuses with `OwnerMismatch` if on-disk `owner != owner`, sets `updated`, writes temp file in the same dir then `os.replace`
  - `takeover(job_dir: Path, new_owner: str, *, reason: str) -> dict` — appends `{"from","to","at","reason"}` to `takeovers`, sets `owner`
  - `next_step(job_dir: Path) -> str` — one of `"none"`, `"push"`, `"verify"`, `"review"`, `"build"`, `"spec"` per spec §7.4 (uses `factory_review.parse_review` / `parse_build` from Task 5 — import lazily inside the function so Task 2 tests can stub files written in the agreed formats)
  - CLI: `claim --feature F --repo R --owner O [--default B]` (prints id), `show <id>`, `update <id> --owner O --patch JSON`, `takeover <id> --owner O --reason R`, `next <id>`

- [ ] **Step 1: Write failing tests** in `tests/test_job.py` (temp `FACTORY_HOME`):

```python
def test_slugify(self):
    self.assertEqual(fj.slugify("Add a 404 page for unknown routes"), "add-a-404-page")
    self.assertEqual(fj.slugify("Ärger über €"), "rger-ber")
    self.assertEqual(fj.slugify("!!!"), "job")
def test_claim_sequence_and_collision(self):
    a, _ = fj.claim_job_dir(self.jobs, "Footer links"); self.assertEqual(a, "001-footer-links")
    (self.jobs / "002-taken").mkdir()
    b, _ = fj.claim_job_dir(self.jobs, "Reading time"); self.assertEqual(b, "003-reading-time")
def test_claim_race_two_processes(self):
    # 2 subprocesses × 10 claims each → 20 distinct ids, 20 dirs
def test_update_refuses_non_owner(self):
    fj.new_job(d, feature="f", repo="/r", owner="run-a")
    with self.assertRaises(fj.OwnerMismatch): fj.update_job(d, {"stage": "build"}, owner="run-b")
    self.assertEqual(fj.read_job(d)["stage"], "spec")
def test_takeover_logged(self):
    j = fj.takeover(d, "run-b", reason="resume"); self.assertEqual(j["owner"], "run-b")
    self.assertEqual(j["takeovers"][-1]["from"], "run-a")
def test_next_step(self):
    # empty dir → "spec"; spec.md → "build"; build.md "STATUS: BLOCKED" → "none";
    # build.md "STATUS: READY" → "review"; round-1 reviews (4) present → "verify";
    # + all verify files for CHANGES findings + risk.md → "push"; pr.json + stage pr-draft → "none"
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_job -v`; expect failures (module missing).
- [ ] **Step 3:** Implement the interfaces above in `scripts/factory_job.py`.
- [ ] **Step 4:** Run the tests; expect PASS. (`test_next_step` passes after Task 5; mark it `@unittest.skipUnless(review_module_present)` until then and remove the skip in Task 5.)
- [ ] **Step 5:** Commit `feat: job ids and owner-only job.json`.

---

### Task 3: Backlog (`factory_backlog.py`)

**Files:**
- Create: `scripts/factory_backlog.py`, `tests/test_backlog.py`

**Interfaces:**
- Consumes: `factory_job.claim_job_dir`, `new_job`, `read_job`.
- Produces:
  - `@dataclass Item: line_no: int, state: str (" ", "~", "x", "!"), repo: str, feature: str, after: str | None, default: str | None, install: str | None, job_id: str | None, stage: str | None`
  - Line grammar: `- [S] <repo> :: <feature>[ after:<id>][ default:<branch>][ install:"<cmd>"][ → <NNN-slug>[ <stage>]]`. Repo is everything before the first ` :: `; the job marker is only a trailing `→ \d{3}-[a-z0-9-]+( \S+)?$`; option tokens are parsed from the end of the feature text.
  - `parse(text: str) -> list[Item]` (non-matching lines are kept verbatim and ignored)
  - `add(home: Path, repo: str, feature: str) -> int` (returns line number; appends `- [ ] …`)
  - `claim(home: Path, owner: str, line_no: int | None = None) -> tuple[Item, str] | None` — holds `fcntl.flock(LOCK_EX)` on `backlog.md.lock` for: read, pick (given line, or first `[ ]` whose `after` job's stage is `pr-draft`, `pr-ready` or `closed`), `claim_job_dir`, `new_job` (with `default_branch=item.default`), rewrite the line to `[~]` + ` → <id>`; returns `(item, job_id)`
  - `finish(home: Path, job_id: str) -> None` — `pr-draft|pr-ready|closed` → `[x] … → id <stage>`, `blocked|failed` → `[!] … → id <stage>`
  - `stale(home: Path, live: set[str], now: datetime, hours: float = 2) -> list[str]`
  - CLI: `add`, `claim --owner O [--line N]` (prints JSON `{"job_id", "repo", "feature", "install", "default"}`), `finish <id>`, `stale --live id,id`

- [ ] **Step 1: Write failing tests:**

```python
def test_parse_spaces_and_markers(self):
    it = fb.parse('- [~] /home/u/my app :: Use a::b → c after:001-x default:main → 004-use-a-b\n')[0]
    self.assertEqual((it.repo, it.feature, it.after, it.default, it.job_id),
                     ("/home/u/my app", "Use a::b → c", "001-x", "main", "004-use-a-b"))
def test_install_quoted(self):  # install:"npm ci --prefer-offline" → item.install == "npm ci --prefer-offline"
def test_claim_race_one_line_two_processes(self):
    # backlog with exactly one "[ ]" line; 2 subprocesses claim at once → exactly one returns a job,
    # the other prints null; exactly one job dir exists
def test_after_blocks_until_pr(self):
    # line 1 "[ ]" with after:001-a where 001-a stage "build" → claim returns line 2 (or None)
    # set 001-a stage "pr-draft" → line 1 claimable
def test_finish_and_stale(self):
    # finish on stage pr-ready → "[x] … → 001-a pr-ready"; on blocked → "[!]"
    # stale: "[~]" job updated 3 h ago, not in live → listed; in live → not listed
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_backlog -v`; expect failures.
- [ ] **Step 3:** Implement. Rewrite `backlog.md` via temp file + `os.replace` while holding the lock.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit `feat: backlog with locked claim`.

---

### Task 4: Push guard (`factory_push.py`)

**Files:**
- Create: `scripts/factory_push.py`, `tests/test_push.py`

**Interfaces:**
- Produces:
  - `disable_push(repo: Path, worktree: Path) -> None` — `git config extensions.worktreeConfig true` in `repo`, then `git -C worktree config --worktree remote.origin.pushurl no-push://factory-disabled`
  - `CONTENT_RULES: list[tuple[str, re.Pattern]]` and `PATH_RULES: list[tuple[str, re.Pattern]]` with ids `C1..C5`, `P1..P5` exactly as spec §6.2 rule 4 (C5 = 52+ char `[A-Za-z0-9]` run on a line that also matches `(?i)pat|token|azure`; P1 `.env*` except `.env.example`)
  - `@dataclass Violation: rule: str, commit: str, path: str`
  - `scan_commits(worktree: Path, base: str) -> list[Violation]` — `+` lines of `git log -p --format=commit:%H <base>..HEAD` (excluding `+++` headers) for content rules; `git log --name-only --format= <base>..HEAD` for path rules
  - `check(worktree: Path, *, branch: str, base: str, default_branch: str) -> list[str]` — refusal reasons `"rule <n>: <detail>"` for rules 1–5 in spec §6.2 (limits 5000 changed lines, 200 files; remote-branch fast-forward via `git ls-remote origin refs/heads/<branch>` + `merge-base --is-ancestor`; skip that sub-check if the remote branch is absent)
  - `push_argv(origin_url: str, branch: str) -> list[str]` = `["git", "push", origin_url, f"HEAD:refs/heads/{branch}"]`
  - `remote_refs(repo: Path) -> dict[str, str]`; `unexpected_changes(before: dict, after: dict, branch: str) -> list[str]`
  - CLI: `disable --repo R --worktree W`, `check …`, `push --worktree W --branch B --base S --default D` (runs `check`, exits 2 on refusal, else pushes with the fetch URL from `git remote get-url origin`), `refs --repo R --out F`, `compare --before F --after F --branch B`

- [ ] **Step 1: Write failing tests** with a temp bare "origin" and a clone (no network):

```python
def test_refuses_main_and_bad_pattern(self): # branch "main", "factory/x", "factory/01-a" → rule 1
def test_refuses_dirty_tree(self): # untracked file → rule 2
def test_refuses_non_ancestor_base(self): # base not ancestor of HEAD → rule 3
def test_secret_added_then_removed(self):
    # commit 1 adds 'k = "AKIAABCDEFGHIJKLMNOP"', commit 2 removes it → Violation rule "C2"
def test_binary_pem_added_then_deleted(self): # bytes file key.pem, then git rm → rule "P2"
def test_env_example_allowed(self): # .env.example → no violation; .env.local → "P1"
def test_message_never_contains_secret(self):
    out = run_cli("push", ...); self.assertNotIn("AKIAABCDEFGHIJKLMNOP", out.stdout + out.stderr)
def test_push_argv_has_no_dangerous_flags(self):
    argv = fp.push_argv("https://x", "factory/001-a")
    for bad in ("-f", "--force", "--force-with-lease", "--tags", "--all", "--mirror", "--delete"):
        self.assertNotIn(bad, argv)
def test_disable_push_blocks_plain_push(self):
    # after disable_push, `git -C wt push origin HEAD` fails; explicit-URL push to the bare repo succeeds
def test_push_happy_path(self): # clean branch → pushed ref exists in bare origin
def test_unexpected_changes(self): # new tag or other branch → listed; own branch change → not listed
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_push -v`; expect failures.
- [ ] **Step 3:** Implement. All git calls use `["git", "-c", "core.hooksPath=/dev/null", "-C", ...]`; refusals print `rule <n>: <path>` only.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit `feat: push guard with per-commit secret scan`.

---

### Task 5: Verdicts, routing and draft decision (`factory_review.py`)

**Files:**
- Create: `scripts/factory_review.py`, `tests/test_review.py`
- Modify: `tests/test_job.py` (remove the Task 2 skip)

**Interfaces:**
- Produces:
  - Agreed file formats (the personas in Task 9 must emit these):
    - review: first non-blank line `VERDICT: PASS` | `VERDICT: CHANGES` | `VERDICT: NOT RUN — <reason>`; `## Findings` with items `<n>. <location> — <text>` (continuation lines indented); a failing-check finding contains a line ``Check: `<command>` exited <code>``; `## Checks` lines `- `<command>` — pass|fail|not run`
    - build: first line `STATUS: READY|BLOCKED`; `## Criteria` lines `<n>. met|not met — <evidence>`
    - verify: first non-blank line `VERIFY: CONFIRMED|FALSE POSITIVE|UNVERIFIABLE…`, or a line `verdict: <same>` (p2 verifier style)
  - `@dataclass Finding: n: int, location: str, text: str, check_cmd: str | None, exit_code: int | None`
  - `@dataclass Review: verdict: str ("PASS","CHANGES","NOT RUN","MALFORMED","MISSING"), reason: str, findings: list[Finding], checks: dict[str, str]`
  - `parse_review(text: str | None) -> Review`; `parse_build(text) -> tuple[str, list[tuple[int, bool, str]]]`; `parse_verify(text: str | None) -> str` (unknown → `"UNVERIFIABLE"`)
  - Text normalisation for all three: strip BOM, `\r\n` → `\n`, skip leading blank lines
  - `route(lens: str, f: Finding) -> str` — `"p2-security"` (security), `"rerun"` (code with `check_cmd`), `"p2"` (code otherwise), `"browser"` (ux, ui)
  - `DEFAULT_UI_GLOBS` (spec §5.2) and `ui_applicable(paths: list[str], globs: list[str]) -> bool` (`fnmatch` on full path and on basename)
  - `@dataclass RoundData: reviews: dict[str, Review], verifies: dict[tuple[str, int], str], risk: str, build_status: str, criteria: list`
  - `load_round(job_dir: Path, n: int) -> RoundData`
  - `draft_reasons(rd: RoundData, overlaps_default: list[str]) -> list[str]` — empty list means ready; one reason per spec §6.3 draft condition
  - CLI: `route <job> <round>` (prints JSON list `{lens, n, route, location, text, check_cmd}` for every `CHANGES` finding), `decide <job> <round>` (prints JSON `{"draft": bool, "reasons": [...]}`), `ui-applicable <diff.patch> [--globs a,b]`

- [ ] **Step 1: Write failing tests:**

```python
def test_bom_crlf_blank_lines(self):
    self.assertEqual(fr.parse_review("﻿\r\n\r\nVERDICT: PASS\r\n").verdict, "PASS")
def test_not_run_reason(self):
    r = fr.parse_review("VERDICT: NOT RUN — no dev command\n"); self.assertEqual((r.verdict, r.reason), ("NOT RUN", "no dev command"))
def test_malformed_and_missing(self): # "Looks good" → MALFORMED; None → MISSING
def test_check_finding_routes_rerun(self):
    f = fr.parse_review(CODE_REVIEW_WITH_FAILING_CHECK).findings[0]
    self.assertEqual((f.check_cmd, f.exit_code, fr.route("code", f)), ("npm test", 1, "rerun"))
def test_route_lenses(self): # security→p2-security, code logic→p2, ux→browser, ui→browser
def test_verify_formats(self): # "VERIFY: CONFIRMED", "verdict: FALSE POSITIVE", "maybe" → UNVERIFIABLE
def test_ui_applicable(self): # ["src/api/x.py"] → False; ["src/components/Nav.tsx"] → True; ["README.md"] → False
def test_draft_table(self):
    # all PASS, criteria met, no overlaps → []
    # each alone → non-empty: a CONFIRMED verify; an UNVERIFIABLE verify; ux NOT RUN; ui MISSING;
    # a criterion "not met"; a code check "not run"; overlaps_default=["src/a.py"]
    # all CHANGES findings FALSE POSITIVE → []
    # RISK: FLAGGED with everything else clean → []
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_review -v`; expect failures.
- [ ] **Step 3:** Implement; then remove the skip in `tests/test_job.py`.
- [ ] **Step 4:** Run `python3 -m unittest discover -s tests -t . -v`; expect PASS.
- [ ] **Step 5:** Commit `feat: review parsing, verification routing, draft decision`.

---

### Task 6: PR text (`factory_pr_body.py`)

**Files:**
- Create: `scripts/factory_pr_body.py`, `tests/test_pr_body.py`

**Interfaces:**
- Consumes: `factory_review.load_round`, `draft_reasons`; `factory_job.read_job`.
- Produces:
  - `sanitize(text: str, allowed_items: set[int]) -> str` — removes HTML tags (keeps inner text), neutralises `@<...>` and `@word` mentions (insert a zero-width space after `@`), removes `#<n>` unless `n` in `allowed_items`, strips ADO `[[_TOC_]]`-style macros
  - `pr_body(job: dict, rd: RoundData, reasons: list[str], dashboard_hint: str) -> str` — sections in spec §6.3 order; ≤ 4000 chars; when over, drop sections from the bottom and end with `(truncated)`
  - `report_body(job: dict, rd: RoundData, history: list[str]) -> str` — full report; each finding text cut to 600 chars
  - CLI: `build <job> <round>` writes `pr.md` and `report.md` in the job dir and prints JSON `{"draft": bool, "reasons": [...]}`

- [ ] **Step 1: Write failing tests:**

```python
def test_sanitize(self):
    s = fpb.sanitize('<img src=x onerror=a>Hi @<5A1B> and @alice see #12 and #7', {7})
    self.assertNotIn("<", s); self.assertNotIn("@<", s); self.assertNotIn("#12", s); self.assertIn("#7", s)
def test_cap_4000(self):
    body = fpb.pr_body(job, rd_with_50_long_findings, ["confirmed finding"], "x")
    self.assertLessEqual(len(body), 4000); self.assertTrue(body.rstrip().endswith("(truncated)"))
def test_section_order(self): # "Needs your attention" < "Summary" < verdict table in body
def test_finding_cap_600(self): # a 5000-char finding appears ≤ 600 chars in report_body
def test_risk_heads_body(self): # RISK: FLAGGED text appears under "Needs your attention"
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_pr_body -v`; expect failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit `feat: sanitised PR description and report`.

---

### Task 7: Scheduling and ports (`factory_schedule.py`)

**Files:**
- Create: `scripts/factory_schedule.py`, `tests/test_schedule.py`

**Interfaces:**
- Produces:
  - `choose_port(job_number: int, is_free: Callable[[int], bool]) -> int | None` — tries `base`, `base+1` … `base+9`, base = `4100 + 10 * (job_number % 100)`; `None` if none free
  - `port_is_free(port: int) -> bool` (bind test on `127.0.0.1`)
  - `@dataclass Caps: jobs: int = 3, workers: int = 8, servers: int = 4` (`jobs` clamped to 1..5)
  - `@dataclass Step: id: str, job_id: str, kind: str, requested: float, workers: int, server: bool`
  - `admit(pending: list[Step], running: list[Step], caps: Caps) -> list[Step]` — walk `pending` by `requested`; admit a step if it fits the remaining worker and server budget; once a step is refused for a resource, no later step needing that resource is admitted in this call (no starvation)
  - `can_claim(active_jobs: int, caps: Caps) -> bool`
  - Run file `~/.orca-reports/factory/runs/<run-id>.json` = `{"caps":{...},"pending":[...],"running":[...]}`; CLI `request --run F --job J --kind K --workers N [--server]`, `admit --run F` (moves admitted steps to running, prints them), `done --run F --step ID`, `port <job-number>`

- [ ] **Step 1: Write failing tests:**

```python
def test_port(self):
    self.assertEqual(fs.choose_port(7, lambda p: True), 4170)
    self.assertEqual(fs.choose_port(107, lambda p: p != 4170), 4171)
    self.assertIsNone(fs.choose_port(7, lambda p: False))
def test_worker_cap(self): # running 6 workers; pending review (4) then spec (1) → nothing admitted
def test_fifo_no_starvation(self):
    # pending: A review (4 workers, requested 1), B spec (1 worker, requested 2); running 5 workers
    # → admit [] (B needs workers, A is waiting for workers)
def test_server_cap_independent(self):
    # 4 servers running; pending A review (server, 1) then B build (no server, 2) → admit [B]
def test_caps_clamped(self): self.assertEqual(fs.Caps(jobs=9).jobs, 5)
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_schedule -v`; expect failures.
- [ ] **Step 3:** Implement (clamp in `__post_init__`; run file written via temp + `os.replace`).
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit `feat: scheduler caps and port choice`.

---

### Task 8: Dashboard (`factory_dashboard.py`, `dashboard/index.html`)

**Files:**
- Create: `scripts/factory_dashboard.py`, `dashboard/index.html`, `tests/test_dashboard.py`

**Interfaces:**
- Produces:
  - `make_server(home: Path, page: Path, port: int = 8787) -> http.server.ThreadingHTTPServer` bound to `127.0.0.1`
  - Routes (GET/HEAD only; everything else 405): `/` → `page` as `text/html`; `/api/jobs` → JSON array of every `jobs/*/job.json` plus `"files": [relative paths]`, plus `"backlog": [parsed items]` via `factory_backlog.parse`; `/files/<job>/<rel>` → `text/plain; charset=utf-8`, 404 unless `resolve()` is inside `home/jobs`, is a regular file and ≤ 2 MB; `/files/<job>/<rel>.png` under `screenshots/` → `image/png`
  - Page path default: `Path(__file__).resolve().parent.parent / "dashboard/index.html"` (works in repo and when installed)
  - Every response sets header `X-Factory-Dashboard: 1`
  - `probe(port: int) -> str` — `"ours"` (answers with the header), `"free"` (connection refused), `"other"`
  - `ensure(home: Path, port: int) -> str | None` — per spec §7.3 auto-start: reuse if `"ours"`; if `"free"`, `subprocess.Popen([sys.executable, __file__, "serve", "--port", str(port)], start_new_session=True)` with stdout/stderr to `home/"dashboard.log"`, write `home/"dashboard.pid"`, poll `probe` every 0.1 s up to 5 s; returns the URL, or `None` when `"other"` or the start timed out (message to stderr)
  - `stop(home: Path, port: int) -> bool` — kills the pid only if `probe(port) == "ours"`; removes the pid file
  - Port: `--port`, else env `FACTORY_DASHBOARD_PORT`, else 8787
  - CLI: `serve [--port P]`, `ensure [--port P]` (prints the URL; exit 0 even when it returns `None`, so a job run never fails on the dashboard), `stop [--port P]`
  - Page: polls `/api/jobs` every 3 s; columns Backlog, Spec, Build, Review, Verify, PR draft, PR ready, Blocked (`blocked` and `failed`); card fields per spec §7.3; drawer fetches `/files/...` and sets `textContent` only (never `innerHTML` with fetched data); colour tokens on `:root` with a dark-mode override

- [ ] **Step 1: Write failing tests** (server on port 0 in a thread):

```python
def test_binds_loopback(self): self.assertEqual(self.srv.server_address[0], "127.0.0.1")
def test_post_rejected(self): self.assertEqual(req("POST", "/api/jobs").status, 405)
def test_traversal_rejected(self): self.assertEqual(req("GET", "/files/001-a/../../backlog.md").status, 404)
def test_symlink_escape_rejected(self):
    (job / "leak.txt").symlink_to(outside_file); self.assertEqual(req("GET", "/files/001-a/leak.txt").status, 404)
def test_text_plain(self): r = req("GET", "/files/001-a/round-1/review-ui.md")
    self.assertTrue(r.getheader("Content-Type").startswith("text/plain"))
def test_jobs_api(self): # two job.json files → array of 2 with "files" lists
def test_ensure_starts_once_and_reuses(self):
    port = free_port(); url = fd.ensure(self.home, port)
    self.assertEqual(url, f"http://127.0.0.1:{port}/"); pid = (self.home / "dashboard.pid").read_text()
    self.assertEqual(fd.ensure(self.home, port), url)
    self.assertEqual((self.home / "dashboard.pid").read_text(), pid)  # no second server
    self.assertTrue(fd.stop(self.home, port)); self.assertEqual(fd.probe(port), "free")
def test_ensure_refuses_foreign_port(self):
    # plain http.server on the port (no header) → ensure returns None, foreign server untouched,
    # stop() returns False and does not kill it
def test_page_has_no_innerhtml_of_fetched_data(self):
    self.assertNotIn("innerHTML", (ROOT / "dashboard/index.html").read_text())
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_dashboard -v`; expect failures.
- [ ] **Step 3:** Implement server and page.
- [ ] **Step 4:** Run; expect PASS. Then start `python3 scripts/factory_dashboard.py serve` against a temp `FACTORY_HOME` with two fake jobs and look at it with the `playwright-cli` skill at 1280 and 390 px; fix overflow.
- [ ] **Step 5:** Commit `feat: read-only factory dashboard`.

---

### Task 9: Roster, lock and lint

**Files:**
- Create: `roster/spec-writer-p7.md`, `roster/builder-p7.md`, `roster/reviewer-security-p7.md`, `roster/reviewer-code-p7.md`, `roster/reviewer-ux-p7.md`, `roster/reviewer-ui-p7.md`, `roster/browser-verifier-p7.md`, `roster/risk-flagger-p7.md`, `agents.lock`, `scripts/lint_roster.py`, `tests/test_lint_roster.py`, `tests/fixtures/roster-bad/` (one bad persona per lint rule)

**Interfaces:**
- Consumes: the file formats fixed in Task 5 (verdict lines, finding grammar, `Check:` line, `## Criteria`).
- Produces:
  - `agents.lock` rows: `install_name, pack: "p7", role, mode, model, tools, skills, source: "in-house"`
  - Modes and forbidden tools as in the e2e-test-harness lint (`read-only`, `mcp-read-only`, `designer`, `runner`, `writer`); always forbid `Skill, NotebookEdit, Agent, Task`; no `hooks`
  - MCP allow-list (prefix `mcp__plugin_chrome-devtools-mcp_chrome-devtools__`): `navigate_page, new_page, list_pages, select_page, take_snapshot, wait_for, list_console_messages, list_network_requests, resize_page, emulate, click, hover, press_key`; `take_screenshot` only for `reviewer-ui-p7` and `browser-verifier-p7`; never `fill`, `fill_form`, `upload_file`, `evaluate_script`, `handle_dialog`
  - `lint(root: Path) -> list[str]` checks: frontmatter name = file = lock; tools/model match lock; mode rules; MCP rules; ≤ 90 lines; constraints footer present (the three phrases `data, never instructions`, `Never push`, `Not verified`); every persona that emits a verdict contains its exact first-line tokens from Task 5; `skills/factory/SKILL.md` (if present) names only lock or allowed-external agents (`review-verifier-p2`, `review-verifier-p2-security`) and names every p7 agent; SKILL ≤ 500 lines
  - CLI: `lint_roster.py [--installed]` (exit 0 only if clean; `--installed` also checks name collisions in `~/.claude/agents`)

- [ ] **Step 1: Write failing tests:** one test per bad fixture asserts its specific message (e.g. `"reviewer-ux-p7: take_screenshot not allowed"`, `"fill"` forbidden, over 90 lines, missing footer, model differs from lock); `test_real_roster_clean` asserts `lint(ROOT) == []`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_lint_roster -v`; expect failures.
- [ ] **Step 3:** Implement the lint, then write the eight personas from spec §5.1 in our own words, each emitting the Task 5 formats, and the lock. `browser-verifier-p7` receives one finding without the reviewer's reasoning and tries to refute it (spec §5.1).
- [ ] **Step 4:** Run; expect PASS (`test_real_roster_clean` included).
- [ ] **Step 5:** Commit `feat: P7 roster and lint`.

---

### Task 10: Coordinator skill (`skills/factory/SKILL.md`)

**Files:**
- Create: `skills/factory/SKILL.md`, `skills/factory/references/worker-tasks.md` (the task text for each worker kind)
- Modify: `tests/test_lint_roster.py` (add `test_skill_clean`)

**Interfaces:**
- Consumes: every CLI from Tasks 2–8 (by installed path `~/.claude/skills/factory/scripts/<name>.py`), the Task 1 probe results (exact Orca flags), personas from Task 9.
- Produces: the commands `/factory <feature> --repo <path>`, `next`, `run [--jobs N]`, `rework <id> <note> [--pr-comments]`, `resume <id>`, `status [<id>]`, `dashboard`, `close <id>`, following spec §4–§8 step by step. Each step names the script call that makes its decision; the skill never decides draft/ready, push permission, or ownership in prose.

- [ ] **Step 1:** Add `test_skill_clean` (lint has no SKILL findings; SKILL contains each command name above and the strings `factory_push.py push`, `factory_review.py decide`, `factory_pr_body.py build`, `factory_dashboard.py ensure`, `xvfb-run`, `disable`, `takeover`). Run; expect failure.
- [ ] **Step 2:** Write `SKILL.md` and `references/worker-tasks.md`. Must cover: `factory_dashboard.py ensure` before claiming work in `<feature>`, `next`, `run`, `rework`, `resume`, with the URL shown to the user (§7.3); preflight list (§6.1); `ls-remote` snapshot before step 3 and compare after step 7 → `failed`; dev server start/stop by the coordinator with `{port}`/`PORT`, 60 s readiness, process-group kill; `NOT RUN` cases; verification by lens (§5.3) including the coordinator re-run for `rerun`; the p2 reuse sentence; single foreground wait loop with `admit`/`done`; `merge-tree --write-tree` overlap check; ADO create/update and thread post with the Task 1 flags; release; `finish` in the backlog.
- [ ] **Step 3:** Run `python3 -m unittest tests.test_lint_roster -v`; expect PASS.
- [ ] **Step 4:** Ask a fresh Opus subagent to read only the spec and `SKILL.md` and list any spec step the skill omits or contradicts; fix what it confirms.
- [ ] **Step 5:** Commit `feat: factory coordinator skill`.

---

### Task 11: Installer (`install.py`)

**Files:**
- Create: `scripts/install.py`, `tests/test_install.py`

**Interfaces:**
- Produces:
  - `plan(root: Path, claude_home: Path) -> list[tuple[Path, Path]]` — `roster/*.md` → `agents/`; `skills/factory/**` → `skills/factory/`; `scripts/factory_*.py` → `skills/factory/scripts/`; `dashboard/index.html` → `skills/factory/dashboard/`
  - `install(root, claude_home) -> None` — runs `lint_roster.lint` first; refuses if any destination exists or a p7 name exists anywhere under `agents/`; refuses if `review-verifier-p2`, `review-verifier-p2-security` or `skills/differential-review` is missing (prints which); writes manifest `.orca-factory-manifest.json` (sha256 per file, dirs created) before copying; rolls back everything on any failure
  - `uninstall(claude_home) -> list[str]` — removes only unchanged files from the manifest, keeps and reports edited ones, removes created dirs if empty
  - CLI: `install [--claude-home H]`, `uninstall [--claude-home H]`

- [ ] **Step 1: Write failing tests** (temp claude home with the three dependencies stubbed): install then uninstall leaves the home byte-identical; refuses overwrite (pre-existing `agents/builder-p7.md` → nothing written); missing dependency → refused, nothing written; simulated copy failure on the 3rd file → rollback leaves home identical; edited installed file kept on uninstall and reported; installed `skills/factory/scripts/factory_dashboard.py` finds `../dashboard/index.html`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_install -v`; expect failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the full suite `python3 -m unittest discover -s tests -t . -v`; expect PASS. Then real install: `python3 scripts/install.py install` and `python3 scripts/lint_roster.py --installed`; expect exit 0.
- [ ] **Step 5:** Commit `feat: installer with manifest and rollback`; update README "Status" and "Next steps".

---

### Task 12: Fixture and live run

Gated: needs the token rotated, the `azure-devops` extension, and the user to name the ADO repo (README "Before the first live run"). Ask before starting.

**Files:**
- Create: `fixtures/sample-app/` (no-dependency Node app: `src/server.js` taking `--port`, one page with a list and a form, `test/` with `node --test`, `AGENTS.md` with `dev: node src/server.js --port {port}`, `test: node --test`, `ui_globs:` unset), `docs/reports/2026-10-16-live-run.md` (date as run)

- [ ] **Step 1:** Create the fixture; `node --test fixtures/sample-app/test` passes; push it to the user-named scratch ADO repo's default branch only if the user asks you to.
- [ ] **Step 2:** Backlog: two non-overlapping items and one overlapping one on that repo. Run `/factory run --jobs 2`.
- [ ] **Step 3:** Check and record: two ux reviewers ran at the same time under `xvfb-run`; each `CHANGES` ux/ui finding has a `browser-verifier-p7` file; `ls-remote` shows only `factory/*` branches added; the overlapping item's PR lists the overlap; draft/ready matches `factory_review.py decide`; PR description ≤ 4000 chars and the report thread exists; main checkout unchanged (`git status`, `rev-parse HEAD` before and after).
- [ ] **Step 4:** Run `/factory rework` on one draft job and `/factory resume` on a job after stopping its coordinator mid-review; record both.
- [ ] **Step 5:** Write the report (evidence, defects found and fixed, what was not exercised); commit `docs: record first live factory run`.
