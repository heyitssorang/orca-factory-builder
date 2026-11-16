#!/usr/bin/env python3
"""Read-only local dashboard for the factory skill: server, probe, ensure, stop.

One server for every repo: it finds repos through Orca (`repo list`; FACTORY_REPOS, a
path-separated list, overrides it), shows each repo with a `.factory/` home as a swim lane, and
marks jobs whose Orca workers are live (`orchestration worker-list --run <the job's owner
run>`, so the answer does not depend on the run bound to the terminal that started the
server). Its pid and log live in
~/.cache/orca-factory (FACTORY_DASHBOARD_STATE overrides), not in any repo."""
import argparse
import dataclasses
import hashlib
import http.client
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import factory_backlog as fb
import factory_job as fj

DEFAULT_PORT = 8787
MAX_FILE = 2 * 1024 * 1024
MARK = "X-Factory-Dashboard"
CACHE_SECONDS = 5
ORCA_TIMEOUT = 10
_LANE_KEY = re.compile(r"[0-9a-f]{12}")
_ORCA_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
# Stages in which a job can have running workers; only their owner runs are asked about.
LIVE_STAGES = ("spec", "build", "review", "verify")
DEFAULT_PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
       "script-src 'self' 'unsafe-inline'; img-src 'self'")


def safe_read(path: Path) -> bytes | None:
    """Read a regular, non-symlink file of at most MAX_FILE bytes; None otherwise.

    O_NOFOLLOW refuses a symlinked last component, O_NONBLOCK keeps a FIFO from hanging
    open(), and the fstat on the opened descriptor removes the stat/read race. Remaining gap:
    a parent directory swapped for a symlink between resolve() and open() is not caught
    (the jobs dir is owner-only, so an attacker would already be the user).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(MAX_FILE)
    except OSError:
        return None
    finally:
        os.close(fd)


def _list_files(job_dir: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(job_dir):  # followlinks=False
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if not name.startswith(".") and not p.is_symlink() and p.is_file():
                out.append(p.relative_to(job_dir).as_posix())
    return sorted(out)


RUN_FILE = re.compile(r"^(\d{8}-\d{4}-[0-9a-f]{4})\.json$")


def _runs(home: Path) -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(home / "runs"))
    except OSError:
        return out
    for name in names:
        m = RUN_FILE.match(name)
        if not m:
            continue
        raw = safe_read(home / "runs" / name)
        try:
            run = json.loads(raw) if raw is not None else None
        except (ValueError, RecursionError):
            continue
        if isinstance(run, dict) and run.get("id") == m.group(1):
            out.append({"id": m.group(1), "branch": run.get("branch"), "shipped": run.get("shipped")})
    return out


def collect(home: Path) -> dict:
    jobs = []
    root = fj.jobs_dir(home)
    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    for d in entries:
        if d.name.startswith(".") or d.is_symlink() or not d.is_dir():
            continue
        raw = safe_read(d / "job.json")
        try:
            job = json.loads(raw) if raw is not None else None
        except (ValueError, RecursionError):  # deep nesting raises RecursionError
            continue
        if not isinstance(job, dict):
            continue
        job["dir"] = d.name
        job["files"] = _list_files(d)
        jobs.append(job)
    try:
        backlog = [dataclasses.asdict(i) for i in fb.parse((home / "backlog.md").read_text())]
    except (OSError, ValueError):
        backlog = []
    return {"jobs": jobs, "backlog": backlog, "runs": _runs(home)}


def lane_key(home: Path) -> str:
    return hashlib.sha256(str(Path(home)).encode()).hexdigest()[:12]


def orca_cmd() -> list[str] | None:
    """The Orca CLI as the orchestration skill resolves it; never the bare `orca` (on Linux,
    /usr/bin/orca is the GNOME screen reader)."""
    env = os.environ.get("ORCA_CLI_COMMAND")
    if env:
        return shlex.split(env)
    found = shutil.which("orca-ide")
    if found:
        return [found]
    shim = Path.home() / ".config/orca/linux-orca-cli-shim/orca"
    return [str(shim)] if shim.is_file() else None


def _orca_json(*args: str) -> dict | None:
    cmd = orca_cmd()
    if not cmd:
        return None
    try:
        p = subprocess.run([*cmd, *args, "--json"], capture_output=True, text=True,
                           timeout=ORCA_TIMEOUT, stdin=subprocess.DEVNULL)
        out = json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, RecursionError):
        return None
    res = out.get("result") if isinstance(out, dict) and out.get("ok") else None
    return res if isinstance(res, dict) else None


def discover_repos() -> list[Path]:
    """Repo paths: FACTORY_REPOS if set, else every repo registered in Orca."""
    env = os.environ.get("FACTORY_REPOS")
    if env is not None:
        return [Path(x) for x in env.split(os.pathsep) if x]
    res = _orca_json("repo", "list")
    repos = res.get("repos") if res else None
    return [Path(r["path"]) for r in repos or []
            if isinstance(r, dict) and isinstance(r.get("path"), str)]


def discover_homes() -> list[Path]:
    """The .factory homes of the discovered repos (a repo without one has no lane)."""
    out = []
    for repo in discover_repos():
        h = repo / fj.HOME_DIR
        if h.is_dir() and not h.is_symlink() and h not in out:
            out.append(h)
    return out


def orca_run_workers(run: str) -> dict[str, dict] | None:
    """dispatch id -> {state, terminal} for one Orca run's workers (explicit --run: without it
    Orca scopes the list to the run bound to the calling terminal); None when Orca cannot be
    asked."""
    if not _ORCA_RUN.fullmatch(run):
        return {}
    res = _orca_json("orchestration", "worker-list", "--run", run, "--limit", "100")
    if res is None:
        return None
    out = {}
    for w in res.get("workers") or []:
        if isinstance(w, dict) and isinstance(w.get("dispatchId"), str):
            out[w["dispatchId"]] = {"state": w.get("workerState"),
                                    "terminal": w.get("terminalState")}
    return out


class RunWorkers:
    """workers(runs) -> merged dispatch map for those runs, or None when Orca could not be
    asked for any of them; each run is asked at most once per CACHE_SECONDS."""

    def __init__(self, fetch=orca_run_workers):
        self.fetch, self.cache = fetch, {}

    def __call__(self, runs) -> dict[str, dict] | None:
        now, out, ok = time.monotonic(), {}, not runs
        for run in sorted(runs):
            at, value = self.cache.get(run, (0.0, None))
            if value is None or now - at > CACHE_SECONDS:
                value = self.fetch(run)
                self.cache[run] = (now, value)
            if value is not None:
                ok = True
                out.update(value)
        return out if ok else None


class _Cached:
    """fn() at most once per CACHE_SECONDS; concurrent requests share the last value."""

    def __init__(self, fn):
        self.fn, self.at, self.value = fn, 0.0, None

    def __call__(self):
        now = time.monotonic()
        if self.value is None or now - self.at > CACHE_SECONDS:
            self.value, self.at = self.fn(), now
        return self.value


def _live(job: dict, workers: dict[str, dict]) -> list[dict]:
    out = []
    for w in job.get("workers") or []:
        if not isinstance(w, dict):
            continue
        seen = workers.get(str(w.get("dispatch")))
        if seen and seen.get("terminal") == "active":
            out.append({"kind": w.get("kind"), "lens": w.get("lens"), "state": seen.get("state")})
    return out


def collect_lanes(homes: list[Path], workers) -> dict:
    """workers: callable(set of Orca run ids) -> dispatch map or None (Orca unavailable)."""
    lanes = []
    for h in homes:
        repo = fj.repo_of(h)
        lanes.append({"key": lane_key(h), "repo": str(repo), "name": repo.name, **collect(h)})
    runs = {j["owner"] for lane in lanes for j in lane["jobs"]
            if j.get("stage") in LIVE_STAGES and isinstance(j.get("owner"), str)}
    seen = workers(runs)
    for lane in lanes:
        for job in lane["jobs"]:
            job["live"] = _live(job, seen or {})
    return {"lanes": lanes, "orca": seen is not None}


def _resolve_file(home: Path, rel_url: str) -> Path | None:
    parts = unquote(rel_url).split("/")
    if len(parts) < 2 or any(p == "" or p.startswith(".") or "\0" in p or "\\" in p for p in parts):
        return None
    root = fj.jobs_dir(home)
    job_dir = root / parts[0]
    if job_dir.is_symlink():
        return None
    try:
        base = job_dir.resolve(strict=True)
        target = (job_dir.joinpath(*parts[1:])).resolve(strict=True)
        if not target.is_relative_to(root.resolve(strict=True)) or not target.is_relative_to(base):
            return None
        if not target.is_file():
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return target


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "factory-dashboard"
    homes: "callable"
    workers: "callable"
    page: Path

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header(MARK, "1")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send(self, status, ctype, body=b"", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._text(code, message or self.responses.get(code, ("error",))[0])

    def _text(self, status, msg):
        self._send(status, "text/plain; charset=utf-8", (msg + "\n").encode())

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _handle(self):
        if not self._host_ok():
            return self._text(403, "forbidden host")
        path = urlsplit(self.path).path
        if path == "/":
            try:
                body = self.page.read_bytes()
            except OSError:
                return self._text(404, "not found")
            return self._send(200, "text/html; charset=utf-8", body, [("Content-Security-Policy", CSP)])
        if path == "/api/lanes":
            body = json.dumps(collect_lanes(self.homes(), self.workers)).encode()
            return self._send(200, "application/json", body)
        if path.startswith("/files/"):
            key, _, rel = path[len("/files/"):].partition("/")
            home = next((h for h in self.homes() if _LANE_KEY.fullmatch(key)
                         and lane_key(h) == key), None)
            target = _resolve_file(home, rel) if home is not None else None
            if target is None:
                return self._text(404, "not found")
            parts = target.relative_to(fj.jobs_dir(home).resolve()).parts
            # <job>/screenshots/ or <job>/round-<n>/screenshots/ (where reviewers save them)
            is_png = target.suffix == ".png" and (
                (len(parts) > 2 and parts[1] == "screenshots")
                or (len(parts) > 3 and re.fullmatch(r"round-\d+", parts[1])
                    and parts[2] == "screenshots"))
            body = safe_read(target)
            if body is None:
                return self._text(404, "not found")
            return self._send(200, "image/png" if is_png else "text/plain; charset=utf-8", body)
        self._text(404, "not found")

    do_GET = do_HEAD = _handle

    def _no(self):
        self._text(405, "method not allowed")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no


def make_server(homes=None, page: Path = DEFAULT_PAGE, port: int = DEFAULT_PORT, workers=None):
    """homes: a list of .factory homes, or a callable returning one (default: discover_homes,
    cached); workers: callable(set of Orca run ids) -> dispatch map or None (default:
    RunWorkers, cached per run)."""
    if homes is None:
        homes = _Cached(discover_homes)
    elif not callable(homes):
        fixed = [Path(h) for h in homes]
        homes = lambda: fixed  # noqa: E731
    workers = workers or RunWorkers()
    handler = type("BoundHandler", (Handler,), {"homes": staticmethod(homes),
                                                 "workers": staticmethod(workers),
                                                 "page": Path(page)})
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)


def probe(port: int) -> str:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        r.read()
        return "ours" if r.getheader(MARK) == "1" else "other"
    except ConnectionRefusedError:
        return "free"
    except (OSError, http.client.HTTPException):
        return "other"
    finally:
        c.close()


_started: dict = {}  # pid -> Popen, so stop() can reap what ensure() started


def ensure(home: Path, port: int) -> str | None:
    url = f"http://127.0.0.1:{port}/"
    state = probe(port)
    if state == "ours":
        return url
    if state == "other":
        print(f"factory dashboard: port {port} is held by another program; continuing without it",
              file=sys.stderr)
        return None
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "dashboard.log", "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "serve", "--port", str(port)],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            env={**os.environ, "FACTORY_DASHBOARD_STATE": str(home)})
    _started[proc.pid] = proc
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if probe(port) == "ours":
            return url
        if proc.poll() is not None:  # child died (e.g. lost the bind race): stop waiting
            _started.pop(proc.pid, None)
            if probe(port) == "ours":  # a concurrent ensure won the port
                return url
            break
        time.sleep(0.1)
    else:
        print(f"factory dashboard: did not start on port {port} within 5 s; see {home / 'dashboard.log'}",
              file=sys.stderr)
        return None
    print(f"factory dashboard: server exited at start on port {port}; see {home / 'dashboard.log'}",
          file=sys.stderr)
    return None


def _is_dashboard_pid(pid: int) -> bool:
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True  # no /proc (e.g. macOS): rely on the probe alone
    return b"factory_dashboard" in cmd


def stop(home: Path, port: int) -> bool:
    if probe(port) != "ours":
        return False
    pidfile = home / "dashboard.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return False
    if pid <= 1 or not _is_dashboard_pid(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if probe(port) == "free":
            break
        time.sleep(0.1)
    else:
        return False
    child = _started.pop(pid, None)
    if child is not None:  # reap it so Popen does not warn at garbage collection
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    try:
        pidfile.unlink()
    except FileNotFoundError:
        pass
    return True


def _write_pid(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=home, prefix=".dashboard.pid.")
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    os.replace(tmp, home / "dashboard.pid")


def state_dir() -> Path:
    env = os.environ.get("FACTORY_DASHBOARD_STATE")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "orca-factory"


def _default_port() -> int:
    try:
        return int(os.environ.get("FACTORY_DASHBOARD_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory_dashboard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "ensure", "stop"):
        sub.add_parser(name).add_argument("--port", type=int, default=_default_port())
    a = ap.parse_args(argv)
    h = state_dir()
    if a.cmd == "serve":
        try:
            srv = make_server(None, DEFAULT_PAGE, a.port)
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        _write_pid(h)  # only after a successful bind, so a losing instance never owns the file
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
        return 0
    if a.cmd == "ensure":
        url = ensure(h, a.port)
        if url:
            print(url)
        return 0  # a job run never fails on the dashboard
    print("stopped" if stop(h, a.port) else "not stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
