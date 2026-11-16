# Factory: integration branch and ship (amends P7 spec)

Date: 2026-10-16
Status: Draft for review. Decisions in §2 are the user's (2026-10-16).
Amends: `docs/specs/2026-10-16-factory-skill-design.md` (the base spec). Where this document
and the base spec disagree, this one wins. Sections of the base spec not named in §10 stay as
they are.

## 1. Goal

Replace the per-job Azure DevOps pull request with a local integration branch per run.
Each job is still specified, built on its own `factory/<id>` branch, reviewed by four lenses
with every `CHANGES` finding verified, and risk-flagged, exactly as the base spec says. At the
end, instead of a PR:

- a **ready** job is squash-merged into its run's branch automatically;
- a **draft** job is **held** until the user accepts it or reworks it;
- nothing reaches `main` until the user runs `/factory ship`, which gates, merges, and asks
  once before pushing `main`.

## 2. Decisions (user, 2026-10-16)

| Topic | Decision |
|---|---|
| Where | Local only. No ADO calls during runs; job and run branches are never pushed |
| What merges into the run branch | Ready jobs automatically; draft jobs only after `/factory accept <job-id>` |
| Runs before a ship | Independent: each run branch starts from current `origin/main`; `/factory ship <run-id>` ships one run |
| Ship | Gate (merge in a temporary worktree, run tests/build, secret scan), fast-forward local `main`, show a summary, ask once, then push `main` |
| History | Each job lands as one squashed commit on the run branch; ship merges the run into `main` with one `--no-ff` merge commit |

## 3. Non-goals (replaces base §3)

- Pull requests of any kind; `az` use of any kind.
- Pushing anything other than the default branch, and pushing it other than from
  `/factory ship` after the user's confirmation in that same command.
- Force-pushing, pushing tags, deleting remote branches.
- Resolving merge conflicts. The factory reports them; a human or a rework resolves them.
- Merging into `main` without `/factory ship`.
- Deploying. Running sign-in or form submission against a dev server.
- Changing P1–P6 skills or personas.

## 4. Runs

### 4.1 Starting a run

`/factory run [--jobs N]`, `/factory next` (a run of one job) and `/factory <feature> --repo
<path>` (a run of one ad-hoc job) each start a **new run**:

1. Preflight (base §6.1 without the ADO and `az` items): Orca ready; p2 verifiers and the
   `differential-review` skill installed; `origin` set; default branch `D` known (backlog
   `default:` → `origin/HEAD` → stop); browser-lens checks as before.
2. Run id `<YYYYMMDD>-<HHMM>-<4 hex>` (UTC), claimed with `mkdir runs/<run-id>.lock` (no `-p`).
3. `factory_integrate.py start-run --repo <repo> --run <run-id> --default D`: runs `git fetch
   origin D` (with the coordinator git hygiene of base §6.2 rule 6 and `core.hooksPath=/dev/null`),
   creates local branch `factory-run/<run-id>` at `origin/D` (refuses if it exists), records
   `branch`, `base` (that sha), `repo`, `default_branch` in the run file, and takes the
   remote-refs snapshot `runs/<run-id>.refs-before.json` (base §6.2 `refs`).
4. Jobs are claimed and driven as in base §4 steps 1–6 and §8. Each job records `run` (the run
   id) in `job.json`. A job's own `base` is still the `origin/D` sha fetched at its step 3.

All jobs of one run must target the same repo. `/factory run` takes backlog items for the repo
of the first claimed item only; items for other repos stay `[ ]` for a later run.

### 4.2 Run file

`~/.orca-reports/factory/runs/<run-id>.json` (existing scheduler file, extended). Only the run's
`owner` (Orca run id) writes it, via `factory_integrate.py`; whole-file rewrite through a temp
file and rename. New fields: `id, repo, default_branch, branch, base, owner, merged[]` (job ids
in merge order, each with the squash commit sha), `held[]` (job id + reasons), `shipped`
(`null` or `{merge_sha, pushed: bool, at}`), `created, updated`. The scheduler's `caps`,
`pending`, `running` keys stay.

## 5. End of a job (replaces base §4 steps 7–8 and §6.3)

After the reviews, verifications and risk file of the job's current round exist (base §4
step 6):

1. `factory_review.py decide J <N>` → `{draft, reasons}` (unchanged, including overlaps with
   `origin/D` from base §8.3).
2. `factory_pr_body.py build J <N>` writes `J/report.md` only (the job report; same sections and
   sanitising as base §6.3's report, no 4000-character `pr.md`).
3. **Ready** (`draft` false): `factory_integrate.py merge-job --run <run-id> --job <job-id>`
   (§6). Exit 0 → stage `merged`. Exit 3 (conflict) → stage `held` with reason
   `conflicts with <job-ids or "run branch"> in <paths>`. Exit 2 (refused) → stage `held` with
   the refusal lines. Exit 1 → stage `failed`.
4. **Draft**: stage `held`; the run file's `held[]` gets the reasons from `decide`.
5. Release workers; `factory_backlog.py finish <job-id>`.

`held` jobs keep their child worktree and `factory/<id>` branch.

## 6. Merging a job (`factory_integrate.py merge-job`)

No checkout and no worktree: the merge is done with plumbing, so no hooks, filters or
`.gitattributes` drivers run. Requires git ≥ 2.40 (`merge-tree --merge-base`); preflight checks
it (this machine: 2.43).

1. Refuse (exit 2) unless the job's run is `<run-id>`, its stage is not `merged`/`shipped`, and
   either `decide` reports no reasons now or `job.json` has an `accepted` record (§7) for the
   job's current round. Also refuse while the run is being shipped (`ship.local` recorded, or
   local `D` already contains the prepared ship merge): a job merged then would sit `merged` in
   a run whose ship merge does not contain it. The job is held (`refused`); rework it after
   the ship finishes. The reviewed commit is bound to its round: `mark-review` records
   `review_head` (the `factory/<id>` tip the reviews looked at) and `review_round`; merge-job
   refuses unless `review_round` equals the job's `round` ("review round not recorded; rework
   the job" when absent, "review head is from another round; rework the job" otherwise) and
   `factory/<id>` is still at `review_head`. Resume never runs `mark-review` past the review
   stage, which would bless an unreviewed head.
2. Run base §6.2 rule 6 (config check, plus the origin URL rewrite check of §8) on the repo
   and the job's worktree, and the base §6.2
   rule 4 secret scan on every commit of `<job base>..factory/<id>`; refuse (exit 2) on any
   violation. These hard rules cannot be overridden by `accept`.
3. Backstop: take a fresh `refs` snapshot and `compare` it with the run's `refs-before` with no
   expected branches (`compare`'s `--branch` becomes optional) and `--default D`; suspicious →
   exit 2 (the job is held; base §6.2 classes).
4. The run tip is owned by the factory: the expected tip is the last `merged[]` commit, or
   the run's `base` when nothing is merged yet. `git merge-tree --write-tree --merge-base
   <job base> <expected tip> factory/<id>`; exit 1 → conflict: exit 3 with the conflicting
   paths. No ref changes. If the branch is not at the expected tip, the only tip adopted is
   this job's own squash from a retry after a crash (CAS done, run file not): exactly one
   parent (the expected tip), the tree merge-tree just wrote, and `Factory-Job`/`Factory-Round`
   trailers naming this job and round; it is recorded as is. Anything else refuses ("run
   branch moved outside the factory").
5. `git commit-tree <tree> -p <expected tip>` (the run tip equals it here) with message `<job-id>: <feature>` (feature sanitised
   and capped at 200 characters) and a body: verdict per lens, confirmed/accepted findings
   count, risk first line, `Factory-Job: <job-id>`, `Factory-Round: <N>`.
6. `git update-ref refs/heads/factory-run/<run-id> <new> <expected tip>` (compare-and-swap; a
   concurrent change makes it fail → exit 1).
7. Append `{job, commit}` to the run's `merged[]`, remove the job from `held[]`.

## 7. Commands (replaces base §4.1–§4.2 where they differ)

| Command | Does |
|---|---|
| `/factory run [--jobs N]`, `/factory next`, `/factory <feature> --repo <path>` | Start a run (§4.1) and drive its jobs |
| `/factory accept <job-id> [<note>]` | For a `held` job whose hold is a draft decision: shows its reasons, records `accepted {by, at, round, note, reasons}` in `job.json` (`factory_integrate.py accept`), then runs `merge-job`. Conflicts, secret-scan and config refusals are not acceptable |
| `/factory rework <job-id> <note>` | Allowed for `held`, `blocked`, `failed` jobs; round N+1 as base §4.1; at the end it goes through §5 again. Not allowed for `merged` or `shipped` jobs (open a new backlog item instead). A `conflict` hold is reworked only once the jobs it conflicts with are in `D`, i.e. after its run shipped (a conflict with `origin/D` alone too); its branch is then restarted on the new run's base (§9) |
| `/factory ship [<run-id>]` | §8. Without an id: the only unshipped run with at least one merged job, else ask which |
| `/factory status [<job-id or run-id>]` | Jobs and runs; for a run: merged, held, unshipped |
| `/factory resume <job-id>` | As base §7.4, with steps re-derived up to §5 |
| `/factory dashboard`, `/factory close <job-id>` | As base. `close` deletes the local `factory/<id>` branch only when the job is `shipped` |

## 8. Ship (`/factory ship`, `factory_integrate.py ship-*`)

The coordinator runs these in order and stops at the first failure, telling the user why.
Nothing in steps 1–4 changes any branch.

1. **Prepare** (`ship-prepare --run <run-id>`): the run has ≥ 1 merged job and is not shipped;
   `git fetch origin D` (hygiene as §4.1; its result is not trusted as an anchor, see step 7); config check (rule 6) on the repo; backstop
   `compare` against the run's `refs-before` (suspicious → stop). Before any call to origin
   (here, in `start-run`, `merge-job`, `ship-status` and the push), the recorded origin URL
   must not be rewritten by any config scope: `git ls-remote --get-url <url>` run from `/`
   (fetch) and `git -c remote.factoryprobe.url=<url> remote -v` in the repo (push; `remote
   get-url` ignores a remote defined only by `-c`) must both print `<url>`, and no scope may
   define a remote named by the URL itself (`remote."<url>".*`: git takes a URL argument as a
   remote name, so its `url`/`pushurl`/`uploadpack`/`receivepack` would redirect the call;
   section compared case-insensitively, subsection exactly). Else `rule 6: origin url is
   rewritten by config (fetch|push|remote)`. Rule 6 already refuses repo and worktree
   `url.*`, `remote.origin.*` keys other than the allowlisted ones, and a remote named
   exactly by the origin URL (other remotes, e.g. a fork's `upstream`, and
   `remote.pushDefault` are allowed: git resolves a remote only by an exactly equal name);
   this covers `~/.gitconfig`, which a worker (same OS user) can write.
   Prepare refuses when an earlier ship already moved local `D` (`ship.local` recorded, or
   local `D` contains `ship.merge_sha`): preparing again would replace `merge_sha` (or merge
   twice after a push). The refusal prints `{merge_sha, base, run_tip, local,
   origin_has_merge}`.
   **Resume**: `ship-status --run <run-id>` (read-only, no owner) prints `{merge_sha, base,
   local, gate, origin_has_merge}`. `origin_has_merge` is true when origin's tip of `D` (by
   `ls-remote`) is `merge_sha` or descends from it (a tip not in the local object store is
   fetched first, with the same hygiene and timeout, and must then exist locally), false when
   not, null when origin cannot be read or a guard refuses (then `origin_error` says why).
   `ship-status` writes no ref other than `refs/remotes/origin/D`. The coordinator first runs
   the gate (step 3) unless `gate` is `pass` for `merge_sha`, then resumes at step 8 with
   `--pushed yes` (true), at step 6 (false), or stops and shows `origin_error` (null).
2. **Merge** (plumbing), anchored on the server tip of `D` (`git ls-remote --symref` run
   outside any repo, as in step 7; `D` must be what origin's `HEAD` points at), never on
   `refs/remotes/origin/D`: after the fetch the object for that tip must exist locally, and the
   run branch must hold exactly the run file's merged commits. `git merge-tree --write-tree
   <tip> factory-run/<run-id>`; conflict → stop, list the paths and the merged jobs that touch
   them. Otherwise `commit-tree` with parents `<tip>` and the run tip and message `factory: ship
   run <run-id>` plus one line per merged job (`<job-id>: <feature>`, `accepted` marked). Record
   `ship = {merge_sha, base: <tip>, run_tip}` in the run file (not shipped yet); `base` is the
   `--base` of step 7. Steps 3 and 5 refuse if the run branch moved since (prepare again).
3. **Gate** (`ship-gate --run <run-id> --owner <owner> [--allow-install]`): `git worktree add --detach <tmp> <merge_sha>` in a
   temporary directory outside every repo (a coordinator-owned throwaway, not an Orca worker
   worktree); run `AGENTS.md` (read at the recorded server tip, never the run or merge commit)
   `install:` (network: the user approves it once per ship), then
   `test:` and `build:` (each absent → recorded `not run`), with the coordinator's environment
   minus `AZURE_DEVOPS_EXT_PAT` and any `*TOKEN*`/`*_PAT*`/`GIT_ASKPASS` variable, timeout 30 min
   each, process group killed on timeout. Any non-zero exit → stop. Secret scan (rule 4) of
   every commit in `<server tip>..<merge_sha>` (the tip reported by `git ls-remote`, step 7; merge commits are scanned against their first parent). Remove the temporary worktree in all cases.
   The checkout blanks every configured filter driver (`GIT_LFS_SKIP_SMUDGE=1`, scrubbed env).
   The result (pass/fail) is recorded as `ship.gate` bound to `merge_sha`; a new prepare clears it.
4. **Summary**: jobs (merged, accepted with their reasons), commits, files changed, gate results.
5. **Local main** (`ship-local --run <run-id>`): local `D` must equal the recorded server tip
   or be an ancestor of `merge_sha`; otherwise stop ("local D has diverged"). It also stops unless `ship.gate` passed for this
   `merge_sha`. If `D` is checked out in a
   worktree (the user's checkout or any other), run `git -C <that worktree> merge --ff-only
   <merge_sha>` there (it refuses if local changes would be overwritten → stop). If not checked
   out, `git update-ref refs/heads/D <merge_sha> <old>`.
6. **Ask once**: "Push D to origin? (y/n)". No → run `shipped {pushed: false}`; the user pushes
   later themselves.
7. **Push** (`factory_push.py ship --repo <repo> --default D --sha <merge_sha> --base <ship.base
   from the run file> --origin-url <origin url> --run <run-id>`): the push is bound to the
   gate record. It reads `runs/<run-id>.json` itself (run id format checked, opened with
   `O_NOFOLLOW`; `FACTORY_HOME` and `runs/` themselves are trusted) and refuses (`ship: …`)
   unless the run's `repo` resolves to `--repo`, `origin_url` is `--origin-url`,
   `default_branch` is `--default`, `ship.merge_sha` is `--sha`, `ship.base` is `--base`,
   `ship.gate` passed for `--sha`, and `shipped` is null. The base and fast-forward anchor is the server-reported tip
   of `D` (`git ls-remote` run outside any repo), never `refs/remotes/origin/D`, which workers can
   rewrite. It refuses unless `D` is the branch origin's `HEAD` points at, `--base` is exactly
   that tip, the tip exists locally and is an ancestor of `merge_sha` (fast-forward), local `D` ==
   `merge_sha`, rule 6 passes and rule 4 passes for `<server tip>..<sha>`;
   then `git push --porcelain --no-follow-tags --recurse-submodules=no <origin url>
   <sha>:refs/heads/D`. A rejected push (branch policy, non-fast-forward) is reported plainly;
   local `D` stays merged and the run is `shipped {pushed: false}`.
8. **Finish**: refused unless `ship.gate` passed for `merge_sha` (local `D` may hold an
   ungated merge, e.g. moved outside the factory). Run `shipped`; every merged job whose squash commit is reachable from
   `merge_sha` (and that still belongs to the run and its owner) becomes `shipped` and is
   `finish`ed in the backlog; any other merged job (e.g. merged after prepare) is skipped and
   listed with its reason. `git branch -d factory-run/<run-id>`; when `-d` refuses because
   `HEAD` is not `D`, `update-ref -d` deletes it if its tip is still `ship.run_tip` and
   reachable from `D`. Remove the job worktrees of shipped jobs through Orca. `held` jobs of that run stay held; they can join a later run only
   by rework (§9).

**Remote calls.** Every call to origin (`ls-remote`, `fetch`, the push) runs with
`remote_env`: the coordinator's environment minus `GIT_DIR`, `GIT_WORK_TREE`,
`GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT` and `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*`
(injected config such as `url.*.insteadOf`), plus `GIT_TERMINAL_PROMPT=0`. Global and system
config are kept on purpose (credential helpers); `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` stay
too, since the coordinator's own environment is not worker-writable, while the
worker-writable `~/.gitconfig` is covered by the URL rewrite check. `ls-remote` and `fetch`
time out after `REMOTE_TIMEOUT` (120 s), the push after `PUSH_TIMEOUT` (600 s); a timeout is
a clean error (exit 1), never a hang.

## 9. Held jobs after a ship

A held job belongs to the run it was built in. If that run ships without it, `/factory accept`
on it is refused ("run shipped"); `/factory rework <job-id>` moves it to the newest unshipped
run of the same repo (or starts a new run), sets its `run`, and its next round merges there.

A job held with kind `conflict` cannot be cleared by a plain rework: the squash merge uses the
job's `base` as merge base and builders never rebase, so the same lines collide again. Its
rework is allowed only once the jobs it conflicts with are in `D`, i.e. its run has shipped.
That holds for a conflict with `origin/D` alone too: until then the job's base is its run's
base and a restart changes nothing. `reassign` keeps the conflict hold and records the old run
as `moved_from`; then `factory_integrate.py rebase-job --job <id> --owner <o>` (exit 2 with a
reason on any refusal), under the run lock, requires the job and its run owned by `<o>`, stage
`held` with hold kind `conflict`, `job.run` started and not shipped or being shipped, a run
`base` different from `job.base` with `job.base` reachable from it, and the `moved_from` run
shipped with its `shipped.merge_sha` reachable from the run base. It runs rule 6 on the
worktree and repo, requires the worktree on `factory/<id>` and clean (`status --porcelain`
empty), records the old tip as `refs/heads/factory/<id>-r<round>`, runs `switch -C
factory/<id> <run base>` in the job worktree, sets `job.base` to the run base, clears
`review_head`, `review_round` and the hold, and records
`rebased {from_base, old_tip, backup_ref, round, at}` in `job.json`. `status` and `switch` run
with filter drivers disabled, `submodule.recurse=false`, `GIT_LFS_SKIP_SMUDGE=1` and a scrubbed
env, as the gate does, so a global filter a worker planted never runs. A retry after a crash
continues: an existing backup ref is accepted when the branch is still at it or already at the
run base; any other existing backup ref is refused (check it, then `update-ref -d` it). The
rework's builder gets round `rebased.round`'s `R/diff.patch` as the change to re-apply on the
new base, as new commits, only while `rebased.round` equals `job.round`.

## 10. Changes to the base spec, section by section

| Base § | Change |
|---|---|
| Header, §1, §2 | Delivery is the run branch + ship (this document) |
| §3 | Replaced by §3 here |
| §4 flow, steps 7–8 | Replaced by §5 here |
| §4.1, §4.2 | Replaced/extended by §7 here |
| §6.1 | ADO host/org check, `FACTORY_ADO_ORGS`, `az devops` and `AZURE_DEVOPS_EXT_PAT` items removed. `origin` must exist; its URL is recorded in `job.json` `origin_url` and checked at ship (rule 6 compares the worktree's `remote.origin.url` with it). The host/org allow-list stays available in `validate_origin` but is used only by `ship` |
| §6.2 | Rules 1–6 stay as the job guard used by `merge-job` (rules 2, 4, 6) and by ship (rules 4, 6). Branch pushes of `factory/*` are removed. New `ship` mode (§8 step 7). Backstop: per run, not per job (§6 step 3, §8 step 1) |
| §6.3 | Removed. `factory_pr_body.py` keeps `sanitize` and `report.md`; `pr.md`, the 4000-character cap and the PR thread are removed |
| §7.1 | `job.json`: remove `pr`; add `run`, `accepted` (optional), `merge_commit` (optional). Stages: `spec, build, review, verify, held, merged, shipped, blocked, failed, closed` (`pr`, `pr-draft`, `pr-ready` removed). Files: `pr.md`, `pr.json` removed |
| §7.2 | `finish`: `merged`/`shipped`/`closed` → `[x]`; `held` → `[!] … held`; `blocked`/`failed` → `[!]`. `after:` is satisfied when the named job is `merged` or `shipped` |
| §7.3 | Columns: Backlog, Spec, Build, Review, Verify, Held, Merged, Shipped, Blocked (`blocked`, `failed`). Cards show the run id; a run filter selects one run |
| §7.4 | `next_step`: `none` for `merged`, `shipped`, `closed`, `blocked`, `failed`; `held` → `none` (the user decides: accept or rework) |
| §8.3 | Overlap with `origin/D` still forces draft (now: held). Overlaps between jobs of the same run show up as `merge-job` conflicts |
| §11 | Tests per §11 here |
| §12 | ADO token and `az` items removed. New: ship pushes the default branch (by design, after confirmation) |

## 11. Tests

Unit (stdlib `unittest`, temporary repos with a local bare `origin`, no network):

- `start-run`: creates `factory-run/<id>` at `origin/D`; refuses an existing branch; records
  base and refs snapshot.
- `merge-job`: a ready job lands as exactly one commit on the run branch with the sanitised
  message and trailers; the job branch and the user's checkout are untouched; a draft job
  without `accepted` is refused; with `accepted` for the current round it merges; an
  `accepted` from an older round does not count; a secret in any job commit is refused even
  when accepted; a conflict exits 3 and leaves the run branch unchanged; a concurrent move of
  the run branch makes the compare-and-swap fail; no hook or filter configured in the repo runs.
- `ship`: refuses a run with no merged jobs; conflict with `origin/D` stops before any ref
  change; a failing `test:` command stops before local `D` moves and kills its process group on
  timeout; a secret in the run stops; diverged local `D` stops; `D` checked out with local
  changes that would be overwritten stops; `D` not checked out is moved by compare-and-swap;
  the push mode refuses a non-default branch, a non-fast-forward, a local `D` that is not
  `merge_sha`, and never emits `-f`/`--force*`/`--tags`/`--all`/`--mirror`/`--delete`.
- Push binding: no run file, a missing gate, a gate for another sha or a failed gate, a
  shipped run, or a run whose repo, origin url, default branch, merge or base differ → exit 2, origin
  unchanged; a symlinked run file or a bad run id is refused.
- Resume: prepare after local `D` moved refuses and makes no second merge; `ship-status`
  reports `origin_has_merge` true after a push whose coordinator died, still true when another
  clone advanced origin past the merge (not fetched locally), false before the push, null when
  origin cannot be read or a guard refuses (with `origin_error`); it changes no ref but
  `refs/remotes/origin/D`. `ship-finish` refuses a local `D` moved to an ungated merge.
- Run tip: a branch moved outside the factory refuses; a retry after a crash adopts its own
  squash. Review round: missing or stale `review_round` refuses with the rework message.
- URL rewrite: a global `insteadOf` or `pushInsteadOf` pointing origin's URL at another bare
  repo refuses `start-run`, `ship-prepare` and the push; so do `remote."<url>".url` and
  `.pushurl` in repo or global config; the other repo stays empty. Injected
  `GIT_CONFIG_*` env is ignored; remote timeouts are clean errors.
- Backlog `after:` waits on `held` and passes on `merged`; `finish` maps the new stages.
- `next_step` for the new stages; dashboard columns for `held`/`merged`/`shipped`.
- `rebase-job`: after a ship, reassign and rebase-job put a conflict-held job on the new run's
  base (backup ref at the old tip, clean worktree) and a re-applied commit merges; refusals for
  a non-conflict hold, a run not started or shipped, an equal base, an existing backup ref and
  a dirty worktree.
- Removed: tests that exist only for `pr.md`, the 4000-character cap, and PR stages.

Live (base Task 12, revised): a scratch repo with a remote the user is happy to have `main`
pushed to; `/factory run --jobs 2` with two non-overlapping items and one overlapping item
(expect one `held` by conflict), one `/factory accept`, one `/factory ship` with the push
answered "no" and then a second ship of a new run answered "yes". No `az` needed.

## 12. Risks

- **Ship pushes `main`.** This is the one outward-facing action; it needs the user's answer in
  the same command, a fast-forward, the secret scan and rule 6. Branch policies on `main` may
  reject it; that is reported, not worked around.
- **Accepted findings reach `main`.** `accept` records the reasons it overrode; the ship summary
  and merge commit list them.
- **The gate runs repository code** (install, test, build) with the user's privileges, as the
  builder and dev server already do; tokens are removed from its environment.
- **Config the factory does not own redirects origin.** Remote calls keep global config for
  credentials; a rewrite of the factory's origin URL (`insteadOf`, `pushInsteadOf`) is
  refused, but other global settings (e.g. a credential helper) are trusted by design.
- **A ship interrupted after the push.** Resume asks origin (`ship-status`) instead of
  pushing again; if origin cannot be read it stops rather than record `pushed: false`.
- **Long-lived run branches drift from `main`.** Ship merges with `origin/D` at ship time;
  conflicts stop the ship and are left to a human.
