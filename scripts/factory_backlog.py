#!/usr/bin/env python3
"""Backlog for the factory skill: <repo>/.factory/backlog.md (committed with the repo), one
markdown line per item; add, list, edit, remove, locked claim, finish, stale.

Item numbers (`n`) count backlog lines from 1 in file order; other lines (headings, notes) are
kept and not numbered."""
import argparse
import dataclasses
import fcntl
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import factory_job as fj

MERGED_STAGES = ("merged", "shipped", "closed")
AFTER_STAGES = ("merged", "shipped")
FAIL_STAGES = ("held", "blocked", "failed")

_LINE = re.compile(r"^- \[([ ~x!])\] (.*)$")
_MARKER = re.compile(r" → (\d{3,}-[a-z0-9-]+)(?: (\S+))?$")
_OPTS = (
    ("install", re.compile(r' install:"([^"]*)"$')),
    ("after", re.compile(r" after:(\S+)$")),
    ("default", re.compile(r" default:(\S+)$")),
    ("wi", re.compile(r" wi:([1-9][0-9]{0,9})$")),
)


class BacklogError(ValueError):
    """A refused backlog edit (unknown item, item in progress); the CLI exits 2."""


STATE_NAMES = {" ": "todo", "~": "claimed", "x": "done", "!": "stopped"}


@dataclass
class Item:
    line_no: int
    state: str
    feature: str
    after: str | None = None
    default: str | None = None
    install: str | None = None
    work_item: int | None = None
    job_id: str | None = None
    stage: str | None = None
    n: int = 0


def _parse_line(line_no: int, line: str) -> Item | None:
    m = _LINE.match(line)
    if not m:
        return None
    rest = m.group(2)
    job_id = stage = None
    mk = _MARKER.search(rest)
    if mk:
        job_id, stage = mk.group(1), mk.group(2)
        rest = rest[:mk.start()]
    opts: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for name, rx in _OPTS:
            if name not in opts and (o := rx.search(rest)):
                opts[name] = o.group(1)
                rest = rest[:o.start()]
                changed = True
    wi = int(opts["wi"]) if "wi" in opts else None
    return Item(line_no, m.group(1), rest, opts.get("after"), opts.get("default"),
                opts.get("install"), wi, job_id, stage)


def parse(text: str) -> list[Item]:
    items = []
    for n, line in enumerate(text.splitlines(), 1):
        it = _parse_line(n, line)
        if it:
            it.n = len(items) + 1
            items.append(it)
    return items


@contextmanager
def _locked(home: Path):
    fj.prepare_home(home)
    (home / "backlog.md").touch()
    with open(home / "backlog.md.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _lines(home: Path) -> list[str]:
    return (home / "backlog.md").read_text().splitlines()


def _save(home: Path, lines: list[str]) -> None:
    fd, tmp = tempfile.mkstemp(dir=home, prefix=".backlog.md.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("".join(l + "\n" for l in lines))
        os.replace(tmp, home / "backlog.md")
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _stage(home: Path, job_id: str) -> str | None:
    try:
        return fj.read_job(fj.jobs_dir(home) / job_id).get("stage")
    except (OSError, ValueError):
        return None


def _retag(line: str, state: str, job_id: str, stage: str | None = None) -> str:
    body = _MARKER.sub("", _LINE.match(line).group(2))
    return f"- [{state}] {body} → {job_id}" + (f" {stage}" if stage else "")


def _check_text(text: str) -> str:
    text = text.strip()
    if not text or "\n" in text or "\r" in text:
        raise BacklogError("an item is one non-empty line")
    if _MARKER.search(text):
        raise BacklogError("an item may not end in a job marker (→ <job-id>)")
    return text


def _item(items: list[Item], n: int) -> Item:
    for it in items:
        if it.n == n:
            return it
    raise BacklogError(f"no backlog item {n}")


def add(home: Path, feature: str) -> int:
    """Append a todo item (the feature text, optionally followed by after:/default:/wi:/install:
    tokens); returns its item number."""
    text = _check_text(feature)
    with _locked(home):
        lines = _lines(home)
        lines.append(f"- [ ] {text}")
        _save(home, lines)
        return len(parse("\n".join(lines)))


def list_items(home: Path) -> list[Item]:
    f = home / "backlog.md"
    return parse(f.read_text()) if f.exists() else []


def edit(home: Path, n: int, feature: str) -> Item:
    """Replace a todo item's text (feature and tokens). Claimed, done or stopped items keep the
    text their job was made from."""
    text = _check_text(feature)
    with _locked(home):
        lines = _lines(home)
        it = _item(parse("\n".join(lines)), n)
        if it.state != " ":
            raise BacklogError(f"item {n} is {STATE_NAMES[it.state]}; only todo items can be "
                               "edited")
        lines[it.line_no - 1] = f"- [ ] {text}"
        _save(home, lines)
        return _item(parse("\n".join(lines)), n)


def remove(home: Path, n: int) -> Item:
    """Delete an item. A claimed one is refused: its job is still running (close it first)."""
    with _locked(home):
        lines = _lines(home)
        it = _item(parse("\n".join(lines)), n)
        if it.state == "~":
            raise BacklogError(f"item {n} is claimed by job {it.job_id}; finish or close the job "
                               "first")
        del lines[it.line_no - 1]
        _save(home, lines)
        return it


def claim(home: Path, owner: str, line_no: int | None = None,
          run: str | None = None) -> tuple[Item, str] | None:
    """Claim the first eligible todo item, or item number line_no (bypassing after:)."""
    fj.check_owner(owner)
    with _locked(home):
        lines = _lines(home)
        pick = None
        for it in parse("\n".join(lines)):
            if it.state != " " or (line_no is not None and it.n != line_no):
                continue
            if line_no is None and it.after and _stage(home, it.after) not in AFTER_STAGES:
                continue
            pick = it
            break
        if pick is None:
            return None
        jobs = fj.jobs_dir(home)
        jobs.mkdir(parents=True, exist_ok=True)
        job_id, path = fj.claim_job_dir(jobs, pick.feature)
        fj.new_job(path, feature=pick.feature, repo=str(fj.repo_of(home)), owner=owner,
                   default_branch=pick.default, work_item=pick.work_item, run=run)
        lines[pick.line_no - 1] = _retag(lines[pick.line_no - 1], "~", job_id)
        _save(home, lines)
        return pick, job_id


def finish(home: Path, job_id: str) -> None:
    with _locked(home):
        stage = _stage(home, job_id)
        if stage in MERGED_STAGES:
            state = "x"
        elif stage in FAIL_STAGES:
            state = "!"
        else:
            return
        lines = _lines(home)
        for it in parse("\n".join(lines)):
            if it.job_id == job_id:
                lines[it.line_no - 1] = _retag(lines[it.line_no - 1], state, job_id, stage)
        _save(home, lines)


def stale(home: Path, live: set[str], now: datetime, hours: float = 2) -> list[str]:
    out = []
    for it in parse((home / "backlog.md").read_text() if (home / "backlog.md").exists() else ""):
        if it.state != "~" or not it.job_id or it.job_id in live:
            continue
        try:
            job = fj.read_job(fj.jobs_dir(home) / it.job_id)
            updated = datetime.fromisoformat(job["updated"])
        except (OSError, ValueError, KeyError):
            out.append(it.job_id)  # missing or unreadable job counts as stale
            continue
        if now - updated > timedelta(hours=hours):
            out.append(it.job_id)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory_backlog")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("add"); a_.add_argument("feature")
    sub.add_parser("list")
    e = sub.add_parser("edit"); e.add_argument("n", type=int); e.add_argument("feature")
    r = sub.add_parser("remove"); r.add_argument("n", type=int)
    c = sub.add_parser("claim"); c.add_argument("--owner", required=True)
    c.add_argument("--line", type=int); c.add_argument("--run")
    f = sub.add_parser("finish"); f.add_argument("id")
    s = sub.add_parser("stale"); s.add_argument("--live", default="")
    a = ap.parse_args(argv)
    try:
        h = fj.home()
        if a.cmd == "add":
            print(add(h, a.feature))
        elif a.cmd == "list":
            print(json.dumps({"repo": str(fj.repo_of(h)), "items": [
                {"n": it.n, "state": STATE_NAMES[it.state], "feature": it.feature,
                 "after": it.after, "default": it.default, "install": it.install,
                 "work_item": it.work_item, "job_id": it.job_id, "stage": it.stage}
                for it in list_items(h)]}, indent=2))
        elif a.cmd == "edit":
            print(json.dumps(dataclasses.asdict(edit(h, a.n, a.feature))))
        elif a.cmd == "remove":
            print(json.dumps(dataclasses.asdict(remove(h, a.n))))
        elif a.cmd == "claim":
            got = claim(h, a.owner, a.line, a.run)
            if got is None:
                print("null")
            else:
                it, job_id = got
                print(json.dumps({"job_id": job_id, "repo": str(fj.repo_of(h)),
                                  "feature": it.feature,
                                  "install": it.install, "default": it.default,
                              "work_item": it.work_item}))
        elif a.cmd == "finish":
            finish(h, a.id)
        elif a.cmd == "stale":
            live = {x for x in a.live.split(",") if x}
            for job_id in stale(h, live, datetime.now(timezone.utc)):
                print(job_id)
    except (fj.BadRequest, BacklogError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (RuntimeError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
