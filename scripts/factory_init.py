#!/usr/bin/env python3
"""/factory init: make a repo ready for the factory (spec amendment 2026-10-17).

inspect   what the repo has: git, commits, mode (ado or local), default branch, AGENTS.md keys,
          tracked browser config that would turn the ux/ui lenses off.
git-init  create the directory and an empty-commit git repo (refuses inside another repo).
agents    detect install/test/build/dev/ui_globs and propose AGENTS.md lines for the keys the
          working-tree AGENTS.md lacks; --write appends them. Never commits.
home      create <repo>/.factory/ with its .gitignore and an empty backlog.md.
migrate   copy the old central state (~/.orca-reports/factory: jobs, runs, backlog lines) into
          each repo's .factory/; the old folder is left as it was.
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory_job as fj  # noqa: E402
import factory_push as fp  # noqa: E402

KEYS = ("install", "test", "build", "dev", "ui_globs")
_LINE = re.compile(r"^(?:[-*]\s+)?(install|test|build|dev|ui_globs):\s*(.*?)\s*$")
BROWSER_CONFIG = (".mcp.json", ".claude/settings.local.json")
STATIC_DIRS = ("web", "public", "site", "static", "www", ".")
_NPM_PLACEHOLDER = "no test specified"


class InitError(Exception):
    pass


def _toplevel(path: Path) -> Path | None:
    """The work tree containing path, or its nearest existing ancestor (path may not exist)."""
    while not path.is_dir():
        if path.parent == path:
            return None
        path = path.parent
    p = fp._git(path, "rev-parse", "--show-toplevel", check=False)
    return Path(p.stdout.strip()).resolve() if p.returncode == 0 and p.stdout.strip() else None


def _has_commit(repo: Path) -> bool:
    return fp._git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}",
                   check=False).returncode == 0


def parse_agents(text: str) -> dict[str, str]:
    """The factory keys of an AGENTS.md text: first of each wins, a bullet and surrounding
    backticks are dropped (as factory_integrate.read_agents_commands reads them)."""
    out: dict[str, str] = {}
    for line in text.split("\n"):
        m = _LINE.match(line.strip())
        if not m or m.group(1) in out:
            continue
        v = m.group(2)
        if len(v) >= 2 and v[0] == v[-1] == "`":
            v = v[1:-1].strip()
        if v:
            out[m.group(1)] = v
    return out


def _make_targets(repo: Path) -> set[str]:
    f = repo / "Makefile"
    if not f.is_file():
        return set()
    text = f.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"^([A-Za-z0-9_-]+)\s*:(?!=)", text, re.M))


def _node(repo: Path) -> dict[str, str]:
    try:
        pkg = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    scripts = scripts if isinstance(scripts, dict) else {}
    if (repo / "pnpm-lock.yaml").exists():
        pm, install, run, extra = "pnpm", "pnpm install --frozen-lockfile", "pnpm run", ""
    elif (repo / "yarn.lock").exists():
        pm, install, run, extra = "yarn", "yarn install --frozen-lockfile", "yarn", ""
    elif (repo / "bun.lockb").exists() or (repo / "bun.lock").exists():
        pm, install, run, extra = "bun", "bun install --frozen-lockfile", "bun run", ""
    else:
        pm, install, run, extra = "npm", ("npm ci" if (repo / "package-lock.json").exists()
                                          else "npm install"), "npm run", "-- "
    out = {"install": install}
    test = scripts.get("test")
    if isinstance(test, str) and _NPM_PLACEHOLDER not in test:
        out["test"] = f"{run} test" if pm == "bun" else f"{pm} test"
    if "build" in scripts:
        out["build"] = f"{run} build"
    for name in ("dev", "start"):
        if name in scripts:
            out["dev"] = f"{run} {name} {extra}--port {{port}}"
            break
    return out


def _python(repo: Path) -> dict[str, str]:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if not any((repo / m).exists() for m in markers) and not any(repo.glob("*.py")):
        return {}
    pyproject = repo / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""
    out = {}
    if ((repo / "pytest.ini").exists() or (repo / "conftest.py").exists()
            or "pytest" in text):
        out["test"] = "python3 -m pytest -q"
    elif (repo / "tests").is_dir():
        out["test"] = "python3 -m unittest discover -s tests -v"
    return out


def _static(repo: Path) -> dict[str, str]:
    for d in STATIC_DIRS:
        if (repo / d / "index.html").is_file():
            out = {"dev": f"python3 -m http.server {{port}} --directory {d}"}
            if d != ".":
                out["ui_globs"] = f"{d}/*"
            return out
    return {}


def detect(repo: Path) -> dict[str, str]:
    """Best guess per key: Makefile targets first, then the ecosystem (node, cargo, go,
    python), then a static index.html for dev. Keys with no guess are left out."""
    out: dict[str, str] = {}
    for t in ("install", "test", "build", "dev"):
        if t in _make_targets(repo):
            out[t] = f"make {t}"
    eco: dict[str, str] = {}
    if (repo / "package.json").is_file():
        eco = _node(repo)
    elif (repo / "Cargo.toml").is_file():
        eco = {"test": "cargo test", "build": "cargo build"}
    elif (repo / "go.mod").is_file():
        eco = {"test": "go test ./...", "build": "go build ./..."}
    else:
        eco = _python(repo)
    for k, v in eco.items():
        out.setdefault(k, v)
    if "dev" not in out:
        for k, v in _static(repo).items():
            out.setdefault(k, v)
    return out


def _tracked(repo: Path, path: str) -> bool:
    return fp._git(repo, "ls-files", "--error-unmatch", "--", path,
                   check=False).returncode == 0


def inspect(path: Path) -> dict:
    path = path.expanduser().resolve()
    top = _toplevel(path)
    out: dict = {"path": str(path), "exists": path.is_dir(), "git": top == path,
                 "inside_repo": str(top) if top and top != path else None,
                 "commits": False, "mode": None, "origin_url": None, "default": None,
                 "agents_md": None, "browser_config_tracked": [], "factory_home": None}
    if top != path:
        return out
    out["commits"] = _has_commit(path)
    out.update(fp.repo_info(path))
    if out["default"]:
        p = fp._git(path, "show", f"refs/heads/{out['default']}:AGENTS.md", check=False)
        committed = parse_agents(p.stdout) if p.returncode == 0 else None
    else:
        committed = None
    f = path / "AGENTS.md"
    out["agents_md"] = {
        "file": f.is_file(),
        "keys": sorted(parse_agents(f.read_text(encoding="utf-8", errors="replace"))
                       if f.is_file() else {}),
        "committed_keys": sorted(committed) if committed is not None else None,
    }
    out["browser_config_tracked"] = [b for b in BROWSER_CONFIG if _tracked(path, b)]
    h = path / fj.HOME_DIR
    out["factory_home"] = {"exists": h.is_dir(), "tracked": {
        f: _tracked(path, f"{fj.HOME_DIR}/{f}") for f in (".gitignore", "backlog.md")}}
    return out


def git_init(path: Path, branch: str = "main") -> dict:
    """Create path (if needed) as a git repo on branch with one empty commit. A repo that
    already has a commit is left alone; one without gets the empty commit."""
    path = path.expanduser().resolve()
    if fp._git(Path("/"), "check-ref-format", f"refs/heads/{branch}",
               check=False).returncode or branch.startswith("-"):
        raise InitError("invalid branch name")
    top = _toplevel(path)
    if top and top != path:
        raise InitError(f"{path} is inside the git repo {top}")
    created = top is None
    if created:
        path.mkdir(parents=True, exist_ok=True)
        fp._git(path, "init", "-q", "-b", branch)
    commit = None
    if not _has_commit(path):
        p = fp._git(path, "commit", "-q", "--allow-empty", "--no-verify", "-m",
                    "Initial commit", check=False)
        if p.returncode:
            raise InitError("git commit failed (set user.name and user.email): "
                            + (p.stderr.strip().splitlines() or [""])[-1])
        commit = fp._git(path, "rev-parse", "HEAD").stdout.strip()
    untracked = fp._git(path, "ls-files", "--others", "--exclude-standard", "-z",
                        raw=True).stdout
    return {"path": str(path), "created": created, "initial_commit": commit,
            "branch": fp._git(path, "symbolic-ref", "--short", "HEAD").stdout.strip(),
            "untracked": len([x for x in untracked.split("\0") if x])}


def agents(path: Path, sets: dict[str, str], write: bool) -> dict:
    """Propose lines for the keys AGENTS.md (working tree) lacks; sets override a guess (an
    empty value drops the key). write appends them; existing lines are never changed."""
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise InitError(f"{path} is not a directory")
    f = path / "AGENTS.md"
    text = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
    existing = parse_agents(text)
    guess = detect(path)
    proposed = {}
    for k in KEYS:
        if k in existing:
            continue
        v = sets[k] if k in sets else guess.get(k, "")
        if v:
            proposed[k] = v
    for k, v in proposed.items():
        if "`" in v or "\n" in v or "\r" in v:
            raise InitError(f"{k}: value may not contain backticks or newlines")
    lines = [f"- {k}: `{v}`" for k, v in proposed.items()]
    if write and lines:
        if not text:
            body = ("# Agents\n\nCommands the factory runs, read from the committed default "
                    "branch.\n\n" + "\n".join(lines) + "\n")
        else:
            body = (text if text.endswith("\n") else text + "\n") + (
                "\n## Factory\n\n" + "\n".join(lines) + "\n")
        f.write_text(body, encoding="utf-8")
    return {"file": str(f), "existing": existing, "proposed": proposed, "lines": lines,
            "missing": [k for k in KEYS if k not in existing and k not in proposed],
            "written": bool(write and lines)}


def make_home(path: Path) -> dict:
    """<repo>/.factory with .gitignore and backlog.md; reports which of the two are tracked."""
    path = path.expanduser().resolve()
    if _toplevel(path) != path:
        raise InitError(f"{path} is not the top of a git repo (run git-init first)")
    h = path / fj.HOME_DIR
    if h.is_symlink():
        raise InitError(f"{h} is a symlink")
    fj.prepare_home(h)
    (h / "backlog.md").touch()
    return {"home": str(h), "tracked": {f: _tracked(path, f"{fj.HOME_DIR}/{f}")
                                        for f in (".gitignore", "backlog.md")}}


OLD_HOME = Path.home() / ".orca-reports" / "factory"
_OLD_LINE = re.compile(r"^- \[([ ~x!])\] (.+?) :: (.*)$")


def _target_home(repo, skipped: list, what: str) -> Path | None:
    try:
        repo = Path(repo).resolve()
        if not repo.is_dir():
            raise InitError("repo not found")
        h = fj.repo_home(repo)
    except (InitError, fj.BadRequest, OSError, TypeError) as e:
        skipped.append({"item": what, "reason": f"{repo}: {e}"})
        return None
    return h


def migrate(old: Path, dry_run: bool = False) -> dict:
    """Copy jobs (with their .n-<n> number markers), runs (json, refs snapshot, lock dir) and
    backlog lines into the repo each names. Existing targets are never overwritten."""
    old = old.expanduser().resolve()
    copied: dict[str, list[str]] = {"jobs": [], "runs": [], "backlog": []}
    skipped: list[dict] = []
    jobs = old / "jobs"
    for d in sorted(jobs.iterdir()) if jobs.is_dir() else []:
        if d.name.startswith(".") or d.is_symlink() or not d.is_dir():
            continue
        try:
            job = fj.read_job(d)
        except (OSError, ValueError) as e:
            skipped.append({"item": f"job {d.name}", "reason": f"unreadable: {e}"})
            continue
        h = _target_home(job.get("repo"), skipped, f"job {d.name}")
        if h is None:
            continue
        dest = fj.jobs_dir(h) / d.name
        if dest.exists():
            skipped.append({"item": f"job {d.name}", "reason": f"{dest} exists"})
            continue
        if not dry_run:
            fj.prepare_home(h)
            fj.jobs_dir(h).mkdir(exist_ok=True)
            shutil.copytree(d, dest, symlinks=True)
            (fj.jobs_dir(h) / f".n-{fj.job_number(d.name):03d}").mkdir(mode=0o700,
                                                                     exist_ok=True)
        copied["jobs"].append(f"{d.name} -> {dest}")
    runs = old / "runs"
    for f in sorted(runs.glob("*.json")) if runs.is_dir() else []:
        rid = f.name[:-len(".json")]
        if f.is_symlink() or not re.fullmatch(r"\d{8}-\d{4}-[0-9a-f]{4}", rid):
            continue
        try:
            run = json.loads(f.read_text())
        except (OSError, ValueError) as e:
            skipped.append({"item": f"run {rid}", "reason": f"unreadable: {e}"})
            continue
        h = _target_home(run.get("repo") if isinstance(run, dict) else None, skipped, f"run {rid}")
        if h is None:
            continue
        dest = h / "runs" / f.name
        if dest.exists():
            skipped.append({"item": f"run {rid}", "reason": f"{dest} exists"})
            continue
        if not dry_run:
            fj.prepare_home(h)
            (h / "runs").mkdir(exist_ok=True)
            shutil.copy2(f, dest)
            snap = runs / f"{rid}.refs-before.json"
            if snap.is_file() and not snap.is_symlink():
                shutil.copy2(snap, h / "runs" / snap.name)
            (h / "runs" / f"{rid}.lock").mkdir(mode=0o700, exist_ok=True)
        copied["runs"].append(f"{rid} -> {dest}")
    bl = old / "backlog.md"
    for line in bl.read_text().splitlines() if bl.is_file() else []:
        m = _OLD_LINE.match(line)
        if not m:
            continue
        h = _target_home(m.group(2), skipped, f"backlog line {line!r}")
        if h is None:
            continue
        new = f"- [{m.group(1)}] {m.group(3)}"
        target = h / "backlog.md"
        have = target.read_text().splitlines() if target.is_file() else []
        if new in have:
            continue
        if not dry_run:
            fj.prepare_home(h)
            with open(target, "a", encoding="utf-8") as out:
                out.write(new + "\n")
        copied["backlog"].append(f"{new} -> {target}")
    return {"from": str(old), "dry_run": dry_run, "copied": copied, "skipped": skipped}


def _sets(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        k, sep, v = item.partition("=")
        if not sep or k not in KEYS:
            raise InitError(f"--set wants <key>=<value> with key in {', '.join(KEYS)}")
        out[k] = v.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory_init")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("inspect")
    i.add_argument("path")
    g = sub.add_parser("git-init")
    g.add_argument("path")
    g.add_argument("--branch", default="main")
    a_ = sub.add_parser("agents")
    a_.add_argument("path")
    a_.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    a_.add_argument("--write", action="store_true")
    hm = sub.add_parser("home")
    hm.add_argument("path")
    mg = sub.add_parser("migrate")
    mg.add_argument("--from", dest="old", default=str(OLD_HOME))
    mg.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "inspect":
            out = inspect(Path(a.path))
        elif a.cmd == "home":
            out = make_home(Path(a.path))
        elif a.cmd == "migrate":
            out = migrate(Path(a.old), a.dry_run)
        elif a.cmd == "git-init":
            out = git_init(Path(a.path), a.branch)
        else:
            out = agents(Path(a.path), _sets(a.set), a.write)
    except InitError as e:
        print(str(e), file=sys.stderr)
        return 2
    except (fp.GitError, OSError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
