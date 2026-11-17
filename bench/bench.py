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
SPEC_ONLY_RULE = (" Benchmark rule (spec-only run): a job is settled once its spec stage is settled. "
                  "Do not start step 3 or anything after it for any job: no worktree, no build, no "
                  "review. Leave each job in stage `build`, and do not wait for anything else.")


def build_prompt(scripts: Path, spec_only: bool = False) -> str:
    """The coordinator prompt; a spec-only run adds the rule that stops every job after its spec."""
    return PROMPT.format(tasks=HERE / "tasks", scripts=scripts) + (SPEC_ONLY_RULE if spec_only else "")


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
def register_with_orca(repo: Path) -> None:
    """Orca resolves `--worktree current` from the repo registry: an unregistered arm repo falls
    back to the *caller's* workspace (workers then run in the wrong repo, with the wrong agents,
    and report to the wrong terminal). Register it so every arm starts the same way."""
    orca = shutil.which("orca")
    if not orca:
        raise SystemExit("`orca` not on PATH: register the arm repo yourself with "
                         f"`orca repo add --path {repo}` before running it")
    p = subprocess.run([orca, "repo", "add", "--path", str(repo), "--json"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"orca repo add failed for {repo}: {(p.stderr or p.stdout).strip()[:300]}")


def setup(root: Path, new_src: Path, old_ref: str, arms=ARMS, spec_only: bool = False) -> None:
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
        register_with_orca(repo)
        prompt = build_prompt(chome / "skills" / "factory" / "scripts", spec_only)
        if spec_only:
            (a / "SPEC_ONLY").write_text("spec stage only; no builds, reviews or merges\n", encoding="utf-8")
        (a / "RUN.md").write_text(
            f"# arm `{arm}`\n\nrepo: `{repo}`\nclaude home: `{chome}`\n\n```\n"
            f"cd {repo} && CLAUDE_CONFIG_DIR={chome} claude --permission-mode bypassPermissions --effort medium "
            f"--output-format json -p {shlex.quote(prompt)} "
            f"> {a / 'coordinator.json'}\n```\n\n`coordinator.json` is Claude's own accounting for "
            f"the coordinator session (the same numbers `/cost` shows). Worker sessions are "
            f"summed from their transcripts. Run `/cost` yourself in an interactive session to "
            f"compare; this harness cannot run slash commands.\n", encoding="utf-8")
    print(f"ready: {" and ".join(f"{root}/{a}" for a in arms)} (see RUN.md in each). Nothing has run.")


# ---------------------------------------------------------------- collect
def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


TOKEN_KEYS = ("inp", "out", "cache_read", "cache_write_5m", "cache_write_1h",
              "cache_write_unknown", "cache_write_total", "inconsistent", "n")


def new_bucket() -> dict:
    return dict.fromkeys(TOKEN_KEYS, 0)


def bucket_add(b: dict, inp, out, read, total, w5, w1, n=1) -> None:
    """Raw tokens by type and cache-write TTL. `cache_write_unknown` is whatever the reported
    total leaves after the explicit 5m and 1h parts (a transcript without a TTL split is all
    unknown); a negative remainder is counted in `inconsistent` and priced from the explicit
    parts only. `cache_write_total` is for display, never for pricing."""
    inp, out, read, w5, w1 = (x or 0 for x in (inp, out, read, w5, w1))
    total = total if total is not None else w5 + w1
    unk = total - w5 - w1
    if unk < 0:
        b["inconsistent"] += 1
        unk = 0
    b["inp"] += inp
    b["out"] += out
    b["cache_read"] += read
    b["cache_write_5m"] += w5
    b["cache_write_1h"] += w1
    b["cache_write_unknown"] += unk
    b["cache_write_total"] += total
    b["n"] += n


def usage_of(path: Path) -> tuple[dict, str | None, str | None]:
    """{model: token bucket} of one transcript, plus first/last timestamp.
    A streamed message repeats its usage, so the last entry per (message id, requestId) wins."""
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
            last[(m["id"], o.get("requestId"))] = (m.get("model") or "?", m["usage"])
    out: dict = {}
    for model, u in last.values():
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        bucket_add(out.setdefault(model, new_bucket()), u.get("input_tokens"),
                   u.get("output_tokens"), u.get("cache_read_input_tokens"),
                   u.get("cache_creation_input_tokens"),
                   cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens"))
    return out, first_ts, last_ts


def model_usage_bucket(u: dict) -> dict:
    """One model's entry of a `claude -p --output-format json` `modelUsage`, same buckets."""
    b = new_bucket()
    bucket_add(b, u.get("inputTokens"), u.get("outputTokens"), u.get("cacheReadInputTokens"),
               u.get("cacheCreationInputTokens"), u.get("cacheCreationEphemeral5mInputTokens"),
               u.get("cacheCreationEphemeral1hInputTokens"), n=0)
    return b


def add_usage(total: dict, part: dict) -> None:
    for model, d in part.items():
        t = total.setdefault(model, new_bucket())
        for k, v in d.items():
            t[k] = t.get(k, 0) + v


def project_dir(path: Path, base: Path | None = None) -> Path:
    return (base or CLAUDE_PROJECTS) / re.sub(r"[^A-Za-z0-9]", "-", str(path))


def sessions(d: Path) -> list[Path]:
    """Transcripts of a project dir, including subagent transcripts nested below it."""
    return sorted(d.rglob("*.jsonl")) if d.is_dir() else []


def session_meta(path: Path) -> dict:
    """Facts about one transcript: what client label it carries, which CLI version, which models,
    and what kind of session it is.
      subagent: lives under a `subagents/` directory or its entries are `isSidechain`
      helper:   a top-level transcript with at most 2 model replies and no tool use (title and
                other one-shot helper calls)
      main:     any other top-level transcript, i.e. the worker session itself
    Only `main` sessions are worth diagnosing; a helper or subagent saying `sdk-cli` is not a
    finding about the worker."""
    eps, vers, models = set(), set(), set()
    replies, tool_use, sidechain = 0, False, False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("entrypoint"):
            eps.add(o["entrypoint"])
        if o.get("version"):
            vers.add(o["version"])
        sidechain = sidechain or o.get("isSidechain") is True
        m = o.get("message")
        if isinstance(m, dict) and isinstance(m.get("usage"), dict) and m.get("id"):
            replies += 1
            models.add(m.get("model") or "?")
        if isinstance(m, dict) and isinstance(m.get("content"), list) and any(
                isinstance(c, dict) and c.get("type") == "tool_use" for c in m["content"]):
            tool_use = True
    if "subagents" in path.parts or sidechain:
        kind = "subagent"
    elif replies <= 2 and not tool_use:
        kind = "helper"
    else:
        kind = "main"
    return {"session": path.stem, "kind": kind, "entrypoints": sorted(eps),
            "versions": sorted(vers), "models": sorted(models)}


def entrypoint_diagnosis(metas: list) -> dict:
    """Entrypoints of the main sessions only. Helpers and subagents are counted, never judged."""
    main = [m for m in metas if m["kind"] == "main"]
    eps: dict = {}
    for m in main:
        for e in m["entrypoints"] or ["(none)"]:
            eps[e] = eps.get(e, 0) + 1
    return {"main_sessions": len(main), "main_entrypoints": eps,
            "sdk_main_sessions": [m["session"] for m in main
                                  if any(e.startswith("sdk") for e in m["entrypoints"])],
            "subagents": sum(m["kind"] == "subagent" for m in metas),
            "helpers": [{"session": m["session"], "entrypoints": m["entrypoints"]}
                        for m in metas if m["kind"] == "helper"]}


def session_group(path: Path) -> Path:
    """The main transcript a session belongs to: a subagent transcript at
    `<id>/subagents/agent-*.jsonl` belongs to `<id>.jsonl`; any other transcript is its own group."""
    if "subagents" in path.parts:
        i = len(path.parts) - 1 - path.parts[::-1].index("subagents")
        return Path(*path.parts[:i]).with_suffix(".jsonl")
    return path


def attribute_groups(repo_sessions: list[Path], job_ids: list[str]) -> dict[Path, str]:
    """Assign each main-workspace worker (a main session with all its subagents) to the job whose
    `.factory/jobs/<id>` path its transcripts name most often. The brief a worker is dispatched with
    does not name its job, and a spec-stage worker may see other jobs' paths, so the dominant job
    wins; a tie or no mention leaves the group unassigned."""
    groups: dict[Path, list[Path]] = {}
    for s in repo_sessions:
        groups.setdefault(session_group(s), []).append(s)
    out = {}
    for g, members in groups.items():
        text = "".join(m.read_text(encoding="utf-8", errors="replace") for m in members)
        score = sorted(((text.count(f".factory/jobs/{j}") + text.count(f"factory/{j}"), j)
                        for j in job_ids), reverse=True)
        if score and score[0][0] and (len(score) == 1 or score[0][0] > score[1][0]):
            out[g] = score[0][1]
    return out


def spec_quality(jd: Path) -> dict:
    """Is the spec usable? Starts with `# `, has an acceptance-criteria section, and how many
    numbered criteria it lists; plus which of the three files exist."""
    sp = jd / "spec.md"
    text = sp.read_text(encoding="utf-8", errors="replace") if sp.is_file() else ""
    m = re.search(r"^## Acceptance criteria\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return {"spec": bool(text), "starts_with_heading": text.startswith("# "),
            "criteria_section": bool(m),
            "criteria": len(re.findall(r"^\s*\d+[.)]\s", m.group(1), re.M)) if m else 0,
            "context": (jd / "context.md").is_file(), "plan": (jd / "plan.md").is_file()}


def collect_arm(root: Path, arm: str) -> dict:
    a = root / arm
    repo = repo_of(root, arm)
    jobs_dir = repo / ".factory" / "jobs"
    jobs = {}
    repo_sessions = sessions(project_dir(repo))              # workers on the main workspace
    # Workers in a run's lean spec worktree (`factory-spec-<RID>`, see spec-workdir.md): its project
    # dir is named after the worktree path, which Orca chooses, so find it by the run id.
    for rf in sorted((repo / ".factory" / "runs").glob("*.json")) if (repo / ".factory" / "runs").is_dir() else []:
        rid = jload(rf, {}).get("id") or rf.stem
        for d in sorted(CLAUDE_PROJECTS.glob(f"*-factory-spec-{rid}")):
            repo_sessions += sessions(d)
    coord_sessions = sessions(project_dir(repo, a / "home" / "projects"))  # coordinator
    claimed: set[Path] = set()
    job_ids = [jload(p / "job.json", {}).get("id") for p in jobs_dir.glob("*") if (p / "job.json").is_file()]
    owner = attribute_groups(repo_sessions, [j for j in job_ids if j])
    for jd in sorted(p for p in jobs_dir.glob("*") if (p / "job.json").is_file()):
        j = jload(jd / "job.json", {})
        jid = j["id"]
        usage: dict = {}
        spec_usage: dict = {}           # spec-stage workers (main workspace), by session kind
        spec_by_kind: dict = {}
        spec_kinds: list = []
        by_kind: dict = {}
        metas: list = []
        spans = []

        def take(s, spec=False):
            u, t0, t1 = usage_of(s)
            if born and t0 and iso(t0) < born:   # a previous run that reused this path
                return False
            mt = session_meta(s)
            metas.append(mt)
            add_usage(usage, u)
            add_usage(by_kind.setdefault(mt["kind"], {}), u)
            spans.append((t0, t1))
            if spec:
                add_usage(spec_usage, u)
                spec_kinds.append(mt["kind"])
                add_usage(spec_by_kind.setdefault(mt["kind"], {}), u)
            return True
        # Worker sessions in the job's own worktree.
        wt = Path(j.get("worktree") or "")
        born = iso(j["created"]) if j.get("created") else None
        for s in sessions(project_dir(wt)) if str(wt) else []:
            take(s)
        # Spec-stage workers run in the repo's main workspace: whole groups assigned by job path.
        for s in repo_sessions:
            if s not in claimed and owner.get(session_group(s)) == jid and take(s, spec=True):
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
            "tokens_by_kind": by_kind,
            "spec_tokens": spec_usage, "spec_session_kinds": spec_kinds, "spec_tokens_by_kind": spec_by_kind,
            "spec_quality": spec_quality(jd),
            "sessions": metas,
            "diagnosis": entrypoint_diagnosis(metas),
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
               "models": {m: dict(model_usage_bucket(u), cost_usd=u.get("costUSD"))
                          for m, u in (cj.get("modelUsage") or {}).items()}}
    return {"jobs": jobs, "coordinator_tokens": coord, "unattributed_tokens": other,
            "coordinator_session": session,
            "runs": len(runs),
            "arm_wall_s": (max(times) - min(times)).total_seconds() if times else None}


PRICE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def load_prices(path=None) -> dict:
    """prices.json, refusing the old single `cache_write` rate: it hid which TTL it priced."""
    prices = jload(path or PRICES, {})
    for family, p in prices.items():
        if isinstance(p, dict) and "cache_write" in p:
            raise SystemExit(
                f"{path or PRICES}: `{family}` has the legacy single `cache_write` price. Replace it "
                f"with `cache_write_5m` (1.25x input) and `cache_write_1h` (2x input), or re-run "
                f"`bench.py calibrate`.")
    return prices


def _ttl_tokens(d: dict) -> tuple:
    """(5m, 1h, unknown) cache-write tokens; a legacy bucket with only `cache_write` is all unknown."""
    if "cache_write_total" not in d and "cache_write" in d:
        return 0, 0, d["cache_write"] or 0
    return d.get("cache_write_5m", 0), d.get("cache_write_1h", 0), d.get("cache_write_unknown", 0)


def cost(tokens: dict, prices: dict, assume_ttl: str | None = None) -> dict | None:
    """API-equivalent estimated USD (not what was billed, not subscription use).
    Returns {usd, usd_low, usd_high, unknown_ttl_tokens}. Cache-write tokens of unknown TTL are
    priced at both TTL rates, so the answer is a range; `usd` is set only when the range is a
    point (no unknown tokens, or `assume_ttl` of "5m"/"1h" says how to price them), else None.
    None overall when a model has no price."""
    if assume_ttl not in (None, "5m", "1h"):
        raise ValueError("assume_ttl must be '5m' or '1h'")
    low = high = 0.0
    unknown_total = 0
    for model, d in tokens.items():
        key = next((k for k in prices if k in model), None)
        p = prices.get(key) if key else None
        if not p or any(p.get(k) is None for k in PRICE_KEYS):
            return None
        w5, w1, unk = _ttl_tokens(d)
        base = (d["inp"] * p["input"] + d["out"] * p["output"]
                + d["cache_read"] * p["cache_read"] + w5 * p["cache_write_5m"]
                + w1 * p["cache_write_1h"]) / 1e6
        rates = {"5m": p["cache_write_5m"], "1h": p["cache_write_1h"]}
        if assume_ttl:
            lo = hi = rates[assume_ttl]
        else:
            lo, hi = min(rates.values()), max(rates.values())
        low += base + unk * lo / 1e6
        high += base + unk * hi / 1e6
        unknown_total += unk
    point = unknown_total == 0 or assume_ttl is not None
    return {"usd": low if point else None, "usd_low": low, "usd_high": high,
            "unknown_ttl_tokens": unknown_total}


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
# Nominal cache-write multipliers of the input price. They are no longer used to fit: calibrate
# fits the 5m and 1h write prices freely and checks the implied multipliers against these.
CACHE_5M_X, CACHE_1H_X = 1.25, 2.0
MULTIPLIER_TOLERANCE = 0.05


def _solve(a, b):
    """Solve a x = b by Gaussian elimination with partial pivoting; None when singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    scale = max(abs(x) for row in a for x in row) or 1.0
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12 * scale:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_prices(points: list[dict]) -> dict:
    """Least squares for USD per million tokens, all five prices free:
    cost = p_in*inp + p_out*out + p_read*read + p_5m*w5 + p_1h*w1 (tokens in millions).
    Needs points with 5m writes (FORCE_PROMPT_CACHING_5M=1 runs) and points with 1h writes.
    Returns the prices, the implied write multipliers, and the worst relative residual."""
    rows = [([p["inp"] / 1e6, p["out"] / 1e6, p["read"] / 1e6, p["w5"] / 1e6, p["w1"] / 1e6], p["cost"])
            for p in points]
    k = 5
    sol = len(rows) >= k and _solve([[sum(r[0][i] * r[0][j] for r in rows) for j in range(k)]
                                     for i in range(k)],
                                    [sum(r[0][i] * r[1] for r in rows) for i in range(k)])
    if not sol:
        raise ValueError("not enough variation in the calibration runs (need 5m and 1h writes, "
                         "cache reads, and at least 5 points)")
    p_in, p_out, p_read, p_5m, p_1h = sol
    resid = max(abs(sum(c * x for c, x in zip(sol, r[0])) - r[1]) / r[1] for r in rows)
    return {"input": round(p_in, 4), "output": round(p_out, 4), "cache_read": round(p_read, 4),
            "cache_write_5m": round(p_5m, 4), "cache_write_1h": round(p_1h, 4),
            "multiplier_5m": p_5m / p_in if p_in else None, "multiplier_1h": p_1h / p_in if p_in else None,
            "residual": resid, "runs": len(points)}


def point_from_model_usage(u: dict) -> dict:
    """One calibration point from a modelUsage entry; ValueError unless the TTL is explicit and
    consistent. No fallback: a missing field is never guessed (that silently priced unknown
    writes as 1h)."""
    f5, f1 = "cacheCreationEphemeral5mInputTokens", "cacheCreationEphemeral1hInputTokens"
    if f5 not in u or f1 not in u or "cacheCreationInputTokens" not in u:
        raise ValueError("modelUsage lacks the explicit 5m/1h cache-write fields")
    if u[f5] + u[f1] != u["cacheCreationInputTokens"]:
        raise ValueError(f"5m ({u[f5]}) + 1h ({u[f1]}) != cache write total "
                         f"({u['cacheCreationInputTokens']})")
    return {"inp": u.get("inputTokens", 0), "out": u.get("outputTokens", 0),
            "read": u.get("cacheReadInputTokens", 0), "w5": u[f5], "w1": u[f1],
            "cost": u["costUSD"]}


def calibrate(models, claude="claude", out=PRICES, max_residual=0.03) -> dict:
    """Run tiny `claude -p` calls per model, twice per prompt (default caching, then with
    FORCE_PROMPT_CACHING_5M=1), read Claude's own per-model costUSD and token counts, fit all five
    prices, and write prices.json only for models whose residual and write multipliers hold.
    Uses real tokens (a few dollars at most, mostly Opus)."""
    import tempfile
    points: dict = {}
    refused: dict = {}
    with tempfile.TemporaryDirectory() as cwd:
        for model in models:
            for prompt in CAL_PROMPTS:
                for forced in (False, True):
                    env = dict(os.environ)
                    env.pop("FORCE_PROMPT_CACHING_5M", None)
                    if forced:
                        env["FORCE_PROMPT_CACHING_5M"] = "1"
                    p = subprocess.run([claude, "-p", prompt, "--model", model, "--output-format",
                                        "json", "--allowedTools", "Bash(echo:*)"],
                                       cwd=cwd, capture_output=True, text=True, timeout=600, env=env)
                    try:
                        usage = json.loads(p.stdout).get("modelUsage") or {}
                    except ValueError:
                        raise SystemExit(f"calibrate: {model}: no JSON from claude "
                                         f"({p.stderr.strip()[:200]})")
                    for m, u in usage.items():
                        if not u.get("costUSD"):
                            continue
                        try:
                            points.setdefault(m, []).append(point_from_model_usage(u))
                        except ValueError as e:
                            refused.setdefault(m, []).append(str(e))
    result, bad = {}, []
    for m, why in refused.items():
        bad.append(f"{m}: refused {len(why)} data point(s): {why[0]}")
    for m, pts in points.items():
        if m in refused:
            continue
        try:
            fit = fit_prices(pts)
        except ValueError as e:
            bad.append(f"{m}: {e}")
            continue
        if fit["residual"] > max_residual:
            bad.append(f"{m}: residual {fit['residual']:.1%} > {max_residual:.0%}; set prices by hand")
            continue
        off = [(n, x) for n, x, want in (("5m", fit["multiplier_5m"], CACHE_5M_X),
                                         ("1h", fit["multiplier_1h"], CACHE_1H_X))
               if x is None or abs(x / want - 1) > MULTIPLIER_TOLERANCE]
        if off:
            bad.append(f"{m}: write multiplier(s) off nominal: "
                       + ", ".join(f"{n} {x:.3f}" for n, x in off) + "; set prices by hand")
            continue
        result[m] = fit
    if bad:
        print("calibrate: not written for:\n  " + "\n  ".join(bad), file=sys.stderr)
    # Merge into the existing file: calibrating one model must not erase the others.
    table = {k: v for k, v in jload(out, {}).items()
             if isinstance(v, dict) and "cache_write" not in v}
    table = {"_note": "USD per million tokens, fitted by `bench.py calibrate` from Claude's own "
                      "costUSD: input, output, cache read, cache_write_5m (FORCE_PROMPT_CACHING_5M=1 "
                      "runs) and cache_write_1h (default runs) are all fitted; _fit records the "
                      "multipliers and the worst relative residual. A family whose _fit has no "
                      "multiplier_5m was not fitted for 5m: its cache_write_5m is ASSUMED at 1.25x "
                      "input.",
             "_calibrated": datetime.now().isoformat(timespec="seconds"), **table}
    for m, fit in result.items():
        family = next((k for k in ("opus", "sonnet", "haiku", "fable") if k in m), m)
        table[family] = {k: fit[k] for k in PRICE_KEYS}
        table[family]["_fit"] = {"model": m, "runs": fit["runs"], "residual": round(fit["residual"], 5),
                                 "multiplier_5m": round(fit["multiplier_5m"], 4),
                                 "multiplier_1h": round(fit["multiplier_1h"], 4)}
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


def suite_and_build(chk: Path):
    suite = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                           cwd=chk, capture_output=True, text=True,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    build = subprocess.run([sys.executable, "-m", "compileall", "-q", "src"], cwd=chk,
                           capture_output=True, text=True)
    return suite, build


def grade_runs(root: Path, arm: str) -> dict:
    """Grade the integrated tip of every run branch (`factory-run/<RID>`): the repo suite, and the
    accept test of every task (not only the merged ones). A job's own `factory/<id>` branch can
    pass while the tip fails, because jobs interact once merged. Result per run id:
    {branch, tip, merged: [job ids], suite_ok, build_ok, accept: {task: result}}."""
    repo = repo_of(root, arm)
    out = {}
    for rf in sorted((repo / ".factory" / "runs").glob("*.json")):
        run = jload(rf, {})
        branch = run.get("branch")
        tip = branch and git(repo, "rev-parse", "--verify", "-q", branch, check=False)
        g = {"branch": branch, "tip": tip or None,
             "merged": [m["job"] for m in run.get("merged") or []]}
        if not tip:
            out[run.get("id") or rf.stem] = g
            continue
        chk = root / arm / "checkouts" / f"run-{rf.stem}"
        shutil.rmtree(chk, ignore_errors=True)
        git(repo, "worktree", "prune")
        git(repo, "worktree", "add", "--detach", str(chk), tip)
        suite, build = suite_and_build(chk)
        g.update({"suite_ok": suite.returncode == 0, "build_ok": build.returncode == 0,
                  "accept": {t: run_accept(t, chk) for t in TASKS}})
        git(repo, "worktree", "remove", "--force", str(chk), check=False)
        out[run.get("id") or rf.stem] = g
    return out


def delivered_jobs(metrics_arm: dict, grades_arm: dict, run_grades: dict) -> list:
    """Job ids that are merged into a run branch AND pass their accept test on that run's tip
    AND leave the tip's repo suite green. Anything else (failed, blocked, held, merged but broken
    on the tip, run branch not graded) is not delivered."""
    done = []
    for jid in (metrics_arm.get("jobs") or {}):
        task = (grades_arm.get(jid) or {}).get("task")
        for rg in run_grades.values():
            if (jid in rg.get("merged", []) and task and rg.get("suite_ok")
                    and (rg.get("accept", {}).get(task) or {}).get("ok")):
                done.append(jid)
                break
    return done


def arm_cost(metrics_arm: dict, prices: dict, assume_ttl: str | None = None) -> dict | None:
    """Everything the arm spent on the Claude side, API-equivalent: the coordinator session,
    every job's workers (failed, blocked, held, retries, rework and integration repair all land in
    the job's sessions) and worker sessions that matched no job. {usd_low, usd_high, parts}; None
    when a model has no price. Builders outside Claude transcripts (Devin) are not metered."""
    ses = metrics_arm.get("coordinator_session") or {}
    if ses.get("cost_usd") is not None:                 # Claude's own number for the coordinator
        coord = {"usd_low": ses["cost_usd"], "usd_high": ses["cost_usd"]}
    else:
        coord = cost(ses.get("models") or {}, prices, assume_ttl) or {"usd_low": 0.0, "usd_high": 0.0}
    parts = {"coordinator": coord}
    jobs = [cost(j["tokens"], prices, assume_ttl) for j in (metrics_arm.get("jobs") or {}).values()]
    other = cost(metrics_arm.get("unattributed_tokens") or {}, prices, assume_ttl)
    if any(c is None for c in jobs) or other is None:
        return None
    parts["jobs"] = {"usd_low": sum(c["usd_low"] for c in jobs), "usd_high": sum(c["usd_high"] for c in jobs)}
    parts["unattributed"] = other
    return {"usd_low": sum(v["usd_low"] for v in parts.values()),
            "usd_high": sum(v["usd_high"] for v in parts.values()), "parts": parts}


def cost_per_delivered(total: dict | None, delivered: int) -> dict | None:
    """Arm cost divided by delivered jobs; None when nothing was delivered or the cost is unknown."""
    if not total or delivered <= 0:
        return None
    return {"usd_low": total["usd_low"] / delivered, "usd_high": total["usd_high"] / delivered}


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
        suite, build = suite_and_build(chk)
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
def fmt_usd(lo, hi) -> str:
    """API-equivalent estimate; a range when cache-write TTLs are unknown."""
    return f"${lo:.2f}" if abs(hi - lo) < 0.005 else f"${lo:.2f}-${hi:.2f}"


def fmt_cost(c) -> str:
    return "n/a" if c is None else fmt_usd(c["usd_low"], c["usd_high"])


def money(t: dict) -> str:
    return fmt_usd(t["lo"], t["hi"]) if t["cost_ok"] else "n/a (fill prices.json)"


def fmt_s(s):
    return "-" if s is None else f"{int(s // 60)}m{int(s % 60):02d}s"


def _ttl_tokens_total(d: dict) -> int:
    return d["cache_write_total"] if "cache_write_total" in d else d.get("cache_write", 0)


def tok_total(tokens: dict) -> int:
    return sum(d["inp"] + d["out"] + _ttl_tokens_total(d) for d in tokens.values())


def spec_stage_summary(ma: dict, prices: dict, assume_ttl: str | None = None) -> dict:
    """The spec stage of one arm: how many specs are usable, mean numbered criteria, what the
    spec workers cost (API-equivalent; a range when a TTL is unknown), and the cache-write
    tokens of each spec worker's main session (the un-lean prompt prefix shows up here)."""
    jobs = (ma.get("jobs") or {}).values()
    q = [j.get("spec_quality") or {} for j in jobs]
    usable = [x for x in q if x.get("starts_with_heading") and x.get("criteria_section")]
    tot: dict = {}
    for j in jobs:
        add_usage(tot, j.get("spec_tokens") or {})
    main_n = sum(k == "main" for j in jobs for k in j.get("spec_session_kinds") or [])
    main_w = 0
    for j in jobs:
        for u in ((j.get("spec_tokens_by_kind") or {}).get("main") or {}).values():
            main_w += u.get("cache_write_total", 0)
    return {"jobs": len(q), "usable": len(usable),
            "criteria_mean": (sum(x["criteria"] for x in usable) / len(usable)) if usable else None,
            "cost": cost(tot, prices, assume_ttl) if tot else None,
            "main_sessions": main_n, "main_cache_write_per_session": main_w / main_n if main_n else None}


def report(root: Path, assume_ttl: str | None = None) -> None:
    metrics, grades = jload(root / "metrics.json", {}), jload(root / "grades.json", {})
    run_grades = jload(root / "run_grades.json", {})
    prices = load_prices()
    judge_key = jload(root / "judge" / "KEY.json", {})
    verdicts: dict = {}
    for task, k in judge_key.items():
        v = jload(root / "judge" / f"{task}.json")
        if v:
            verdicts[task] = {k[x]: sum(v.get(x, {}).values()) for x in ("A", "B")}
            w = v.get("winner")
            verdicts[task]["winner"] = k.get(w) if w in k else "tie"
    L = [f"# Factory benchmark: {' vs '.join(ARMS)}", "",
         "Costs are API-equivalent estimates from transcript tokens x `prices.json`; they are not "
         "billed amounts and not subscription use. Cache-write tokens with no TTL split are priced "
         "as a range (5m rate to 1h rate)" + (f"; this report assumes **{assume_ttl}** for them "
         f"(`--assume-ttl {assume_ttl}`)." if assume_ttl else "."), ""]
    L += ["## Per task", "", "| task | arm | engine | accept | suite | rounds | stage | wall | "
          "tokens (in+out+cache write) | cost | judge |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    tot = {arm: dict(wall=0.0, tok=0, lo=0.0, hi=0.0, cost_ok=True, accept=0, w=0, n=0, wt=0)
           for arm in ARMS}
    for task in TASKS:
        for arm in ARMS:
            hit = [(jid, g) for jid, g in grades.get(arm, {}).items() if g.get("task") == task]
            if not hit:
                L.append(f"| {task} | {arm} | - | not run | | | | | | | |")
                continue
            jid, g = hit[0]
            m = metrics[arm]["jobs"][jid]
            c = cost(m["tokens"], prices, assume_ttl)
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
                t["lo"] += c["usd_low"]
                t["hi"] += c["usd_high"]
            jv = verdicts.get(task, {})
            L.append(f"| {task} | {arm} | {m['builder_engine']} | "
                     f"{'PASS' if acc.get('ok') else 'FAIL ' + str(acc.get('failed'))} | "
                     f"{'ok' if g.get('suite_ok') else 'FAIL'} | {m['round']} | {m['stage']} | "
                     f"{fmt_s(m['wall_s'])} | {tok_total(m['tokens']):,} | "
                     f"{fmt_cost(c)} | "
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
        mine = cost(ses.get("models", {}), prices, assume_ttl)
        wc = [cost(j["tokens"], prices, assume_ttl) for j in metrics.get(arm, {}).get("jobs", {}).values()]
        ok = all(c is not None for c in wc)
        wlo, whi = sum(c["usd_low"] for c in wc if c), sum(c["usd_high"] for c in wc if c)
        real = ses.get("cost_usd")
        total = fmt_usd((real or 0) + wlo, (real or 0) + whi) if ok and real is not None else "n/a"
        L.append(f"| {arm} | {'n/a' if real is None else f'${real:.2f}'} | {fmt_cost(mine)} | "
                 f"{fmt_usd(wlo, whi) if ok else 'n/a'} | {total} | "
                 f"{'$0, not metered' if arm == 'new' else '-'} |")
    L += ["", "The coordinator is a main session, so its numbers say nothing about the worker "
          "personas, which run as subagents with their own cache TTL. A coordinator match does "
          "not validate the worker cost.", ""]
    L += ["", "## Delivery on the run branch", "",
          "Graded on each run's integrated tip `factory-run/<RID>` (repo suite plus every task's "
          "accept test), not on the job's own branch. A job is **delivered** when it is merged "
          "into a run branch, passes its accept test on that tip, and the tip's suite is green. "
          "Cost per delivered job = all Claude-side arm cost (coordinator, every job's workers "
          "including failed, blocked, held, retry, rework and repair sessions, and unmatched "
          "worker sessions) / delivered jobs. Builders not metered by Claude (Devin) are not in it.", "",
          "| arm | jobs | merged | delivered | run suite | arm cost (API-equiv.) | cost per delivered job |",
          "|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        ma, rg = metrics.get(arm, {}), run_grades.get(arm, {})
        dl = delivered_jobs(ma, grades.get(arm, {}), rg)
        total = arm_cost(ma, prices, assume_ttl)
        per = cost_per_delivered(total, len(dl))
        merged = sum(len(r.get("merged", [])) for r in rg.values())
        suite = "no run branch" if not rg else ", ".join(
            f"{'ok' if r.get('suite_ok') else 'FAIL'}" for r in rg.values())
        L.append(f"| {arm} | {len(ma.get('jobs', {}))} | {merged} | {len(dl)} | {suite} | "
                 f"{fmt_cost(total)} | {fmt_cost(per) if per else 'n/a (nothing delivered)'} |")
    reg = [(arm, jid, g.get("task")) for arm in ARMS for jid, g in grades.get(arm, {}).items()
           if g.get("accept", {}).get("ok") and g.get("task")
           and any(jid in r.get("merged", []) and not (r.get("accept", {}).get(g["task"]) or {}).get("ok")
                   for r in run_grades.get(arm, {}).values())]
    L += ["", "Merged jobs that pass on their own branch but fail on the run tip: "
          + (", ".join(f"{a}/{j} ({t})" for a, j, t in reg) if reg else "none") + ".", ""]
    L += ["", "## Spec stage", "",
          "Spec workers run on the repo's main workspace (scout, spec writer, planner as subagents). "
          "Cost is API-equivalent; `main cache write / session` is the prompt prefix a spec "
          "worker's main session writes before it does anything. A spec is usable when it starts "
          "with `# ` and has an `## Acceptance criteria` section.", "",
          "| arm | spec-only run | jobs | usable specs | mean criteria | main sessions | main cache write / session | spec-stage cost | cost per job |",
          "|---|---|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        ss = spec_stage_summary(metrics.get(arm, {}), prices, assume_ttl)
        per = None
        if ss["cost"] and ss["jobs"]:
            per = {"usd_low": ss["cost"]["usd_low"] / ss["jobs"], "usd_high": ss["cost"]["usd_high"] / ss["jobs"]}
        L.append(f"| {arm} | {'yes' if (root / arm / 'SPEC_ONLY').exists() else 'no'} | {ss['jobs']} | "
                 f"{ss['usable']} | {'-' if ss['criteria_mean'] is None else f'{ss['criteria_mean']:.1f}'} | "
                 f"{ss['main_sessions']} | "
                 f"{'-' if ss['main_cache_write_per_session'] is None else f'{ss['main_cache_write_per_session']:,.0f}'} | "
                 f"{fmt_cost(ss['cost']) if ss['cost'] else 'n/a'} | {fmt_cost(per) if per else 'n/a'} |")
    L += ["", "## Cache-write TTL by session kind", "",
          "Raw tokens, summed over the arm's jobs; the coordinator is a separate main session. "
          "`unknown` is cache-write tokens with no 5m/1h split.", "",
          "| arm | session kind | sessions | cache write 5m | cache write 1h | unknown TTL | cache read |",
          "|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        jobs = metrics.get(arm, {}).get("jobs", {})
        rows = {}
        for j in jobs.values():
            for kind, toks in (j.get("tokens_by_kind") or {}).items():
                r = rows.setdefault(kind, [0, 0, 0, 0, 0])
                r[0] += sum(1 for x in j.get("sessions", []) if x["kind"] == kind)
                for d in toks.values():
                    r[1] += d["cache_write_5m"]
                    r[2] += d["cache_write_1h"]
                    r[3] += d["cache_write_unknown"]
                    r[4] += d["cache_read"]
        for kind in ("main", "subagent", "helper"):
            if kind in rows:
                r = rows[kind]
                L.append(f"| {arm} | worker {kind} | {r[0]} | {r[1]:,} | {r[2]:,} | {r[3]:,} | "
                         f"{r[4]:,} |")
        co = (metrics.get(arm, {}).get("coordinator_session") or {}).get("models", {})
        if co:
            L.append(f"| {arm} | coordinator (main) | 1 | "
                     f"{sum(d['cache_write_5m'] for d in co.values()):,} | "
                     f"{sum(d['cache_write_1h'] for d in co.values()):,} | "
                     f"{sum(d['cache_write_unknown'] for d in co.values()):,} | "
                     f"{sum(d['cache_read'] for d in co.values()):,} |")
    L += ["", "## Worker sessions and entrypoints", "",
          "Only main sessions are diagnosed. Subagents and helper/title sessions are counted and "
          "listed apart, and a helper's `sdk-*` label is not a finding about the worker.", "",
          "| job | arm | main sessions | main entrypoints | main sessions on sdk-* | subagents | "
          "helper sessions (entrypoints) |", "|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        for jid, j in metrics.get(arm, {}).get("jobs", {}).items():
            dg = j.get("diagnosis")
            if not dg:
                continue
            hl = ", ".join(f"{'/'.join(h['entrypoints']) or '(none)'}" for h in dg["helpers"]) or "-"
            L.append(f"| {jid} | {arm} | {dg['main_sessions']} | "
                     f"{', '.join(f'{k} x{v}' for k, v in dg['main_entrypoints'].items()) or '-'} | "
                     f"{', '.join(dg['sdk_main_sessions']) or '-'} | {dg['subagents']} | {hl} |")
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
        if name == "report":
            s.add_argument("--assume-ttl", choices=("5m", "1h"), default=None,
                           help="price cache writes of unknown TTL at this rate (default: a range)")
        if name == "setup":
            s.add_argument("--new-src", default=str(ROOT))
            s.add_argument("--old-ref", default="main")
            s.add_argument("--arms", default=",".join(ARMS), help="rebuild only these arms")
            s.add_argument("--spec-only", action="store_true",
                           help="coordinator stops every job after its spec stage (no builds)")
    a = ap.parse_args(argv)
    if a.cmd == "calibrate":
        t = calibrate([m for m in a.models.split(",") if m], a.claude)
        print(json.dumps(t, indent=2))
        return 0 if any(k in t for k in ("opus", "sonnet", "haiku", "fable")) else 1
    root = Path(a.root).expanduser().resolve()
    if a.cmd == "setup":
        setup(root, Path(a.new_src), a.old_ref, tuple(x for x in a.arms.split(',') if x in ARMS),
              spec_only=a.spec_only)
    elif a.cmd == "collect":
        (root / "metrics.json").write_text(json.dumps({arm: collect_arm(root, arm) for arm in ARMS},
                                                      indent=1), encoding="utf-8")
    elif a.cmd == "grade":
        (root / "grades.json").write_text(json.dumps({arm: grade_arm(root, arm) for arm in ARMS},
                                                     indent=1), encoding="utf-8")
        (root / "run_grades.json").write_text(json.dumps({arm: grade_runs(root, arm) for arm in ARMS},
                                                         indent=1), encoding="utf-8")
    elif a.cmd == "pack":
        pack(root)
    else:
        report(root, a.assume_ttl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
