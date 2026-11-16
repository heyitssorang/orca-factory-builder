#!/usr/bin/env python3
"""Scheduling caps and dev-server port choice for the factory coordinator.

Pure decision logic (`admit`, `can_claim`, `choose_port`) plus a small CLI that
keeps a run file `<FACTORY_HOME>/runs/<run-id>.json`:

    {"caps": {...}, "pending": [...], "running": [...]}

One coordinator drives a run, so concurrent CLI invocations on the same run
file are not expected and no locking is done. Writes are atomic (temp file in
the same directory, then os.replace).

Usage:
    factory_schedule.py init --run F [--jobs N] [--workers W] [--servers S]
    factory_schedule.py request --run F --job J --kind K --workers N [--server] [--requested T]
    factory_schedule.py admit --run F
    factory_schedule.py done --run F --step ID
    factory_schedule.py can-claim --run F --active N
    factory_schedule.py port JOB_NUMBER
"""
import argparse
import contextlib
import fcntl
import json
import os
import socket
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Caps:
    jobs: int = 3
    workers: int = 8
    servers: int = 4

    def __post_init__(self):
        self.jobs = max(1, min(5, self.jobs))


@dataclass
class Step:
    id: str
    job_id: str
    kind: str
    requested: float
    workers: int
    server: bool


def choose_port(job_number: int, is_free: Callable[[int], bool]) -> int | None:
    base = 4100 + 10 * (job_number % 100)
    for port in range(base, base + 10):
        if is_free(port):
            return port
    return None


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def can_claim(active_jobs: int, caps: Caps) -> bool:
    return active_jobs < caps.jobs


def claimable(active_jobs: int, caps: Caps) -> int:
    """How many more jobs may be claimed now."""
    return max(0, caps.jobs - max(0, active_jobs))


def _never_fits(step: Step, caps: Caps) -> bool:
    return step.workers > caps.workers or (step.server and caps.servers < 1)


def admit(pending: list[Step], running: list[Step], caps: Caps) -> list[Step]:
    workers_left = caps.workers - sum(s.workers for s in running)
    servers_left = caps.servers - sum(1 for s in running if s.server)
    workers_blocked = servers_blocked = False
    admitted: list[Step] = []
    for step in sorted(pending, key=lambda s: s.requested):
        if _never_fits(step, caps):
            continue  # can never run; must not block others
        needs_workers = step.workers > 0
        if needs_workers and (workers_blocked or step.workers > workers_left):
            workers_blocked = True
            continue
        if step.server and (servers_blocked or servers_left < 1):
            servers_blocked = True
            continue
        workers_left -= step.workers
        servers_left -= 1 if step.server else 0
        admitted.append(step)
    return admitted


# --- run file -------------------------------------------------------------

@contextlib.contextmanager
def run_lock(path: Path):
    """Exclusive flock serialising read-modify-write of run file `<id>.json` (lock file
    `.<id>.flock` beside it); factory_integrate takes the same lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / f".{path.name.removesuffix('.json')}.flock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load(path: Path) -> dict:
    if path.exists():
        data = json.loads(path.read_text())
    else:
        data = {}
    data.setdefault("caps", asdict(Caps()))
    data.setdefault("pending", [])
    data.setdefault("running", [])
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _steps(rows: list[dict]) -> list[Step]:
    return [Step(**r) for r in rows]


def _next_id(data: dict, job: str, kind: str) -> str:
    used = {s["id"] for s in data["pending"] + data["running"]}
    n = 1
    while f"{job}:{kind}:{n}" in used:
        n += 1
    return f"{job}:{kind}:{n}"


# --- CLI ------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory_schedule")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--run", required=True)
    i.add_argument("--jobs", type=int, default=Caps.jobs)
    i.add_argument("--workers", type=int, default=Caps.workers)
    i.add_argument("--servers", type=int, default=Caps.servers)
    r = sub.add_parser("request")
    r.add_argument("--run", required=True)
    r.add_argument("--job", required=True)
    r.add_argument("--kind", required=True)
    r.add_argument("--workers", type=int, required=True)
    r.add_argument("--server", action="store_true")
    r.add_argument("--requested", type=float)
    a = sub.add_parser("admit")
    a.add_argument("--run", required=True)
    d = sub.add_parser("done")
    d.add_argument("--run", required=True)
    d.add_argument("--step", required=True)
    cc = sub.add_parser("can-claim")
    cc.add_argument("--run", required=True)
    cc.add_argument("--active", type=int, required=True)
    p = sub.add_parser("port")
    p.add_argument("job_number", type=int)
    args = ap.parse_args(argv)

    if args.cmd == "port":
        port = choose_port(args.job_number, port_is_free)
        if port is None:
            print("no free port", file=sys.stderr)
            return 1
        print(port)
        return 0

    path = Path(args.run)
    with run_lock(path):
        return _run_cmd(args, path)


def _run_cmd(args, path: Path) -> int:
    if args.cmd == "init":
        if path.exists():
            print(f"run file exists: {path}", file=sys.stderr)
            return 1
        if args.workers < 0 or args.servers < 0:
            print("--workers and --servers must be >= 0", file=sys.stderr)
            return 2
        _save(path, _load(path) | {"caps": asdict(
            Caps(args.jobs, args.workers, args.servers))})
        return 0
    data = _load(path)
    if args.cmd == "can-claim":
        if args.active < 0:
            print("--active must be >= 0", file=sys.stderr)
            return 2
        print(claimable(args.active, Caps(**data["caps"])))
        return 0
    if args.cmd == "request":
        if args.workers < 0:
            print("--workers must be >= 0", file=sys.stderr)
            return 2
        if args.workers > data["caps"]["workers"] or (
                args.server and data["caps"]["servers"] < 1):
            print("step can never fit the caps", file=sys.stderr)
            return 2
        step = Step(_next_id(data, args.job, args.kind), args.job, args.kind,
                    time.time() if args.requested is None else args.requested,
                    args.workers, args.server)
        data["pending"].append(asdict(step))
        _save(path, data)
        print(step.id)
    elif args.cmd == "admit":
        caps = Caps(**data["caps"])
        got = admit(_steps(data["pending"]), _steps(data["running"]), caps)
        ids = {s.id for s in got}
        data["pending"] = [s for s in data["pending"] if s["id"] not in ids]
        data["running"] += [asdict(s) for s in got]
        _save(path, data)
        for s in got:
            print(json.dumps(asdict(s)))
    else:
        before = len(data["running"])
        data["running"] = [s for s in data["running"] if s["id"] != args.step]
        if len(data["running"]) == before:
            print(f"step not running: {args.step}", file=sys.stderr)
            return 1
        _save(path, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
