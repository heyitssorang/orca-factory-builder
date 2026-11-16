#!/usr/bin/env python3
"""Benchmark harness: factory skill (old = main) vs (new = this tree) on the same tasks.

  bench.py calibrate [--models opus,sonnet,haiku]   fit bench/prices.json from Claude's own costUSD
  bench.py setup   --root DIR [--new-src DIR] [--old-ref main]
  bench.py collect --root DIR            per-job time, tokens, process facts  -> DIR/metrics.json
  bench.py grade   --root DIR            hidden accept tests, suite, diff     -> DIR/grades.json
  bench.py pack    --root DIR            blinded diff pairs for the judge     -> DIR/judge/
  bench.py report  --root DIR            tables                               -> DIR/report.md

Arms are isolated: each has its own repo clone of bench/seed, its own Claude home (CLAUDE_CONFIG_DIR)
with that arm's skill and agents installed, and nothing is pushed anywhere (no remote).
Only the `new` arm opts in to Devin (`builder: devin`), and it only ever sees the synthetic seed.
The harness itself never starts a model; `setup` writes the launch commands to DIR/<arm>/RUN.md.
"""
import argparse
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# BENCH_ARMS=sonnet,new compares the new skill with the Sonnet builder (`sonnet`, no `builder:` line)
# against the new skill with Devin (`new`); `old` is the skill at --old-ref.
ARMS = tuple(os.environ.get("BENCH_ARMS", "old,new").split(","))
TASKS = sorted(p.name for p in (HERE / "tasks").iterdir() if p.is_dir())
PRICES = HERE / "prices.json"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
PROMPT = ("For this run the factory scripts directory `S` is `{scripts}` (not "
          "`~/.claude/skills/factory/scripts`); use it everywhere the skill says `S`. "
          "Run the factory on every task file in {tasks}: for each `<task>/task.md`, in order, "
          "run `/factory backlog add` with the file's text as the feature request, then run "
          "`/factory run --jobs 4` and wait until every job has settled. Do not accept, rework "
          "or ship anything. Approve no install commands (the repo has none). "
          "Benchmark rule: in every spec worker task add the sentence \"The scout and the planner are "
          "mandatory: the spec stage is not settled until context.md, spec.md and plan.md all "
          "exist; if one is missing, run that subagent again once.\" "
          "When done, print `/factory status`.")


def repo_of(root: Path, arm: str) -> Path:
    """Unique directory names: Orca keys worktree paths by the repo directory name."""
    return root / arm / f"bench-{arm}"


def sh(*cmd, cwd=None, check=True, env=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if check and p.returncode:
        raise SystemExit(f"{' '.join(map(str, cmd))}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def git(repo, *a, check=True):
    return sh("git", "-C", str(repo), *a, check=check)


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- setup
def setup(root: Path, new_src: Path, old_ref: str, arms=ARMS) -> None:
    root.mkdir(parents=True, exist_ok=True)
    home = Path.home() / ".claude"
    for arm in arms:
        a = root / arm
        if a.exists():
            raise SystemExit(f"{a} exists; remove it first (bench never overwrites an arm)")
        repo, chome, skill_src = repo_of(root, arm), a / "home", a / "src"
        shutil.copytree(HERE / "seed", repo, ignore=shutil.ignore_patterns("__pycache__"))
        if arm == "new":
            with (repo / "AGENTS.md").open("a") as f:
                f.write("- builder: devin\n")
        if arm == "old":
            sh("git", "-C", str(ROOT), "worktree", "add", "--detach", str(skill_src), old_ref)
        else:
            shutil.copytree(new_src, skill_src, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "bench", ".impeccable"))
        (chome / "agents").mkdir(parents=True)
        (chome / "skills").mkdir()
        for dep in ("review-verifier-p2.md", "review-verifier-p2-security.md"):
            shutil.copy(home / "agents" / dep, chome / "agents" / dep)
        shutil.copytree(home / "skills" / "differential-review",
                        chome / "skills" / "differential-review")
        if (home / ".credentials.json").exists():
            os.symlink(home / ".credentials.json", chome / ".credentials.json")
        if (home / "settings.json").exists():
            shutil.copy(home / "settings.json", chome / "settings.json")
        if (home / "factory-lean.json").exists():   # from `factory_lean.py learn`
            shutil.copy(home / "factory-lean.json", chome / "factory-lean.json")
        sh(sys.executable, str(skill_src / "scripts" / "install.py"), "install",
           "--claude-home", str(chome))
        # Orca workers do not honor CLAUDE_CONFIG_DIR (they read ~/.claude), but they do read
        # <cwd>/.claude/agents, which beats the user-level agents of the same name. Commit this
        # arm's personas into the repo so every worktree (and the spec worker) uses them.
        (repo / ".claude" / "agents").mkdir(parents=True)
        for f in sorted((chome / "agents").glob("*.md")):
            shutil.copy(f, repo / ".claude" / "agents" / f.name)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=bench", "-c", "user.email=bench@example.invalid",
            "commit", "-qm", "seed")
        (a / "RUN.md").write_text(
            f"# arm `{arm}`\n\nrepo: `{repo}`\nclaude home: `{chome}`\n\n```\n"
            f"cd {repo} && CLAUDE_CONFIG_DIR={chome} claude --permission-mode bypassPermissions --effort medium "
            f"--output-format json -p {shlex.quote(PROMPT.format(tasks=HERE / 'tasks', scripts=chome / 'skills' / 'factory' / 'scripts'))} "
            f"> {a / 'coordinator.json'}\n```\n\n`coordinator.json` is Claude's own accounting for "
            f"the coordinator session (the same numbers `/cost` shows). Worker sessions are "
            f"summed from their transcripts. Run `/cost` yourself in an interactive session to "
            f"compare; this harness cannot run slash commands.\n", encoding="utf-8")
    print(f"ready: {" and ".join(f"{root}/{a}" for a in arms)} (see RUN.md in each). Nothing has run.")


# ---------------------------------------------------------------- collect
def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def usage_of(path: Path) -> tuple[dict, str | None, str | None]:
    """{model: {in,out,cache_read,cache_write,n}} of one transcript, plus first/last timestamp.
    A streamed message repeats its usage, so the last entry per message id wins."""
    last, first_ts, last_ts = {}, None, None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        ts = o.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        m = o.get("message")
        if isinstance(m, dict) and isinstance(m.get("usage"), dict) and m.get("id"):
            last[m["id"]] = (m.get("model") or "?", m["usage"])
    out: dict = {}
    for model, u in last.values():
        d = out.setdefault(model, dict(inp=0, out=0, cache_read=0, cache_write=0, n=0))
        d["inp"] += u.get("input_tokens", 0) or 0
        d["out"] += u.get("output_tokens", 0) or 0
        d["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
        d["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
        d["n"] += 1
    return out, first_ts, last_ts


def add_usage(total: dict, part: dict) -> None:
    for model, d in part.items():
        t = total.setdefault(model, dict(inp=0, out=0, cache_read=0, cache_write=0, n=0))
        for k, v in d.items():
            t[k] += v


def project_dir(path: Path, base: Path | None = None) -> Path:
    return (base or CLAUDE_PROJECTS) / re.sub(r"[^A-Za-z0-9]", "-", str(path))


def sessions(d: Path) -> list[Path]:
    """Transcripts of a project dir, including subagent transcripts nested below it."""
    return sorted(d.rglob("*.jsonl")) if d.is_dir() else []


def first_user_text(path: Path, limit=40000) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def collect_arm(root: Path, arm: str) -> dict:
    a = root / arm
    repo = repo_of(root, arm)
    jobs_dir = repo / ".factory" / "jobs"
    jobs = {}
    repo_sessions = sessions(project_dir(repo))              # workers on the main workspace
    coord_sessions = sessions(project_dir(repo, a / "home" / "projects"))  # coordinator
    claimed: set[Path] = set()
    for jd in sorted(p for p in jobs_dir.glob("*") if (p / "job.json").is_file()):
        j = jload(jd / "job.json", {})
        jid = j["id"]
        usage: dict = {}
        spans = []
        # Worker sessions in the job's own worktree.
        wt = Path(j.get("worktree") or "")
        born = iso(j["created"]) if j.get("created") else None
        for s in sessions(project_dir(wt)) if str(wt) else []:
            u, t0, t1 = usage_of(s)
            if born and t0 and iso(t0) < born:   # a previous run that reused this worktree path
                continue
            add_usage(usage, u)
            spans.append((t0, t1))
        # Spec-stage workers run in the repo's main workspace: match them by job id.
        for s in repo_sessions:
            if s in claimed:
                continue
            if f"factory/{jid}" in first_user_text(s) or jid in first_user_text(s, 6000):
                u, t0, t1 = usage_of(s)
                if born and t0 and iso(t0) < born:
                    continue
                add_usage(usage, u)
                spans.append((t0, t1))
                claimed.add(s)
        rounds = sorted(p.name for p in jd.glob("round-*"))
        build = (jd / "build.md")
        jobs[jid] = {
            "stage": j.get("stage"), "round": j.get("round"), "risk": j.get("risk"),
            "hold": j.get("hold"), "reviews": j.get("reviews"), "verified": j.get("verified"),
            "workers": len(j.get("workers") or []), "branch": j.get("branch"),
            "base": j.get("base"), "merge_commit": j.get("merge_commit"),
            "created": j.get("created"), "updated": j.get("updated"),
            "wall_s": (iso(j["updated"]) - iso(j["created"])).total_seconds()
            if j.get("created") and j.get("updated") else None,
            "worker_span_s": (max(iso(t1) for _, t1 in spans if t1)
                              - min(iso(t0) for t0, _ in spans if t0)).total_seconds()
            if any(t0 and t1 for t0, t1 in spans) else None,
            "rounds": len(rounds),
            "builder_engine": "devin" if (jd / "devin-out.md").exists() else "claude",
            "devin_brief_bytes": (jd / "devin-brief.md").stat().st_size
            if (jd / "devin-brief.md").exists() else 0,
            "devin_fallback": bool(j.get("workers")) and (jd / "devin-out.md").exists()
            and not (build.exists() and build.read_text().startswith("STATUS: READY")),
            "build_status": build.read_text().splitlines()[0] if build.exists() else None,
            "has_plan": (jd / "plan.md").exists(), "has_context": (jd / "context.md").exists(),
            "checks": (jd / "round-1" / "checks.md").read_text().splitlines()[0]
            if (jd / "round-1" / "checks.md").exists() else None,
            "tokens": usage,
        }
    coord: dict = {}
    for s in coord_sessions:
        add_usage(coord, usage_of(s)[0])
    other: dict = {}   # worker sessions on the main workspace that match no job
    for s in repo_sessions:
        if s not in claimed:
            add_usage(other, usage_of(s)[0])
    runs = [jload(p, {}) for p in (repo / ".factory" / "runs").glob("*.json")] \
        if (repo / ".factory" / "runs").is_dir() else []
    times = [iso(j["created"]) for j in jobs.values() if j["created"]] + \
            [iso(j["updated"]) for j in jobs.values() if j["updated"]]
    cj = jload(a / "coordinator.json", {}) or {}
    session = {"cost_usd": cj.get("total_cost_usd"), "duration_s": (cj.get("duration_ms") or 0) / 1000
               or None, "api_s": (cj.get("duration_api_ms") or 0) / 1000 or None,
               "turns": cj.get("num_turns"),
               "models": {m: {"inp": u.get("inputTokens", 0), "out": u.get("outputTokens", 0),
                              "cache_read": u.get("cacheReadInputTokens", 0),
                              "cache_write": u.get("cacheCreationInputTokens", 0),
                              "cost_usd": u.get("costUSD")}
                          for m, u in (cj.get("modelUsage") or {}).items()}}
    return {"jobs": jobs, "coordinator_tokens": coord, "unattributed_tokens": other,
            "coordinator_session": session,
            "runs": len(runs),
            "arm_wall_s": (max(times) - min(times)).total_seconds() if times else None}


def cost(tokens: dict, prices: dict) -> float | None:
    total = 0.0
    for model, d in tokens.items():
        key = next((k for k in prices if k in model), None)
        p = prices.get(key) if key else None
        if not p or any(v is None for v in p.values()):
            return None
        total += (d["inp"] * p["input"] + d["out"] * p["output"]
                  + d["cache_read"] * p["cache_read"] + d["cache_write"] * p["cache_write"]) / 1e6
    return total


# ---------------------------------------------------------------- calibrate
CAL_PROMPTS = ["Reply with exactly one word: ok",
               "Write the integers from 1 to 120 separated by single spaces and nothing else.",
               "Write the integers from 1 to 400 separated by single spaces and nothing else.",
               # Many turns re-read the cached prompt: this is what makes cache reads large and
               # separates their price from the cache-write price.
               "Run the shell command `echo 1`, then `echo 2`, then `echo 3`, then `echo 4`, then "
               "`echo 5`, each as its own separate tool call (never combined). Then reply: done.",
               "Run `echo a` through `echo j` (ten commands: a b c d e f g h i j), each as its "
               "own separate tool call (never combined). Then reply: done."]
# The 1h and 5m cache-write prices are tied to the input price (2x, 1.25x: the cache write
# multipliers); input, output and cache-read prices are fitted freely from Claude's own `costUSD`.
CACHE_5M_X, CACHE_1H_X = 1.25, 2.0


def _solve3(m, v):
    """Solve a 3x3 linear system by Cramer's rule; None when singular."""
    def det(a):
        return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    d = det(m)
    if abs(d) < 1e-18 * max(1e-30, abs(m[0][0] * m[1][1] * m[2][2])):
        return None
    return [det([[v[r] if c == k else m[r][c] for c in range(3)] for r in range(3)]) / d
            for k in range(3)]


def fit_prices(points: list[dict]) -> dict:
    """Least squares for USD per million tokens: input, output and cache read, with
    cost = p_in * (inp + 1.25 w5 + 2 w1) + p_out * out + p_read * read (all in millions)."""
    rows = [((p["inp"] + CACHE_5M_X * p["w5"] + CACHE_1H_X * p["w1"]) / 1e6, p["out"] / 1e6,
             p["read"] / 1e6, p["cost"]) for p in points]
    m = [[sum(r[i] * r[j] for r in rows) for j in range(3)] for i in range(3)]
    v = [sum(r[i] * r[3] for r in rows) for i in range(3)]
    sol = len(rows) >= 4 and _solve3(m, v)
    if not sol:
        raise ValueError("not enough variation in the calibration runs")
    p_in, p_out, p_read = sol
    resid = max(abs(p_in * r[0] + p_out * r[1] + p_read * r[2] - r[3]) / r[3] for r in rows)
    w = sum(p["w5"] + p["w1"] for p in points)
    w1_share = sum(p["w1"] for p in points) / w if w else 1.0
    return {"input": round(p_in, 4), "output": round(p_out, 4), "cache_read": round(p_read, 4),
            "cache_write": round(p_in * (CACHE_1H_X if w1_share >= 0.5 else CACHE_5M_X), 4),
            "residual": resid, "runs": len(points)}


def calibrate(models, claude="claude", out=PRICES, max_residual=0.03) -> dict:
    """Run tiny `claude -p` calls per model, read Claude's own per-model costUSD and token counts,
    fit prices, write prices.json. Uses real tokens (a few dollars at most, mostly Opus)."""
    import tempfile
    points: dict = {}
    with tempfile.TemporaryDirectory() as cwd:
        for model in models:
            for prompt in CAL_PROMPTS:
                p = subprocess.run([claude, "-p", prompt, "--model", model, "--output-format",
                                    "json", "--allowedTools", "Bash(echo:*)"],
                                   cwd=cwd, capture_output=True, text=True, timeout=600)
                try:
                    usage = json.loads(p.stdout).get("modelUsage") or {}
                except ValueError:
                    raise SystemExit(f"calibrate: {model}: no JSON from claude ({p.stderr.strip()[:200]})")
                for m, u in usage.items():
                    if not u.get("costUSD"):
                        continue
                    points.setdefault(m, []).append({
                        "inp": u.get("inputTokens", 0), "out": u.get("outputTokens", 0),
                        "read": u.get("cacheReadInputTokens", 0),
                        "w5": u.get("cacheCreationEphemeral5mInputTokens", 0),
                        "w1": u.get("cacheCreationEphemeral1hInputTokens", 0)
                        or (u.get("cacheCreationInputTokens", 0)
                            - u.get("cacheCreationEphemeral5mInputTokens", 0)),
                        "cost": u["costUSD"]})
    result, bad = {}, []
    for m, pts in points.items():
        try:
            fit = fit_prices(pts)
        except ValueError as e:
            bad.append(f"{m}: {e}")
            continue
        if fit["residual"] > max_residual:
            bad.append(f"{m}: residual {fit['residual']:.1%} > {max_residual:.0%}; "
                       f"the cache multipliers do not hold, set prices by hand")
            continue
        result[m] = fit
    if bad:
        print("calibrate: not written for:\n  " + "\n  ".join(bad), file=sys.stderr)
    table = {"_note": "USD per million tokens, fitted by `bench.py calibrate` from Claude's own "
                      "costUSD (cache writes assumed 1.25x/2x input; input, output and cache read "
                      "fitted; residual is the worst relative error of the fit).",
             "_calibrated": datetime.now().isoformat(timespec="seconds")}
    for m, fit in result.items():
        family = next((k for k in ("opus", "sonnet", "haiku", "fable") if k in m), m)
        table[family] = {k: fit[k] for k in ("input", "output", "cache_read", "cache_write")}
        table[family]["_fit"] = {"model": m, "runs": fit["runs"], "residual": round(fit["residual"], 5)}
    if result:
        Path(out).write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    return table


# ---------------------------------------------------------------- grade
def run_accept(task: str, checkout: Path) -> dict:
    env = dict(os.environ, BENCH_REPO=str(checkout), PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, str(HERE / "tasks" / task / "accept_test.py")],
                       cwd=checkout, capture_output=True, text=True, env=env, timeout=300)
    m = re.search(r"Ran (\d+) tests?", p.stderr)
    bad = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", p.stderr))
    n = int(m.group(1)) if m else 0
    return {"tests": n, "failed": bad if n else None, "ok": p.returncode == 0 and n > 0,
            "tail": p.stderr.strip().splitlines()[-1] if p.stderr.strip() else ""}


def task_of(feature: str) -> str | None:
    """The task a job implements, by the task.md text the coordinator was given."""
    f = " ".join((feature or "").split())[:60]
    for t in TASKS:
        text = " ".join((HERE / "tasks" / t / "task.md").read_text().split())
        if f and (f in text or text[:60] in " ".join((feature or "").split())):
            return t
    return None


def grade_arm(root: Path, arm: str) -> dict:
    repo = repo_of(root, arm)
    out = {}
    for jd in sorted(p for p in (repo / ".factory" / "jobs").glob("*") if (p / "job.json").is_file()):
        j = jload(jd / "job.json", {})
        task = task_of(j.get("feature"))
        branch, base = j.get("branch"), j.get("base")
        g = {"task": task, "branch_exists": bool(branch and git(
            repo, "rev-parse", "--verify", "-q", branch, check=False))}
        if not (g["branch_exists"] and task and base):
            out[j["id"]] = g
            continue
        chk = root / arm / "checkouts" / j["id"]
        shutil.rmtree(chk, ignore_errors=True)
        git(repo, "worktree", "prune")
        git(repo, "worktree", "add", "--detach", str(chk), branch)
        suite = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                               cwd=chk, capture_output=True, text=True,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        build = subprocess.run([sys.executable, "-m", "compileall", "-q", "src"], cwd=chk,
                               capture_output=True, text=True)
        stat = git(repo, "diff", "--numstat", f"{base}...{branch}").splitlines()
        files = [ln.split("\t")[2] for ln in stat if ln.count("\t") >= 2]
        added = sum(int(ln.split("\t")[0]) for ln in stat if ln.split("\t")[0].isdigit())
        removed = sum(int(ln.split("\t")[1]) for ln in stat if ln.split("\t")[1].isdigit())
        g.update({
            "accept": run_accept(task, chk),
            "suite_ok": suite.returncode == 0, "build_ok": build.returncode == 0,
            "files": len(files), "lines_added": added, "lines_removed": removed,
            "tests_added": any(f.startswith("tests/") for f in files),
            "touches_factory_or_claude": any(f.startswith((".factory", ".claude")) for f in files),
            "commits": int(git(repo, "rev-list", "--count", f"{base}..{branch}") or 0),
        })
        git(repo, "worktree", "remove", "--force", str(chk), check=False)
        out[j["id"]] = g
    return out


# ---------------------------------------------------------------- judge packets
RUBRIC = """You are grading two anonymous implementations (A and B) of the same task for a code
review benchmark. You see only the task and each diff. Score each 1-5 on: correctness against the
task text; simplicity (smallest change that fully works); fit with the surrounding code's style;
test quality (do the tests prove the requirement); safety (no needless risk, no scope creep).
Then say which is better overall or `tie`. Reply as JSON:
{"A": {"correctness":n,"simplicity":n,"style":n,"tests":n,"safety":n},
 "B": {...}, "winner": "A|B|tie", "reason": "<=2 sentences"}"""


def pack(root: Path) -> None:
    out = root / "judge"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    rng = random.Random(1)
    key = {}
    grades = {arm: jload(root / "grades.json", {}).get(arm, {}) for arm in ARMS}
    by_task = {arm: {g["task"]: (jid, g) for jid, g in grades[arm].items() if g.get("task")}
               for arm in ARMS}
    for task in TASKS:
        if not all(task in by_task[arm] for arm in ARMS):
            continue
        diffs = {}
        for arm in ARMS:
            jid, _ = by_task[arm][task]
            repo = repo_of(root, arm)
            j = jload(repo / ".factory" / "jobs" / jid / "job.json", {})
            diffs[arm] = git(repo, "diff", "--no-color", f"{j['base']}...{j['branch']}")
        order = list(ARMS)
        rng.shuffle(order)
        text = (RUBRIC + "\n\n## Task\n" + (HERE / "tasks" / task / "task.md").read_text()
                + "\n## Implementation A (diff)\n```diff\n" + diffs[order[0]] + "\n```\n"
                + "\n## Implementation B (diff)\n```diff\n" + diffs[order[1]] + "\n```\n")
        (out / f"{task}.md").write_text(text, encoding="utf-8")
        key[task] = {"A": order[0], "B": order[1]}
    (out / "KEY.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
    print(f"{len(key)} packets in {out}. Judge each .md with a model that has not seen KEY.json, "
          f"save the JSON reply as <task>.json beside it, then run `report`.")


# ---------------------------------------------------------------- report
def money(t: dict) -> str:
    return f"${t['cost']:.2f}" if t["cost_ok"] else "n/a (fill prices.json)"


def fmt_s(s):
    return "-" if s is None else f"{int(s // 60)}m{int(s % 60):02d}s"


def tok_total(tokens: dict) -> int:
    return sum(d["inp"] + d["out"] + d["cache_write"] for d in tokens.values())


def report(root: Path) -> None:
    metrics, grades = jload(root / "metrics.json", {}), jload(root / "grades.json", {})
    prices = jload(PRICES, {})
    judge_key = jload(root / "judge" / "KEY.json", {})
    verdicts: dict = {}
    for task, k in judge_key.items():
        v = jload(root / "judge" / f"{task}.json")
        if v:
            verdicts[task] = {k[x]: sum(v.get(x, {}).values()) for x in ("A", "B")}
            w = v.get("winner")
            verdicts[task]["winner"] = k.get(w) if w in k else "tie"
    L = [f"# Factory benchmark: {' vs '.join(ARMS)}", ""]
    L += ["## Per task", "", "| task | arm | engine | accept | suite | rounds | stage | wall | "
          "tokens (in+out+cache write) | cost | judge |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    tot = {arm: dict(wall=0.0, tok=0, cost=0.0, cost_ok=True, accept=0, w=0, n=0, wt=0)
           for arm in ARMS}
    for task in TASKS:
        for arm in ARMS:
            hit = [(jid, g) for jid, g in grades.get(arm, {}).items() if g.get("task") == task]
            if not hit:
                L.append(f"| {task} | {arm} | - | not run | | | | | | | |")
                continue
            jid, g = hit[0]
            m = metrics[arm]["jobs"][jid]
            c = cost(m["tokens"], prices)
            acc = g.get("accept", {})
            w = json.loads((HERE / "tasks" / task / "meta.json").read_text())["weight"]
            t = tot[arm]
            t["n"] += 1
            t["w"] += w
            t["wt"] += w if acc.get("ok") else 0
            t["accept"] += 1 if acc.get("ok") else 0
            t["wall"] += m["wall_s"] or 0
            t["tok"] += tok_total(m["tokens"])
            if c is None:
                t["cost_ok"] = False
            else:
                t["cost"] += c
            jv = verdicts.get(task, {})
            L.append(f"| {task} | {arm} | {m['builder_engine']} | "
                     f"{'PASS' if acc.get('ok') else 'FAIL ' + str(acc.get('failed'))} | "
                     f"{'ok' if g.get('suite_ok') else 'FAIL'} | {m['round']} | {m['stage']} | "
                     f"{fmt_s(m['wall_s'])} | {tok_total(m['tokens']):,} | "
                     f"{'n/a' if c is None else f'${c:.2f}'} | "
                     f"{jv.get(arm, '')}{' (win)' if jv.get('winner') == arm else ''} |")
    L += ["", "## Totals", "", "| arm | accept pass | weighted pass | sum of job walls | arm wall "
          "(parallel) | tokens | cost | coordinator tokens (shared) |",
          "|---|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        t = tot[arm]
        co = tok_total(metrics.get(arm, {}).get("coordinator_tokens", {}))
        L.append(f"| {arm} | {t['accept']}/{t['n']} | {t['wt']}/{t['w']} | {fmt_s(t['wall'])} | "
                 f"{fmt_s(metrics.get(arm, {}).get('arm_wall_s'))} | {t['tok']:,} | "
                 f"{money(t)} | {co:,} |")
    L += ["", "## Session usage (the `/cost` view)", "",
          "| arm | coordinator session cost (Claude) | coordinator cost from transcripts x prices | "
          "worker cost (transcripts x prices) | total | Devin |", "|---|---|---|---|---|---|"]
    for arm in ARMS:
        ses = metrics.get(arm, {}).get("coordinator_session", {}) or {}
        mine = cost({m: dict(d, n=0) for m, d in ses.get("models", {}).items()}, prices)
        workers = sum(cost(j["tokens"], prices) or 0 for j in metrics.get(arm, {}).get("jobs", {}).values())
        ok = all(cost(j["tokens"], prices) is not None for j in metrics.get(arm, {}).get("jobs", {}).values())
        real = ses.get("cost_usd")
        total = (real or 0) + workers if ok and real is not None else None
        L.append(f"| {arm} | {'n/a' if real is None else f'${real:.2f}'} | "
                 f"{'n/a' if mine is None else f'${mine:.2f}'} | "
                 f"{f'${workers:.2f}' if ok else 'n/a'} | "
                 f"{'n/a' if total is None else f'${total:.2f}'} | "
                 f"{'$0, not metered' if arm == 'new' else '-'} |")
    L += ["", "The coordinator column is Claude's own number; if the next column differs, "
          "`prices.json` is wrong and every worker cost above is off by the same factor.", ""]
    L += ["", "## Process and risk signals", "",
          "| task | arm | files | +lines | -lines | tests added | verified (conf/FP/unv) | "
          "reviews | risk | devin fallback | checks |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for task in TASKS:
        for arm in ARMS:
            for jid, g in grades.get(arm, {}).items():
                if g.get("task") != task:
                    continue
                m = metrics[arm]["jobs"][jid]
                v = m["verified"] or {}
                L.append(f"| {task} | {arm} | {g.get('files', '-')} | {g.get('lines_added', '-')} | "
                         f"{g.get('lines_removed', '-')} | {g.get('tests_added', '-')} | "
                         f"{v.get('confirmed', 0)}/{v.get('false_positive', 0)}/{v.get('unverifiable', 0)} | "
                         f"{'/'.join(f'{k[0]}:{x[0]}' for k, x in (m['reviews'] or {}).items())} | "
                         f"{m['risk']} | {m['devin_fallback']} | {m['checks'] or '-'} |")
    L += ["", "Notes: tokens exclude cache reads (they dominate counts and are priced separately); "
          "Devin tokens are not visible to Claude transcripts, so the new arm's coding cost is "
          "time only. Cost needs `bench/prices.json`. One run per task is a direction, not "
          "significance.", ""]
    (root / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(root / "report.md")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("--models", default="opus,sonnet,haiku")
    c.add_argument("--claude", default="claude")
    for name in ("setup", "collect", "grade", "pack", "report"):
        s = sub.add_parser(name)
        s.add_argument("--root", required=True)
        if name == "setup":
            s.add_argument("--new-src", default=str(ROOT))
            s.add_argument("--old-ref", default="main")
            s.add_argument("--arms", default=",".join(ARMS), help="rebuild only these arms")
    a = ap.parse_args(argv)
    if a.cmd == "calibrate":
        t = calibrate([m for m in a.models.split(",") if m], a.claude)
        print(json.dumps(t, indent=2))
        return 0 if any(k in t for k in ("opus", "sonnet", "haiku", "fable")) else 1
    root = Path(a.root).expanduser().resolve()
    if a.cmd == "setup":
        setup(root, Path(a.new_src), a.old_ref, tuple(x for x in a.arms.split(',') if x in ARMS))
    elif a.cmd == "collect":
        (root / "metrics.json").write_text(json.dumps({arm: collect_arm(root, arm) for arm in ARMS},
                                                      indent=1), encoding="utf-8")
    elif a.cmd == "grade":
        (root / "grades.json").write_text(json.dumps({arm: grade_arm(root, arm) for arm in ARMS},
                                                     indent=1), encoding="utf-8")
    elif a.cmd == "pack":
        pack(root)
    else:
        report(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
