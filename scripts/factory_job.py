#!/usr/bin/env python3
"""Job state for the factory skill: ids, owner-only job.json, resume step."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

LENSES = ("security", "code", "ux", "ui")


class OwnerMismatch(Exception):
    pass


class BadRequest(ValueError):
    """A caller error (bad job id, protected field in a patch); the CLI exits 2."""


JOB_ID_RE = re.compile(r"\d{3,}-[a-z0-9-]+")
PROTECTED = ("owner", "id", "takeovers")  # changed only by claim and takeover
REASON_MAX = 1000  # characters kept of a failure reason


BOGUS_OWNERS = ("none", "null", "nil", "undefined")


def check_owner(owner: str) -> str:
    """An owner is an Orca run id written by claim or takeover; refuse the values a caller that
    lost the id prints instead (empty, `None`, `null`) and anything with whitespace."""
    if (not isinstance(owner, str) or not owner or owner.lower() in BOGUS_OWNERS
            or re.search(r"[\s\x00-\x1f\x7f]", owner)):
        raise BadRequest(f"invalid owner {owner!r}")
    return owner


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise BadRequest("invalid job id")
    return job_id


HOME_DIR = ".factory"
# Committed with the repo: everything under .factory/ stays local except the backlog.
HOME_GITIGNORE = "*\n!.gitignore\n!backlog.md\n"


def repo_home(repo: Path) -> Path:
    """<main checkout>/.factory for the repo containing repo. The common git dir's parent, so a
    path inside a linked worktree resolves to the main checkout's home."""
    try:
        p = subprocess.run(["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
                            "-c", "core.fsmonitor=false", "-C", str(repo), "rev-parse",
                            "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        p = None
    gd = Path(p.stdout.strip()) if p and p.returncode == 0 and p.stdout.strip() else None
    if gd is None or gd.name != ".git":
        raise BadRequest(f"{repo} is not in a git repo with a work tree; set FACTORY_HOME")
    return gd.parent / HOME_DIR


def home(repo: Path | None = None) -> Path:
    """FACTORY_HOME if set, else <repo>/.factory (repo defaults to the current directory)."""
    env = os.environ.get("FACTORY_HOME")
    if env:
        return Path(env)
    return repo_home(Path(repo) if repo is not None else Path.cwd())


def repo_of(home: Path) -> Path:
    """The repo a home belongs to: <repo>/.factory -> <repo>."""
    return Path(home).resolve().parent


def prepare_home(home: Path) -> None:
    """Create the home and its .gitignore (only the backlog and the ignore file are tracked)."""
    home.mkdir(parents=True, exist_ok=True)
    gi = home / ".gitignore"
    if not gi.exists() and not gi.is_symlink():
        gi.write_text(HOME_GITIGNORE)


def jobs_dir(home: Path) -> Path:
    return home / "jobs"


def slugify(feature: str) -> str:
    words = re.findall(r"[a-z0-9]+", feature.lower())[:4]
    return "-".join(words)[:40].strip("-") or "job"


def job_number(job_id: str) -> int:
    return int(job_id.split("-", 1)[0])


def _highest(jobs: Path) -> int:
    best = 0
    for p in jobs.iterdir():
        m = re.match(r"\.n-(\d+)$|(\d+)-", p.name)
        if m:
            best = max(best, int(m.group(1) or m.group(2)))
    return best


def claim_job_dir(jobs: Path, feature: str) -> tuple[str, Path]:
    slug = slugify(feature)
    for _ in range(20):
        n = _highest(jobs) + 1
        try:  # atomic reservation of the number itself, independent of the slug
            (jobs / f".n-{n:03d}").mkdir(mode=0o700)
        except FileExistsError:
            continue
        job_id = f"{n:03d}-{slug}"
        path = jobs / job_id
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue  # number stays reserved; take the next one
        return job_id, path
    raise RuntimeError("could not claim a job directory after 20 attempts")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(job_dir: Path, job: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=job_dir, prefix=".job.json.")  # mode 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(job, f, indent=2)
            f.write("\n")
        os.replace(tmp, job_dir / "job.json")
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def new_job(job_dir: Path, *, feature: str, repo: str, owner: str,
            default_branch: str | None = None, work_item: int | None = None,
            run: str | None = None, after: str | None = None) -> dict:
    check_owner(owner)
    now = _now()
    job = {
        "id": job_dir.name, "feature": feature, "repo": repo,
        "default_branch": default_branch, "work_item": work_item, "after": after, "base": None, "branch": None,
        "worktree": None, "port": None, "stage": "spec", "round": 0,
        "owner": owner, "reviews": {},
        "verified": {"confirmed": 0, "false_positive": 0, "unverifiable": 0},
        "risk": None, "overlaps": [], "run": run, "hold": None, "accepted": None, "reason": None,
        "merge_commit": None, "workers": [], "takeovers": [],
        "created": now, "updated": now,
    }
    _write(job_dir, job)
    return job


def read_job(job_dir: Path) -> dict:
    """job.json as a dict. Raises OSError, or ValueError when it is not a readable object
    (including JSON nested too deeply for the parser, which raises RecursionError)."""
    try:
        job = json.loads((job_dir / "job.json").read_text())
    except RecursionError:
        raise ValueError("job.json nested too deeply") from None
    if not isinstance(job, dict):
        raise ValueError("job.json is not an object")
    return job


def update_job(job_dir: Path, patch: dict, *, owner: str) -> dict:
    if not isinstance(patch, dict):
        raise BadRequest("patch must be a JSON object")
    bad = sorted(k for k in patch if k in PROTECTED)
    if bad:
        raise BadRequest(f"patch may not change {', '.join(bad)} (use claim or takeover)")
    if patch.get("stage") == "failed":
        # A failed job must say why; the dashboard and status show it.
        reason = patch.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise BadRequest("stage failed needs a non-empty \"reason\" in the same patch")
        patch = dict(patch, reason=reason.strip()[:REASON_MAX])
    elif "stage" in patch and "reason" not in patch:
        patch = dict(patch, reason=None)  # leaving failed (rework, resume) drops the old reason
    job = read_job(job_dir)
    if job.get("owner") != owner:
        raise OwnerMismatch(f"job owned by {job.get('owner')!r}, not {owner!r}")
    job.update(patch)
    job["updated"] = _now()
    _write(job_dir, job)
    return job


def takeover(job_dir: Path, new_owner: str, *, reason: str) -> dict:
    check_owner(new_owner)
    job = read_job(job_dir)
    now = _now()
    job.setdefault("takeovers", []).append(
        {"from": job.get("owner"), "to": new_owner, "at": now, "reason": reason})
    job["owner"] = new_owner
    job["updated"] = now
    _write(job_dir, job)
    return job


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


STAGES = ("spec", "build", "review", "verify", "held", "merged", "shipped", "blocked", "failed",
          "closed")
DONE_STAGES = ("held", "merged", "shipped", "closed", "blocked", "failed")


def next_step(job_dir: Path) -> str:
    """Next coordinator step: none|spec|build|review|verify|finish.

    `none` only for a finished stage. Stages `spec`/`build` mean the builder has not settled
    (a rework round sets `build` before its builder starts). Later stages are derived from the
    files of round `job.round`, which the coordinator sets when it accepts a READY build.
    """
    from factory_review import parse_build, parse_review  # lazy: Task 5

    try:
        job = read_job(job_dir)
    except (OSError, ValueError):
        job = {}
    stage = job.get("stage", "")
    if stage in DONE_STAGES:
        return "none"
    if not (job_dir / "spec.md").exists():
        return "spec"
    if stage in ("spec", "build"):
        return "build"
    status, _ = parse_build(_read(job_dir / "build.md"))
    if status != "READY":
        return "build"
    rnd = job.get("round")
    if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
        return "review"
    rdir = job_dir / f"round-{rnd}"
    if not all((rdir / f"review-{lens}.md").exists() for lens in LENSES):
        return "review"
    for lens in LENSES:
        review = parse_review(_read(rdir / f"review-{lens}.md"))
        if review.verdict == "CHANGES":
            for f in review.findings:
                if not (rdir / f"verify-{lens}-{f.n}.md").exists():
                    return "verify"
    return "finish" if (rdir / "risk.md").exists() else "verify"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory_job")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--feature", required=True)
    c.add_argument("--repo", required=True)
    c.add_argument("--owner", required=True)
    c.add_argument("--default")
    s = sub.add_parser("show"); s.add_argument("id")
    u = sub.add_parser("update"); u.add_argument("id")
    u.add_argument("--owner", required=True); u.add_argument("--patch", required=True)
    t = sub.add_parser("takeover"); t.add_argument("id")
    t.add_argument("--owner", required=True); t.add_argument("--reason", required=True)
    n = sub.add_parser("next"); n.add_argument("id")
    a = ap.parse_args(argv)

    try:
        jobs = jobs_dir(home())
        if a.cmd == "claim":
            jobs.mkdir(parents=True, exist_ok=True)
            job_id, path = claim_job_dir(jobs, a.feature)
            new_job(path, feature=a.feature, repo=a.repo, owner=a.owner,
                    default_branch=a.default)
            print(job_id)
            return 0
        path = jobs / check_job_id(a.id)
        if a.cmd == "show":
            print(json.dumps(read_job(path), indent=2))
        elif a.cmd == "update":
            try:
                patch = json.loads(a.patch)
            except (ValueError, RecursionError):
                raise BadRequest("patch is not valid JSON") from None
            update_job(path, patch, owner=a.owner)
        elif a.cmd == "takeover":
            takeover(path, a.owner, reason=a.reason)
        elif a.cmd == "next":
            print(next_step(path))
    except BadRequest as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OwnerMismatch, RuntimeError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
