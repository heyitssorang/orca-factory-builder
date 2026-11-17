#!/usr/bin/env python3
"""Run branches for the factory: start a run, squash-merge finished jobs into
factory-run/<run-id> with git plumbing (no checkout, hook or filter runs), hold drafts,
accept, reassign, status, and the gated ship of a run into the default branch.

Exit codes: 0 ok, 2 refused, 3 conflict, 1 error. JSON on stdout.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory_backlog as fb  # noqa: E402
import factory_job as fj  # noqa: E402
import factory_pr_body as fpb  # noqa: E402
import factory_push as fp  # noqa: E402
import factory_review as fr  # noqa: E402
import factory_schedule as fs  # noqa: E402

RUN_ID_RE = re.compile(r"\d{8}-\d{4}-[0-9a-f]{4}")
SUBJECT_CAP = 200
SCHEDULER_KEYS = ("caps", "pending", "running")
_DRIVER_RE = re.compile(r"merge\..+\.driver")
GATE_TIMEOUT = 1800
GATE_KEYS = ("install", "test", "build")
_AGENTS_LINE = re.compile(r"^(?:[-*]\s+)?(install|test|build):\s*(.*?)\s*$")
TAIL_LINES, TAIL_CHARS = 20, 2000


class Refused(Exception):
    """A request the rules do not allow; the CLI exits 2."""


class ShipRefused(Refused):
    """A ship step stopped; detail (if any) is printed as JSON by the CLI."""

    def __init__(self, reason: str, detail: dict | None = None):
        super().__init__(reason)
        self.detail = detail


@dataclass
class MergeResult:
    status: str  # merged | held | conflict | refused | error
    commit: str | None = None
    paths: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runs_dir(home: Path) -> Path:
    return home / "runs"


def new_run_id(now: datetime, rand4: str) -> str:
    return f"{now.strftime('%Y%m%d-%H%M')}-{rand4}"


def check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise fj.BadRequest("invalid run id")
    return run_id


def claim_run(home: Path, now: datetime | None = None) -> str:
    fj.prepare_home(home)
    d = runs_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        run_id = new_run_id(now or datetime.now(timezone.utc), secrets.token_hex(2))
        try:
            (d / f"{run_id}.lock").mkdir(mode=0o700)
        except FileExistsError:
            continue
        return run_id
    raise RuntimeError("could not claim a run id after 20 attempts")


def _run_path(home: Path, run_id: str) -> Path:
    return runs_dir(home) / f"{check_run_id(run_id)}.json"


def _read_run(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except RecursionError:
        raise ValueError("run file nested too deeply") from None
    if not isinstance(data, dict):
        raise ValueError("run file is not an object")
    return data


def load_run(home: Path, run_id: str) -> dict:
    return _read_run(_run_path(home, run_id))


def save_run(home: Path, run_id: str, data: dict, *, owner: str) -> None:
    """Whole-file rewrite (temp file + rename). Keys of the current file that data does not
    carry are kept; the scheduler's keys always come from the current file."""
    fj.check_owner(owner)
    path = _run_path(home, run_id)
    cur = _read_run(path) if path.exists() else {}
    if cur.get("owner") not in (None, owner):
        raise fj.OwnerMismatch(f"run owned by {cur.get('owner')!r}, not {owner!r}")
    out = {**cur, **{k: v for k, v in data.items() if k not in SCHEDULER_KEYS or k not in cur}}
    out["owner"] = owner
    _write_run(path, out)


def _write_run(path: Path, out: dict) -> None:
    out["updated"] = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(out, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _run_lock(home: Path, run_id: str):
    """Serialise read-modify-write of one run's file and branch across processes (the
    scheduler takes the same lock)."""
    return fs.run_lock(_run_path(home, run_id))


def takeover_run(home: Path, run_id: str, new_owner: str, *, reason: str) -> dict:
    """Transfer a run to a new owner (mirrors factory_job.takeover), e.g. on /factory resume."""
    fj.check_owner(new_owner)
    with _run_lock(home, run_id):
        path = _run_path(home, run_id)
        run = _read_run(path)
        now = _now()
        takeovers = run.get("takeovers") if isinstance(run.get("takeovers"), list) else []
        run["takeovers"] = takeovers + [{"from": run.get("owner"), "to": new_owner, "at": now,
                                         "reason": reason}]
        run["owner"] = new_owner
        _write_run(path, run)
        return run


def _owned_run(home: Path, run_id: str, owner: str) -> dict:
    run = load_run(home, run_id)
    if run.get("owner") != owner:
        raise fj.OwnerMismatch(f"run owned by {run.get('owner')!r}, not {owner!r}")
    return run


def _resolve(repo: Path, ref: str) -> str | None:
    p = fp._git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    return p.stdout.strip() if p.returncode == 0 else None


def _update_ref(repo: Path, ref: str, new: str, old: str) -> bool:
    """Compare-and-swap: move ref to new only if it is still old ("" = must not exist)."""
    return fp._git(repo, "update-ref", ref, new, old, check=False).returncode == 0


def _check_default(repo: Path, default_branch: str) -> None:
    if (not isinstance(default_branch, str) or default_branch.startswith("-") or fp._git(
            repo, "check-ref-format", f"refs/heads/{default_branch}", check=False).returncode):
        raise fj.BadRequest("invalid default branch")


def _is_local(run: dict) -> bool:
    """A local-mode run (no trusted origin): nothing fetches, snapshots or pushes."""
    return run.get("mode") == "local"


def _local_config_reasons(repo: Path) -> list[str]:
    """Merge drivers would run as commands inside merge-tree; rule 6 does not list them."""
    out = []
    for scope in ("--local", "--worktree"):
        for key, _ in fp._config_entries(repo, scope) or []:
            if _DRIVER_RE.fullmatch(key.lower()):
                out.append(f"rule 6: config {key.lower()}")
    return sorted(set(out))


def start_run(home: Path, run_id: str, *, repo: Path, default_branch: str,
              origin_url: str | None, owner: str, local: bool = False) -> dict:
    """Create the run branch at D's tip: the server's (fetched, with a remote-refs snapshot for
    the backstop) or, local, the local branch's."""
    repo = Path(repo).resolve()
    with _run_lock(home, run_id):
        path = _run_path(home, run_id)
        cur = _read_run(path) if path.exists() else {}
        if cur.get("owner") not in (None, owner):
            raise fj.OwnerMismatch(f"run owned by {cur.get('owner')!r}, not {owner!r}")
        if cur.get("branch"):
            raise Refused("run already started")
        if Path(home).name == fj.HOME_DIR and fj.repo_of(home) != repo:
            raise Refused(f"this factory home belongs to {fj.repo_of(home)}, not {repo}")
        _check_default(repo, default_branch)
        if local:
            reasons = (fp.config_check(repo, origin_url, local=True)
                       + _local_config_reasons(repo))
        else:
            reasons = (fp.config_check(repo, origin_url) + _local_config_reasons(repo)
                       or fp.rewrite_reasons(repo, origin_url))
        if reasons:
            raise Refused("; ".join(reasons))
        branch = f"factory-run/{run_id}"
        if _resolve(repo, f"refs/heads/{branch}"):
            raise Refused(f"branch {branch} exists")
        refs = None
        if local:
            base = _resolve(repo, f"refs/heads/{default_branch}")
            if not base:
                raise Refused(f"no local {default_branch}")
        else:
            # Anchor on the tip the server reports, not on refs/remotes/* (worker-writable).
            refs = fp.remote_refs(repo, origin_url)
            base = refs.get(f"refs/heads/{default_branch}")
            if not base or not fp.SHA_RE.fullmatch(base):
                raise Refused(f"origin has no {default_branch}")
            fp._git(repo, "fetch", "--no-tags", "--no-recurse-submodules", origin_url,
                    f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}",
                    env=fp.remote_env(), timeout=fp.REMOTE_TIMEOUT)
            if not fp.has_local_commit(repo, base):
                raise Refused(f"origin/{default_branch} tip not fetched")
        if not _update_ref(repo, f"refs/heads/{branch}", base, ""):
            raise Refused(f"branch {branch} exists")
        if refs is not None:
            snap = runs_dir(home) / f"{run_id}.refs-before.json"
            snap.write_text(json.dumps(refs, sort_keys=True))
        now = _now()
        run = {"id": run_id, "repo": str(repo), "default_branch": default_branch,
               "origin_url": origin_url, "mode": "local" if local else "ado", "branch": branch,
               "base": base, "owner": owner, "merged": [], "held": [], "shipped": None,
               "created": now, "updated": now}
        save_run(home, run_id, run, owner=owner)
        return load_run(home, run_id)


# --- jobs ---------------------------------------------------------------------

def _job_dir(home: Path, job_id: str) -> Path:
    return fj.jobs_dir(home) / fj.check_job_id(job_id)


def mark_review(home: Path, job_id: str, *, owner: str) -> str:
    """Record the factory/<id> tip the reviews of this round looked at, and the round; only
    that commit, in that round, can be merged."""
    jd = _job_dir(home, job_id)
    job = fj.read_job(jd)
    if not job.get("repo"):
        raise Refused("job has no repo")
    head = _resolve(Path(job["repo"]), f"refs/heads/factory/{job_id}")
    if not head:
        raise Refused(f"branch factory/{job_id} not found")
    fj.update_job(jd, {"review_head": head, "review_round": job.get("round")}, owner=owner)
    return head


def _round(job: dict) -> int:
    n = job.get("round")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise Refused("job has no review round")
    return n


def _overlaps_default(job: dict, default_branch: str) -> list[str]:
    out = []
    for o in job.get("overlaps") or []:
        if isinstance(o, dict) and o.get("with") in (f"origin/{default_branch}",
                                                     f"local/{default_branch}"):
            out += [str(p) for p in o.get("paths") or []]
    return out


def _decide(job_dir: Path, job: dict, run: dict) -> tuple[list[str], bool]:
    """(draft reasons now, whether an accept of the current round covers all of them)."""
    n = _round(job)
    reasons = fr.draft_reasons(fr.load_round(job_dir, n),
                               _overlaps_default(job, run["default_branch"]))
    acc = job.get("accepted")
    ok = (isinstance(acc, dict) and acc.get("round") == n and isinstance(acc.get("reasons"), list)
          and set(reasons) <= set(acc["reasons"]))
    return reasons, ok


def _shipping(run: dict) -> bool:
    """Local D already holds (or was recorded as holding) the ship merge: a job merged into the
    run now would end up merged in a shipped run, outside the ship merge."""
    ship = run.get("ship") if isinstance(run.get("ship"), dict) else {}
    if ship.get("local"):
        return True
    m, d = ship.get("merge_sha"), run.get("default_branch")
    if not (_sha(m) and isinstance(d, str) and run.get("repo")):
        return False
    try:
        repo = Path(run["repo"])
        if fp._git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{d}",
                   check=False).returncode or not fp.reachable_from(repo, m, m):
            return True  # D or the merge cannot be resolved: fail closed
        return fp.reachable_from(repo, m, f"refs/heads/{d}")
    except (fp.GitError, OSError, ValueError):
        return True


def _preconditions(run: dict, job: dict, run_id: str) -> list[str]:
    out = []
    if not run.get("branch") or not run.get("base"):
        out.append("run not started")
    if run.get("shipped"):
        out.append("run shipped")
    elif _shipping(run):
        out.append("run is being shipped")
    if job.get("run") != run_id:
        out.append(f"job belongs to run {job.get('run')}")
    if job.get("stage") in ("merged", "shipped", "closed"):
        out.append(f"job is {job.get('stage')}")
    if any(isinstance(m, dict) and m.get("job") == job.get("id") for m in run.get("merged") or []):
        out.append("job already merged into this run")
    try:
        same = Path(job.get("repo") or "").resolve() == Path(run.get("repo") or "").resolve()
    except (OSError, ValueError):
        same = False
    if not job.get("repo") or not same:
        out.append("job repo differs from run repo")
    return out


def _clean(job: dict, s: str, limit: int | None = None) -> str:
    s = re.sub(r"[\x00-\x1f\x7f]", " ", fpb._clean(job, str(s)))
    s = re.sub(r"\s+", " ", s).strip()
    return fpb._cut(s, limit) if limit else s


def _message(job: dict, rd, n: int) -> str:
    subject = fpb._cut(f"{job['id']}: {_clean(job, job.get('feature') or '')}".rstrip(),
                       SUBJECT_CAP)
    verdicts = ", ".join(f"{lens} {rd.reviews[lens].verdict}" for lens in fr.LENSES)
    confirmed = sum(1 for (lens, k), v in rd.verifies.items() if v == "CONFIRMED"
                    and any(f.n == k for f in rd.reviews[lens].findings))
    risk = rd.risk.replace("﻿", "").strip().split("\n", 1)[0] if rd.risk.strip() else ""
    body = [f"Reviews: {verdicts}", f"Confirmed findings: {confirmed}",
            f"Risk: {_clean(job, risk, 200) or 'no risk report'}"]
    acc = job.get("accepted")
    if isinstance(acc, dict) and acc.get("round") == n:
        body.append(f"Accepted by {_clean(job, acc.get('by') or '', 80)} over "
                    f"{len(acc.get('reasons') or [])} reason(s)")
    return (subject + "\n\n" + "\n".join(body) + "\n\n"
            f"Factory-Job: {job['id']}\nFactory-Round: {n}\n")


def _conflict_with(repo: Path, run: dict, paths: list[str]) -> list[str]:
    who = []
    for m in run.get("merged") or []:
        c = m.get("commit") if isinstance(m, dict) else None
        if not isinstance(c, str) or not fp.SHA_RE.fullmatch(c):
            continue
        p = fp._git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                    "--no-renames", c, check=False, raw=True)
        if set(p.stdout.split("\0")) & set(paths):
            who.append(str(m.get("job")))
    return who


def _own_squash(repo: Path, tip: str, parent: str, tree: str, job_id: str, n: int) -> bool:
    """tip is exactly the squash _merge would write now: one parent (the expected run tip),
    the same tree, and Factory-Job/Factory-Round trailers naming this job and round."""
    p = fp._git(repo, "show", "-s", "--format=%P%n%T%n%(trailers:only,unfold)", tip,
                check=False)
    out = p.stdout.split("\n") if p.returncode == 0 else []
    if len(out) < 2 or out[0].split() != [parent] or out[1] != tree:
        return False
    trailers = [x.partition(": ") for x in out[2:] if x]
    return (sorted((k, v) for k, _, v in trailers if k in ("Factory-Job", "Factory-Round"))
            == [("Factory-Job", job_id), ("Factory-Round", str(n))])


def _merge(home: Path, run_id: str, job_id: str, owner: str) -> MergeResult:
    """Spec §6 steps 1-7. Caller holds the run lock. Writes nothing unless it merged."""
    run = _owned_run(home, run_id, owner)
    jd = _job_dir(home, job_id)
    job = fj.read_job(jd)
    if job.get("owner") != owner:
        raise fj.OwnerMismatch(f"job owned by {job.get('owner')!r}, not {owner!r}")
    reasons = _preconditions(run, job, run_id)
    if reasons:
        return MergeResult("refused", reasons=reasons)
    n = _round(job)
    draft, accepted = _decide(jd, job, run)
    if draft and not accepted:
        return MergeResult("refused", reasons=["draft not accepted for this round"] + draft)

    repo, d, url = Path(run["repo"]), run["default_branch"], run["origin_url"]
    base, branch = job.get("base"), f"factory/{job_id}"
    if job.get("origin_url") not in (None, url):
        return MergeResult("refused", reasons=["rule 6: job origin url differs from run"])
    if not isinstance(base, str) or not fp.SHA_RE.fullmatch(base):
        return MergeResult("refused", reasons=["rule 3: invalid base"])
    if job.get("branch") != branch or not job.get("worktree"):
        return MergeResult("refused", reasons=["job has no factory branch worktree"])
    reviewed = job.get("review_head")
    if not isinstance(reviewed, str) or not fp.SHA_RE.fullmatch(reviewed):
        return MergeResult("refused", reasons=["review head not recorded"])
    if job.get("review_round") is None:
        return MergeResult("refused", reasons=["review round not recorded; rework the job"])
    if job.get("review_round") != n:
        return MergeResult("refused",
                           reasons=["review head is from another round; rework the job"])
    try:
        # Hard rules (2, 4, 6 and the rest of the job guard); accept cannot override them.
        local = _is_local(run)
        reasons = (fp.config_check(repo, url, local=local) + _local_config_reasons(repo)
                   or ([] if local else fp.rewrite_reasons(repo, url)))
        if reasons:
            return MergeResult("refused", reasons=reasons)
        reasons, head = fp._check(Path(job["worktree"]), branch=branch, base=base,
                                  default_branch=d, origin_url=url, local=local)
        if reasons:
            return MergeResult("refused", reasons=reasons)
        if head != reviewed or _resolve(repo, f"refs/heads/{branch}") != reviewed:
            return MergeResult("refused", reasons=["branch moved since review"])
        if not local:
            # Backstop: nothing of ours reached the remote outside the guard since the run began.
            before = json.loads((runs_dir(home) / f"{run_id}.refs-before.json").read_text())
            if not isinstance(before, dict):
                raise ValueError("refs snapshot is not an object")
            after = fp.remote_refs(repo, url)
            bad = fp.classify_snapshots(repo, before, after, [], d)["suspicious"]
            if bad:
                return MergeResult("refused", reasons=[f"backstop: suspicious change: {r}"
                                                       for r in bad])
        ref = f"refs/heads/{run['branch']}"
        tip = _resolve(repo, ref)
        if not tip:
            return MergeResult("error", reasons=["run branch missing"])
        merged = run.get("merged") or []
        expected = merged[-1].get("commit") if merged and isinstance(merged[-1], dict) else (
            None if merged else run["base"])
        if not _sha(expected):
            return MergeResult("error", reasons=["run file merged list is malformed"])
        mt = fp._git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                     "--merge-base", base, expected, head, check=False)
        lines = mt.stdout.splitlines()
        tree = lines[0] if mt.returncode == 0 and lines and _sha(lines[0]) else None
        if tip != expected:
            # Only a squash of this job and round that we wrote before a crash (CAS done, run
            # file not) is adopted; anything else moved the branch behind the factory's back.
            if not (tree and _own_squash(repo, tip, expected, tree, job_id, n)):
                return MergeResult("refused", reasons=["run branch moved outside the factory"])
            new = tip
        elif mt.returncode == 1:
            paths = list(dict.fromkeys(x for x in lines[1:] if x))
            return MergeResult("conflict", paths=paths, reasons=[
                f"conflicts with {', '.join(_conflict_with(repo, run, paths)) or 'run branch'}"
                f" in {', '.join(paths)}"])
        elif not tree:
            return MergeResult("error", reasons=["merge-tree failed"])
        else:
            msg = _message(job, fr.load_round(jd, n), n)
            fd, tmp = tempfile.mkstemp(prefix="factory-msg-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(msg)
                new = fp._git(repo, "commit-tree", "--no-gpg-sign", tree, "-p", tip,
                              "-F", tmp).stdout.strip()
            finally:
                os.unlink(tmp)
            if not fp.SHA_RE.fullmatch(new) or not _update_ref(repo, ref, new, tip):
                return MergeResult("error", reasons=["run branch moved (compare-and-swap failed)"])
    except (fp.GitError, OSError, ValueError, RecursionError) as e:
        return MergeResult("error", reasons=[f"error: {type(e).__name__}"])
    run["merged"] = list(run.get("merged") or []) + [{"job": job_id, "commit": new}]
    run["held"] = [h for h in run.get("held") or [] if not (isinstance(h, dict)
                                                           and h.get("job") == job_id)]
    save_run(home, run_id, run, owner=owner)
    fj.update_job(jd, {"stage": "merged", "merge_commit": new, "hold": None}, owner=owner)
    return MergeResult("merged", commit=new)


def merge_job(home: Path, run_id: str, job_id: str, *, owner: str) -> MergeResult:
    with _run_lock(home, run_id):
        return _merge(home, run_id, job_id, owner)


def _hold(home: Path, run_id: str, job_id: str, owner: str, hold: dict) -> None:
    run = _owned_run(home, run_id, owner)
    held = [h for h in run.get("held") or [] if not (isinstance(h, dict)
                                                    and h.get("job") == job_id)]
    entry = {"job": job_id, "kind": hold["kind"], "reasons": hold["reasons"]}
    if "paths" in hold:
        entry["paths"] = hold["paths"]
    run["held"] = held + [entry]
    save_run(home, run_id, run, owner=owner)
    fj.update_job(_job_dir(home, job_id), {"stage": "held", "hold": hold}, owner=owner)


def _hold_refused(home: Path, run_id: str, run: dict, job: dict, owner: str,
                  reasons: list[str]) -> None:
    """A precondition refusal still ends the job (spec §5: exit 2 -> held). A job that is
    already merged, shipped or closed is left alone; the run's held[] gets an entry only when
    the job belongs to that run, the run is started, unshipped and owned by the caller."""
    if job.get("stage") in ("merged", "shipped", "closed") or any(
            isinstance(m, dict) and m.get("job") == job.get("id") for m in run.get("merged") or []):
        return
    hold = {"kind": "refused", "round": job.get("round"), "reasons": reasons}
    if (job.get("run") == run_id and run.get("branch") and not run.get("shipped")
            and run.get("owner") == owner):
        _hold(home, run_id, job["id"], owner, hold)
    else:
        fj.update_job(_job_dir(home, job["id"]), {"stage": "held", "hold": hold}, owner=owner)


def finish_job(home: Path, run_id: str, job_id: str, *, owner: str) -> MergeResult:
    """Spec §5: hold a draft (no current-round accept), else merge and record the outcome."""
    with _run_lock(home, run_id):
        path = _run_path(home, run_id)
        run = _read_run(path) if path.exists() else {}
        if run.get("owner") not in (None, owner):
            raise fj.OwnerMismatch(f"run owned by {run.get('owner')!r}, not {owner!r}")
        jd = _job_dir(home, job_id)
        job = fj.read_job(jd)
        if job.get("owner") != owner:
            raise fj.OwnerMismatch(f"job owned by {job.get('owner')!r}, not {owner!r}")
        reasons = _preconditions(run, job, run_id)
        if reasons:
            _hold_refused(home, run_id, run, dict(job, id=job.get("id") or job_id), owner,
                          reasons)
            return MergeResult("refused", reasons=reasons)
        n = _round(job)
        draft, accepted = _decide(jd, job, run)
        if draft and not accepted:
            _hold(home, run_id, job_id, owner, {"kind": "draft", "round": n, "reasons": draft})
            return MergeResult("held", reasons=draft)
        r = _merge(home, run_id, job_id, owner)
        if r.status == "conflict":
            _hold(home, run_id, job_id, owner, {"kind": "conflict", "round": n,
                                                 "reasons": r.reasons, "paths": r.paths})
        elif r.status == "refused":
            _hold(home, run_id, job_id, owner, {"kind": "refused", "round": n,
                                                 "reasons": r.reasons})
        elif r.status == "error":
            fj.update_job(jd, {"stage": "failed", "hold": None,
                               "reason": "merge failed: " + "; ".join(r.reasons or ["unknown error"])},
                         owner=owner)
        return r


def accept(home: Path, job_id: str, *, owner: str, by: str, note: str) -> dict:
    jd = _job_dir(home, job_id)
    job = fj.read_job(jd)
    hold = job.get("hold") if isinstance(job.get("hold"), dict) else {}
    if job.get("stage") != "held":
        raise Refused("job is not held")
    if hold.get("kind") != "draft":
        raise Refused(f"a {hold.get('kind')} hold cannot be accepted")
    if hold.get("round") != job.get("round"):
        raise Refused("hold is from an older round")
    run = load_run(home, check_run_id(job.get("run") or ""))
    if run.get("shipped"):
        raise Refused("run shipped")
    acc = {"by": by, "at": _now(), "round": job["round"], "note": note,
           "reasons": list(hold.get("reasons") or [])}
    return fj.update_job(jd, {"accepted": acc}, owner=owner)


def reassign(home: Path, job_id: str, run_id: str, *, owner: str,
             force_move: bool = False) -> dict:
    """Move a held/blocked/failed job to another unshipped run of the same repo (spec §9)."""
    jd = _job_dir(home, job_id)
    job = fj.read_job(jd)
    if job.get("stage") not in ("held", "blocked", "failed"):
        raise Refused(f"job is {job.get('stage')}")
    target = load_run(home, run_id)
    if target.get("shipped") or not target.get("branch"):
        raise Refused("target run is shipped or not started")
    if Path(job.get("repo") or "").resolve() != Path(target.get("repo") or "").resolve():
        raise Refused("target run is for another repo")
    old = job.get("run")
    if old == run_id:
        raise Refused("job already belongs to that run")
    if old:
        with _run_lock(home, old):
            prev = load_run(home, old)
            if not prev.get("shipped") and not force_move:
                raise Refused("job's run is not shipped (use --force-move)")
            held = prev.get("held") or []
            keep = [h for h in held if not (isinstance(h, dict) and h.get("job") == job_id)]
            # Only the run's owner writes its file; another owner's held[] is left stale
            # (run_status filters it by the job's current run).
            if keep != held and prev.get("owner") == owner:
                prev["held"] = keep
                save_run(home, old, prev, owner=owner)
    # A conflict hold is kept: rebase-job needs it to restart the branch on the new run's base.
    hold = job.get("hold") if isinstance(job.get("hold"), dict) else None
    keep_hold = hold if hold and hold.get("kind") == "conflict" else None
    return fj.update_job(jd, {"run": run_id, "hold": keep_hold, "accepted": None,
                                 "review_head": None, "review_round": None,
                                 "moved_from": old}, owner=owner)


def rebase_job(home: Path, job_id: str, *, owner: str) -> dict:
    """Restart a conflict-held job's factory/<id> on its new run's base (spec §9): the old tip
    is kept as refs/heads/factory/<id>-r<round>; the rework re-applies the change. Safe to
    re-run after a crash between the backup, the switch and the job record."""
    jd = _job_dir(home, job_id)
    job = fj.read_job(jd)
    if job.get("owner") != owner:
        raise fj.OwnerMismatch(f"job owned by {job.get('owner')!r}, not {owner!r}")
    hold = job.get("hold") if isinstance(job.get("hold"), dict) else {}
    if job.get("stage") != "held" or hold.get("kind") != "conflict":
        raise Refused("job is not under a conflict hold")
    n = _round(job)
    run_id = check_run_id(job.get("run") or "")
    with _run_lock(home, run_id):
        run = _owned_run(home, run_id, owner)
        if not run.get("branch") or not _sha(run.get("base")):
            raise Refused("job's run is not started")
        if run.get("shipped") or _shipping(run):
            raise Refused("job's run is shipped or being shipped")
        base, new_base = job.get("base"), run["base"]
        if base == new_base:
            raise Refused("job base already equals the run base")
        try:
            same = Path(job.get("repo") or "").resolve() == Path(run.get("repo") or "").resolve()
        except (OSError, ValueError):
            same = False
        branch = f"factory/{job_id}"
        if not job.get("repo") or not same:
            raise Refused("job repo differs from run repo")
        if job.get("branch") != branch or not job.get("worktree"):
            raise Refused("job has no factory branch worktree")
        repo, wt, url = Path(run["repo"]), Path(job["worktree"]), run["origin_url"]
        if job.get("origin_url") not in (None, url):
            raise Refused("rule 6: job origin url differs from run")
        reasons = (fp.config_check(wt, url, local=_is_local(run))
                   + _local_config_reasons(repo))
        if reasons:
            raise Refused("; ".join(reasons))
        # The jobs it conflicted with must be in the new base: its previous run shipped and
        # that ship merge is reachable from the run base.
        prev_id, merge = job.get("moved_from"), None
        if isinstance(prev_id, str) and RUN_ID_RE.fullmatch(prev_id):
            prev = load_run(home, prev_id)
            shipped = prev.get("shipped") if isinstance(prev.get("shipped"), dict) else {}
            merge = shipped.get("merge_sha")
        if not fp.reachable_from(repo, merge, new_base):
            raise Refused("the conflicting jobs are not in the new base yet")
        if not _sha(base) or not fp.reachable_from(repo, base, new_base):
            raise Refused("job base is not older than the run base")
        # Worker-set global filters and attributes (outside rule 6) must not run here.
        safe = [*_filter_overrides(repo), "-c", "submodule.recurse=false"]
        env = dict(fr.scrubbed_env(), GIT_LFS_SKIP_SMUDGE="1")
        if fp._git(wt, "symbolic-ref", "--short", "-q", "HEAD",
                   check=False).stdout.strip() != branch:
            raise Refused(f"worktree is not on {branch}")
        if fp._git(wt, *safe, "status", "--porcelain", "--untracked-files=all",
                   env=env).stdout.strip():
            raise Refused("worktree not clean")
        tip = _resolve(repo, f"refs/heads/{branch}")
        if not tip:
            raise Refused(f"branch {branch} not found")
        backup = f"refs/heads/{branch}-r{n}"
        old_tip = _resolve(repo, backup)
        if old_tip is None:
            if not _update_ref(repo, backup, tip, ""):
                raise Refused(f"backup ref {backup} exists")
            old_tip = tip
        elif tip not in (old_tip, new_base):
            raise Refused(f"backup ref {backup} exists (remove it after checking it is not "
                          "needed)")
        if tip != new_base:
            fp._git(wt, *safe, "switch", "-q", "-C", branch, new_base, env=env)
        if _resolve(repo, f"refs/heads/{branch}") != new_base:
            raise Refused(f"{branch} did not move to the run base")
        rebased = {"from_base": base, "old_tip": old_tip, "backup_ref": backup, "round": n,
                   "at": _now()}
        job = fj.update_job(jd, {"base": new_base, "review_head": None, "review_round": None,
                                 "hold": None, "rebased": rebased}, owner=owner)
    return {k: job.get(k) for k in ("id", "run", "base", "branch", "review_head",
                                    "review_round", "rebased")}


def after_base(home: Path, run_id: str, dep_job_id: str) -> str | None:
    """Commit a job that must follow `dep_job_id` (backlog `after:`) is based on.
    The default ref when the dependency is shipped (it is in the default branch). The run
    branch tip when the dependency is merged into this run but not shipped: its code lives only
    there, and a job based on the default ref would build without it. Anything else is refused:
    a dependency that is not merged, or is merged into another unshipped run, cannot be built on."""
    dep = fj.read_job(_job_dir(home, dep_job_id))
    stage = dep.get("stage")
    if stage == "shipped":
        return None
    if stage != "merged":
        raise Refused(f"dependency {dep_job_id} is {stage}, not merged")
    if dep.get("run") != run_id:
        raise Refused(f"dependency {dep_job_id} is merged into run {dep.get('run')}, "
                      f"not shipped; ship it or put this job in that run")
    run = load_run(home, run_id)
    tip = _resolve(Path(run["repo"]), f"refs/heads/{run['branch']}")
    if not tip:
        raise Refused(f"run branch {run.get('branch')} is missing")
    return tip


def run_status(home: Path, run_id: str) -> dict:
    run = load_run(home, run_id)
    held = []
    for h in run.get("held") or []:
        try:
            job = fj.read_job(_job_dir(home, str(h.get("job"))))
        except (OSError, ValueError):
            continue
        if job.get("run") == run_id and job.get("stage") == "held":
            held.append(h)
    ahead = None
    if run.get("repo") and run.get("branch") and run.get("base"):
        p = fp._git(Path(run["repo"]), "rev-list", "--count",
                    f"{run['base']}..refs/heads/{run['branch']}", check=False)
        ahead = int(p.stdout.strip()) if p.returncode == 0 else None
    return {"id": run_id, "branch": run.get("branch"), "base": run.get("base"),
            "merged": run.get("merged") or [], "held": held, "shipped": run.get("shipped"),
            "ahead": ahead}


# --- ship ---------------------------------------------------------------------

def read_agents_commands(repo: Path, ref: str) -> dict[str, str]:
    """install:/test:/build: lines of AGENTS.md at ref (first of each wins; a bullet and
    surrounding backticks are dropped). No AGENTS.md there -> {}."""
    if not isinstance(ref, str) or ref.startswith("-"):
        raise fj.BadRequest("invalid ref")
    p = fp._git(repo, "show", f"{ref}:AGENTS.md", check=False, raw=True)
    out: dict[str, str] = {}
    if p.returncode != 0:
        return out
    for line in p.stdout.split("\n"):
        m = _AGENTS_LINE.match(line.strip())
        if not m or m.group(1) in out:
            continue
        cmd = m.group(2)
        if len(cmd) >= 2 and cmd[0] == cmd[-1] == "`":
            cmd = cmd[1:-1].strip()
        if cmd:
            out[m.group(1)] = cmd
    return out


def _sha(x) -> bool:
    return isinstance(x, str) and bool(fp.SHA_RE.fullmatch(x))


def _not_shipped(run: dict) -> None:
    if run.get("shipped"):
        raise ShipRefused("already shipped")


def _guard(repo: Path, url: str | None, *where: Path, remote: bool = False,
           local: bool = False) -> None:
    """Rule 6 on repo and where; remote=True (before any call to origin) also refuses an
    origin URL that some config scope rewrites. local: a local-mode run's rules."""
    reasons = []
    for w in (repo, *where):
        reasons += fp.config_check(w, url, local=local) + _local_config_reasons(w)
    if remote and not local and not reasons:
        reasons = fp.rewrite_reasons(repo, url)
    if reasons:
        raise ShipRefused("; ".join(sorted(set(reasons))))


def _prepared(repo: Path, run: dict) -> dict:
    """The ship recorded by ship-prepare, checked against the repo: the merge commit has
    parents (server tip, run tip) and the run branch has not moved since."""
    ship = run.get("ship")
    if not isinstance(ship, dict) or not all(_sha(ship.get(k))
                                             for k in ("merge_sha", "base", "run_tip")):
        raise ShipRefused("ship not prepared")
    m = ship["merge_sha"]
    parents = fp._git(repo, "rev-list", "--parents", "-n", "1", m, check=False).stdout.split()
    if (parents[1:] != [ship["base"], ship["run_tip"]]
            or _resolve(repo, f"refs/heads/{run.get('branch')}") != ship["run_tip"]):
        raise ShipRefused("run changed since ship-prepare; prepare again")
    return ship


def _ship_message(home: Path, run_id: str, run: dict) -> str:
    lines = []
    for m in run["merged"]:
        jid = str(m.get("job"))
        try:
            job = fj.read_job(_job_dir(home, jid))
        except (OSError, ValueError):
            lines.append(_clean({}, jid, SUBJECT_CAP))
            continue
        acc = job.get("accepted")
        mark = (" (accepted)" if isinstance(acc, dict) and acc.get("round") == job.get("round")
                else "")
        lines.append(fpb._cut(f"{jid}: {_clean(job, job.get('feature') or '')}".rstrip(),
                              SUBJECT_CAP) + mark)
    return f"factory: ship run {run_id}\n\n" + "\n".join(lines) + "\n"


def _origin_has_merge(repo: Path, run: dict, m) -> tuple[bool | None, str | None]:
    """(whether origin's D holds merge m, why that is unknown). Holds: its server tip is m or
    descends from it. A tip this repo lacks (origin moved on past a push) is fetched first; the
    fetched object must be that tip. None (with the reason) when origin cannot be read or the
    guards refuse."""
    d, url = run.get("default_branch"), run.get("origin_url")
    if not _sha(m):
        return None, "ship not prepared"
    if _is_local(run):
        return None, "local run: no origin"
    if not (isinstance(url, str) and fp.validate_origin(url, fp.allowed_orgs())):
        return None, "rule 6: origin url not allowed"
    try:
        _guard(repo, url, remote=True)
        tip = fp.server_head(url, d)[1]
        if tip is None:
            return False, None
        if tip == m:
            return True, None
        if not fp.has_local_commit(repo, tip):
            fp._git(repo, "fetch", "--no-tags", "--no-recurse-submodules", url,
                    f"+refs/heads/{d}:refs/remotes/origin/{d}", env=fp.remote_env(),
                    timeout=fp.REMOTE_TIMEOUT)
            if not fp.has_local_commit(repo, tip):
                return None, f"origin/{d} tip not fetched"
        return fp.reachable_from(repo, m, tip), None
    except (Refused, fp.GitError) as e:
        return None, str(e) or type(e).__name__
    except OSError as e:
        return None, f"error: {type(e).__name__}"


def _moved_already(repo: Path, run: dict) -> None:
    """A ship whose step 5 already moved local D resumes at the push/finish steps; preparing
    again would replace merge_sha and wedge them (or merge a second time after a push)."""
    d = run["default_branch"]
    ship = run.get("ship") if isinstance(run.get("ship"), dict) else {}
    m = ship.get("merge_sha")
    if not ship.get("local") and not (_sha(m) and fp.reachable_from(repo, m, f"refs/heads/{d}")):
        return
    detail = {k: ship.get(k) for k in ("merge_sha", "base", "run_tip", "local")}
    detail["origin_has_merge"], err = _origin_has_merge(repo, run, m)
    if err:
        detail["origin_error"] = err
    raise ShipRefused(f"local {d} already moved to the ship merge; continue with push or "
                      "ship-finish", detail)


def ship_status(home: Path, run_id: str) -> dict:
    """Read-only (no owner needed): where a prepared ship stands, for resuming it. local: local
    D contains merge_sha; gate: the gate status recorded for this merge_sha (else None);
    origin_has_merge: see _origin_has_merge; origin_error (only when it is None): why."""
    run = load_run(home, run_id)
    ship = run.get("ship") if isinstance(run.get("ship"), dict) else {}
    m = ship.get("merge_sha")
    if not _sha(m) or not run.get("repo"):
        raise ShipRefused("ship not prepared")
    repo, d = Path(run["repo"]), run.get("default_branch")
    _check_default(repo, d)
    gate = ship.get("gate")
    has, err = _origin_has_merge(repo, run, m)
    out = {"mode": "local" if _is_local(run) else "ado",
           "merge_sha": m, "base": ship.get("base"),
           "local": fp.reachable_from(repo, m, f"refs/heads/{d}"),
           "gate": (gate.get("status") if isinstance(gate, dict) and gate.get("merge_sha") == m
                    else None),
           "origin_has_merge": has}
    if has is None:
        out["origin_error"] = err
    return out


def ship_prepare(home: Path, run_id: str, *, owner: str) -> dict:
    """Spec §8 steps 1-2: merge the run onto the server tip of D (local run: local D's tip) as
    one merge commit, recorded in the run file. Moves no branch."""
    with _run_lock(home, run_id):
        run = _owned_run(home, run_id, owner)
        _not_shipped(run)
        merged = run.get("merged") or []
        if not run.get("branch") or not _sha(run.get("base")):
            raise ShipRefused("run not started")
        if not merged:
            raise ShipRefused("no merged jobs")
        if not all(isinstance(m, dict) and _sha(m.get("commit")) for m in merged):
            raise ShipRefused("run file merged list is malformed")
        repo, d, url = Path(run["repo"]), run["default_branch"], run["origin_url"]
        local = _is_local(run)
        _check_default(repo, d)
        _guard(repo, url, remote=True, local=local)
        _moved_already(repo, run)
        if local:
            tip = _resolve(repo, f"refs/heads/{d}")
            if not tip:
                raise ShipRefused(f"no local {d}")
            onto = d
        else:
            before = json.loads((runs_dir(home) / f"{run_id}.refs-before.json").read_text())
            if not isinstance(before, dict):
                raise ValueError("refs snapshot is not an object")
            bad = fp.classify_snapshots(repo, before, fp.remote_refs(repo, url), [],
                                        d)["suspicious"]
            if bad:
                raise ShipRefused("; ".join(f"backstop: suspicious change: {r}" for r in bad))
            # Anchor on the tip the server reports, never refs/remotes/* (worker-writable).
            head_ref, tip = fp.server_head(url, d)
            if head_ref != f"refs/heads/{d}":
                raise ShipRefused(f"{d} is not origin's default branch")
            if not _sha(tip):
                raise ShipRefused(f"origin has no {d}")
            fp._git(repo, "fetch", "--no-tags", "--no-recurse-submodules", url,
                    f"+refs/heads/{d}:refs/remotes/origin/{d}", env=fp.remote_env(),
                    timeout=fp.REMOTE_TIMEOUT)
            if not fp.has_local_commit(repo, tip):
                raise ShipRefused(f"origin/{d} tip not fetched")
            onto = f"origin/{d}"
        run_tip = _resolve(repo, f"refs/heads/{run['branch']}")
        own = fp._git(repo, "rev-list", "--reverse", f"{run['base']}..{run_tip}",
                      check=False) if run_tip else None
        if not own or own.returncode or own.stdout.split() != [m["commit"] for m in merged]:
            raise ShipRefused("run branch holds commits other than its merged jobs")
        mt = fp._git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                     tip, run_tip, check=False)
        lines = mt.stdout.splitlines()
        if mt.returncode == 1:
            paths = list(dict.fromkeys(x for x in lines[1:] if x))
            raise ShipRefused(f"conflict with {onto} in {', '.join(paths)} (merged jobs: "
                              f"{', '.join(_conflict_with(repo, run, paths)) or 'none'})")
        if mt.returncode != 0 or not lines or not _sha(lines[0]):
            raise fp.GitError("git merge-tree failed")
        fd, tmp = tempfile.mkstemp(prefix="factory-msg-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_ship_message(home, run_id, run))
            new = fp._git(repo, "commit-tree", "--no-gpg-sign", lines[0], "-p", tip,
                          "-p", run_tip, "-F", tmp).stdout.strip()
        finally:
            os.unlink(tmp)
        if not _sha(new):
            raise fp.GitError("git commit-tree failed")
        run["ship"] = {"merge_sha": new, "base": tip, "run_tip": run_tip}
        save_run(home, run_id, run, owner=owner)
        files = fp._git(repo, "diff", "--name-only", "-z", *fp._DIFF_FLAGS, tip, new,
                        raw=True).stdout
        moved = fp._git(repo, "rev-list", "--count", f"{run['base']}..{tip}", check=False)
        return {**run["ship"], "jobs": [m["job"] for m in merged],
                "commits": len(merged), "files": [x for x in files.split("\0") if x],
                "run_base": run["base"],
                "origin_moved": int(moved.stdout.strip()) if moved.returncode == 0 else None}


def _tail(path: Path) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 64 * 1024))
        text = f.read().decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-TAIL_LINES:])[-TAIL_CHARS:]


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _gate_cmd(cmd: str, cwd: Path, log: Path, tmpdir: Path) -> dict:
    """One AGENTS.md command as a shell line in its own process group, scrubbed env with TMPDIR
    = tmpdir, no stdin; the group is killed when it times out and after it exits (stray
    background children)."""
    with open(log, "wb") as out:
        p = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=cwd,
                             env=dict(fr.scrubbed_env(), TMPDIR=str(tmpdir)),
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True)
        try:
            rc = p.wait(timeout=GATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            _killpg(p.pid)
            p.wait()
            return {"exit": None, "tail": _tail(log), "status": "timeout"}
        finally:
            _killpg(p.pid)
    return {"exit": rc, "tail": _tail(log), "status": "pass" if rc == 0 else "fail"}


def _filter_overrides(repo: Path) -> list[str]:
    """-c options that blank every filter driver configured in any scope (global/system ones,
    e.g. git-lfs, survive rule 6), so checking out a worker-controlled tree runs none."""
    p = fp._git(repo, "config", "--null", "--get-regexp", r"^filter\.", check=False, raw=True)
    if p.returncode not in (0, 1):
        raise fp.GitError("git config failed")
    out = []
    for name in sorted({e.partition("\n")[0][7:].rpartition(".")[0]
                        for e in p.stdout.split("\0") if e}):
        if not name or "=" in name or any(ord(c) < 32 for c in name):
            raise ShipRefused("gate: unsupported filter driver name in git config")
        for var in ("smudge", "clean", "process"):
            out += ["-c", f"filter.{name}.{var}="]
        out += ["-c", f"filter.{name}.required=false"]
    return out


def _gate_run(repo: Path, base: str, m: str, allow_install: bool) -> dict:
    res: dict = {k: {"exit": None, "tail": "", "status": "not run"} for k in GATE_KEYS}
    try:
        res["scan"] = [f"rule 4: {v.rule} {v.path} (commit {v.commit[:12]})"
                       for v in fp.scan_commits(repo, base, m)]
    except fp.GitError:
        res["scan"] = ["rule 4: secret scan failed"]
    if res["scan"]:
        raise ShipRefused("gate: secret scan: " + "; ".join(res["scan"]), res)
    cmds = read_agents_commands(repo, base)
    tmp = Path(tempfile.mkdtemp(prefix="factory-gate-", dir=fr.exec_tmpdir()))
    wt = tmp / "wt"
    (tmp / "tmp").mkdir()
    try:
        env = dict(fr.scrubbed_env(), GIT_LFS_SKIP_SMUDGE="1")
        fp._git(repo, *_filter_overrides(repo), "worktree", "add", "--detach", str(wt), m,
                env=env)
        for k in GATE_KEYS:
            if k not in cmds or (k == "install" and not allow_install):
                continue
            res[k] = _gate_cmd(cmds[k], wt, tmp / f"{k}.log", tmp / "tmp")
            if res[k]["status"] != "pass":
                raise ShipRefused(f"gate: {k} {res[k]['status']}"
                                  + (f" (exit {res[k]['exit']})" if res[k]["exit"] else ""), res)
    finally:
        fp._git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        fp._git(repo, "worktree", "prune", check=False)
    return res


def _record_gate(home: Path, run_id: str, owner: str, m: str, status: str) -> None:
    with _run_lock(home, run_id):
        run = _owned_run(home, run_id, owner)
        _not_shipped(run)
        ship = run.get("ship") if isinstance(run.get("ship"), dict) else {}
        if ship.get("merge_sha") != m:
            raise ShipRefused("ship re-prepared during the gate; run the gate again")
        run["ship"] = {**ship, "gate": {"merge_sha": m, "status": status, "at": _now()}}
        save_run(home, run_id, run, owner=owner)


def ship_gate(home: Path, run_id: str, *, owner: str, allow_install: bool) -> dict:
    """Spec §8 step 3: secret scan of <server tip>..<merge_sha>, then AGENTS.md install/test/
    build (read at the server tip, never the run) in a throwaway detached worktree. The result
    is recorded as ship.gate, bound to the merge it tested; ship-local needs a pass."""
    with _run_lock(home, run_id):
        run = _owned_run(home, run_id, owner)
        _not_shipped(run)
        repo, url = Path(run["repo"]), run["origin_url"]
        ship = _prepared(repo, run)
        base, m = ship["base"], ship["merge_sha"]
        _guard(repo, url, local=_is_local(run))
        run["ship"] = {k: v for k, v in ship.items() if k != "gate"}
        save_run(home, run_id, run, owner=owner)
    try:
        res = _gate_run(repo, base, m, allow_install)
    except ShipRefused:
        _record_gate(home, run_id, owner, m, "fail")
        raise
    _record_gate(home, run_id, owner, m, "pass")
    return res


def _checkout_of(repo: Path, branch: str) -> Path | None:
    """The worktree that has refs/heads/<branch> checked out, if any."""
    p = fp._git(repo, "worktree", "list", "--porcelain", "-z", raw=True)
    path = None
    for field_ in p.stdout.split("\0"):
        if field_.startswith("worktree "):
            path = field_[9:]
        elif field_ == f"branch refs/heads/{branch}" and path:
            return Path(path)
    return None


def ship_local(home: Path, run_id: str, *, owner: str) -> str:
    """Spec §8 step 5: move local D to merge_sha (ff-only merge where D is checked out, else
    compare-and-swap). Refuses without touching anything when D diverged or the merge fails."""
    with _run_lock(home, run_id):
        run = _owned_run(home, run_id, owner)
        _not_shipped(run)
        repo, d, url = Path(run["repo"]), run["default_branch"], run["origin_url"]
        _check_default(repo, d)
        ship = _prepared(repo, run)
        m = ship["merge_sha"]
        gate = ship.get("gate")
        if not (isinstance(gate, dict) and gate.get("status") == "pass"
                and gate.get("merge_sha") == m):
            raise ShipRefused("gate has not passed for this merge")
        cur = _resolve(repo, f"refs/heads/{d}")
        if not cur or (cur != ship["base"] and not fp.reachable_from(repo, cur, m)):
            raise ShipRefused(f"local {d} has diverged")
        wt = _checkout_of(repo, d)
        _guard(repo, url, *([wt] if wt else []), local=_is_local(run))
        if wt:
            if fp._git(wt, "merge", "--ff-only", "--no-stat", m, check=False).returncode:
                raise ShipRefused(f"git merge --ff-only failed in {wt} (local changes?)")
            if _resolve(repo, f"refs/heads/{d}") != m:
                raise ShipRefused(f"local {d} is not the ship merge after merging")
            how = f"ff-checkout:{wt}"
        elif not _update_ref(repo, f"refs/heads/{d}", m, cur):
            raise ShipRefused(f"local {d} moved (compare-and-swap failed)")
        else:
            how = "update-ref"
        run["ship"] = {**ship, "local": how}
        save_run(home, run_id, run, owner=owner)
        return how


def ship_finish(home: Path, run_id: str, *, owner: str, pushed: bool) -> dict:
    """Spec §8 step 8: merged jobs in the ship merge -> shipped (and finished in the backlog;
    the rest are listed as skipped), run branch deleted, run marked shipped. Job worktrees are
    left to the coordinator (Orca)."""
    with _run_lock(home, run_id):
        run = _owned_run(home, run_id, owner)
        _not_shipped(run)
        repo, d = Path(run["repo"]), run["default_branch"]
        _check_default(repo, d)
        ship = run.get("ship")
        if not isinstance(ship, dict) or not _sha(ship.get("merge_sha")):
            raise ShipRefused("ship not prepared")
        m = ship["merge_sha"]
        if pushed and _is_local(run):
            raise ShipRefused("local run: nothing was pushed")
        gate = ship.get("gate")
        if not (isinstance(gate, dict) and gate.get("status") == "pass"
                and gate.get("merge_sha") == m):
            # D can hold an ungated merge (moved by hand, or by a worker): never mark it shipped.
            raise ShipRefused("gate has not passed for this merge")
        if not fp.reachable_from(repo, m, f"refs/heads/{d}"):
            raise ShipRefused(f"local {d} does not contain the ship merge")
        done, skipped = [], []
        for e in run.get("merged") or []:
            jid = str(e.get("job")) if isinstance(e, dict) else "?"
            try:
                if not fp.reachable_from(repo, e.get("commit") if isinstance(e, dict) else None,
                                         m):
                    raise Refused("not in the ship merge")
                jd = _job_dir(home, jid)
                if fj.read_job(jd).get("run") != run_id:
                    raise Refused("job moved to another run")
                fj.update_job(jd, {"stage": "shipped"}, owner=owner)
                fb.finish(home, jid)
                done.append(jid)
            except (Refused, fj.OwnerMismatch, fj.BadRequest, OSError, ValueError) as e2:
                skipped.append({"job": jid, "reason": str(e2) or type(e2).__name__})
        ref = f"refs/heads/{run['branch']}"
        tip = _resolve(repo, ref)
        deleted = tip is None
        if tip and fp._git(repo, "branch", "-d", run["branch"], check=False).returncode == 0:
            deleted = True
        elif tip and tip == ship.get("run_tip") and fp.reachable_from(repo, tip, f"refs/heads/{d}"):
            # -d judges "merged" against HEAD, which need not be D (update-ref case).
            deleted = fp._git(repo, "update-ref", "-d", ref, tip, check=False).returncode == 0
        run["shipped"] = {"merge_sha": m, "pushed": bool(pushed), "at": _now()}
        save_run(home, run_id, run, owner=owner)
        return {"shipped": run["shipped"], "jobs": done, "skipped": skipped,
                "branch_deleted": deleted}


# --- CLI ----------------------------------------------------------------------

EXIT = {"merged": 0, "held": 0, "refused": 2, "conflict": 3, "error": 1}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory_integrate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new-run")
    s = sub.add_parser("start-run")
    s.add_argument("--repo", required=True)
    s.add_argument("--run", required=True)
    s.add_argument("--default", required=True)
    s.add_argument("--origin-url")
    s.add_argument("--local", action="store_true",
                   help="no trusted origin: base on local D, never fetch or push")
    s.add_argument("--owner", required=True)
    for name in ("finish-job", "merge-job"):
        m = sub.add_parser(name)
        m.add_argument("--run", required=True)
        m.add_argument("--job", required=True)
        m.add_argument("--owner", required=True)
    a = sub.add_parser("accept")
    a.add_argument("--job", required=True)
    a.add_argument("--owner", required=True)
    a.add_argument("--by", required=True)
    a.add_argument("--note", default="")
    r = sub.add_parser("reassign")
    r.add_argument("--job", required=True)
    r.add_argument("--run", required=True)
    r.add_argument("--owner", required=True)
    r.add_argument("--force-move", action="store_true")
    rj = sub.add_parser("rebase-job")
    rj.add_argument("--job", required=True)
    rj.add_argument("--owner", required=True)
    mr = sub.add_parser("mark-review")
    mr.add_argument("--job", required=True)
    mr.add_argument("--owner", required=True)
    tr = sub.add_parser("takeover-run")
    tr.add_argument("--run", required=True)
    tr.add_argument("--owner", required=True)
    tr.add_argument("--reason", required=True)
    ab = sub.add_parser("after-base")
    ab.add_argument("--run", required=True)
    ab.add_argument("--after", required=True, help="the job this one follows")
    st = sub.add_parser("status")
    st.add_argument("--run", required=True)
    ss = sub.add_parser("ship-status")
    ss.add_argument("--run", required=True)
    for name in ("ship-prepare", "ship-local", "ship-finish"):
        sp = sub.add_parser(name)
        sp.add_argument("--run", required=True)
        sp.add_argument("--owner", required=True)
        if name == "ship-finish":
            sp.add_argument("--pushed", required=True, choices=("yes", "no"))
    sg = sub.add_parser("ship-gate")
    sg.add_argument("--run", required=True)
    sg.add_argument("--owner", required=True)
    sg.add_argument("--allow-install", action="store_true")
    args = ap.parse_args(argv)
    try:
        home = fj.home()
        if args.cmd == "new-run":
            out, rc = {"run": claim_run(home)}, 0
        elif args.cmd == "start-run":
            out, rc = start_run(home, args.run, repo=Path(args.repo), default_branch=args.default,
                                origin_url=args.origin_url, owner=args.owner,
                                local=args.local), 0
        elif args.cmd in ("finish-job", "merge-job"):
            fn = finish_job if args.cmd == "finish-job" else merge_job
            res = fn(home, args.run, args.job, owner=args.owner)
            out, rc = asdict(res), EXIT[res.status]
        elif args.cmd == "accept":
            out, rc = accept(home, args.job, owner=args.owner, by=args.by, note=args.note), 0
        elif args.cmd == "reassign":
            out, rc = reassign(home, args.job, args.run, owner=args.owner,
                               force_move=args.force_move), 0
        elif args.cmd == "rebase-job":
            out, rc = rebase_job(home, args.job, owner=args.owner), 0
        elif args.cmd == "mark-review":
            out, rc = {"review_head": mark_review(home, args.job, owner=args.owner)}, 0
        elif args.cmd == "takeover-run":
            out, rc = takeover_run(home, args.run, args.owner, reason=args.reason), 0
        elif args.cmd == "after-base":
            out, rc = {"base": after_base(home, args.run, args.after)}, 0
        elif args.cmd == "ship-status":
            out, rc = ship_status(home, args.run), 0
        elif args.cmd == "ship-prepare":
            out, rc = ship_prepare(home, args.run, owner=args.owner), 0
        elif args.cmd == "ship-gate":
            out, rc = ship_gate(home, args.run, owner=args.owner,
                                allow_install=args.allow_install), 0
        elif args.cmd == "ship-local":
            out, rc = {"local": ship_local(home, args.run, owner=args.owner)}, 0
        elif args.cmd == "ship-finish":
            out, rc = ship_finish(home, args.run, owner=args.owner,
                                  pushed=args.pushed == "yes"), 0
        else:
            out, rc = run_status(home, args.run), 0
    except ShipRefused as e:
        if e.detail is not None:
            print(json.dumps(e.detail, indent=2))
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except (Refused, fj.OwnerMismatch, fj.BadRequest) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except (fp.GitError, OSError, ValueError, RuntimeError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
