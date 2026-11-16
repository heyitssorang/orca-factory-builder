#!/usr/bin/env python3
"""Review parsing, verification routing and the draft/ready decision."""
import argparse
import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

LENSES = ("security", "code", "ux", "ui")
# Broad on purpose: a false "applicable" costs a dev-server run, a false "not applicable"
# skips the UX/UI lenses on a real user-facing change.
DEFAULT_UI_GLOBS = ["*.html", "*.css", "*.scss", "*.sass", "*.less", "*.jsx", "*.tsx", "*.js",
                    "*.ts", "*.mjs", "*.cjs", "*.vue", "*.svelte", "*.cshtml", "*.razor",
                    "*.erb", "*.hbs", "*.ejs", "*.svg",
                    "templates/**", "public/**", "src/**/components/**"]
_SEP = r"\s*[—–-]\s*"


@dataclass
class Finding:
    n: int
    location: str
    text: str
    check_cmd: str | None = None
    exit_code: int | None = None


@dataclass
class Review:
    verdict: str
    reason: str = ""
    findings: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    unparsed: list = field(default_factory=list)  # unrecognised lines in the checks section
    unparsed_findings: list = field(default_factory=list)  # stray lines in ## Findings


@dataclass
class RoundData:
    reviews: dict
    verifies: dict
    risk: str
    build_status: str
    criteria: list
    build_unparsed: list = field(default_factory=list)  # unrecognised lines in ## Criteria
    spec_criteria: list = field(default_factory=list)  # numbers under ## Acceptance criteria


def _lines(text: str) -> list[str]:
    """Strip BOM, normalise newlines, drop leading blank lines."""
    text = text.replace("﻿", "", 1) if text.startswith("﻿") else text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def _sections(lines: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for ln in lines:
        m = re.match(r"##\s+(.*?)\s*$", ln)
        if m:
            cur = m.group(1).lower()
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(ln)
    return out


def _sec(sections: dict[str, list[str]], prefix: str) -> list[str] | None:
    """Body of every section whose heading starts with prefix (None if absent)."""
    keys = [k for k in sections if k.startswith(prefix)]
    if not keys:
        return None
    return [ln for k in keys for ln in sections[k]]


def _parse_findings(body: list[str]) -> tuple[list[Finding], list[str]]:
    items: list[list] = []
    stray: list[str] = []
    for ln in body:
        m = re.match(r"\s*(?:[-*]\s*)?\**(\d+)[.)]\**\s+(.*)$", ln)
        if m:
            items.append([int(m.group(1)), m.group(2).strip()])
        elif ln.strip():
            if items and ln[0].isspace():
                items[-1][1] += "\n" + ln.strip()
            elif not re.fullmatch(r"none\.?", ln.strip(), re.I):
                stray.append(ln.strip())
    out = []
    for n, rest in items:
        parts = re.split(r"\s+[—–]\s+|\s+-\s+", rest, maxsplit=1)
        loc, text = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")
        cm = re.search(r"^\s*Check:\s*`([^`]+)`\s+exited\s+(-?\d+)", rest, re.M)
        out.append(Finding(n, loc.strip(), text.strip(),
                           cm.group(1) if cm else None, int(cm.group(2)) if cm else None))
    return out, stray


def parse_review(text: str | None) -> Review:
    if text is None:
        return Review("MISSING")
    lines = _lines(text)
    if not lines:
        return Review("MALFORMED")
    m = re.match(r"VERDICT:\s*(PASS|CHANGES|NOT RUN)(.*)$", lines[0].strip())
    if not m or (m.group(2).strip() and not re.match(r"\s*[—–-]", m.group(2))):
        return Review("MALFORMED")
    reason = re.sub(r"^\s*[—–-]\s*", "", m.group(2)).strip()
    sec = _sections(lines[1:])
    checks, unparsed = {}, []
    for ln in _sec(sec, "check") or []:
        cm = re.match(r"\s*[-*]\s*`([^`]+)`" + _SEP + r"(pass|fail|not run)\b.*$", ln, re.I)
        if cm:
            checks[cm.group(1)] = cm.group(2).lower()
        elif ln.strip():
            unparsed.append(ln.strip())
    findings, stray = _parse_findings(_sec(sec, "finding") or [])
    return Review(m.group(1), reason, findings, checks, unparsed, stray)


def parse_build_full(text: str | None) -> tuple[str, list, list[str], bool]:
    """(status, criteria, unparsed criteria lines, has ## Criteria section)."""
    if text is None:
        return "MISSING", [], [], False
    lines = _lines(text)
    m = re.match(r"STATUS:\s*(READY|BLOCKED)\s*$", lines[0].strip()) if lines else None
    if not m:
        return "MALFORMED", [], [], False
    crit, unparsed = [], []
    body = _sec(_sections(lines[1:]), "criteri")
    for ln in body or []:
        cm = re.match(r"\s*(\d+)\.\s*(not met|met)\b\s*(?:[—–-]\s*)?(.*)$", ln, re.I)
        if cm:
            crit.append((int(cm.group(1)), cm.group(2).lower() == "met", cm.group(3).strip()))
        elif ln.strip():
            unparsed.append(ln.strip())
    return m.group(1), crit, unparsed, body is not None


def parse_build(text: str | None) -> tuple[str, list[tuple[int, bool, str]]]:
    status, crit, _, _ = parse_build_full(text)
    return status, crit


def parse_spec_criteria(text: str | None) -> list[int]:
    """Numbers of the items under spec.md's `## Acceptance criteria` (column-0 items only;
    indented lines are continuations or sub-steps)."""
    if text is None:
        return []
    body = _sec(_sections(_lines(text)), "acceptance criteri") or []
    nums = []
    for ln in body:
        m = re.match(r"(?:[-*]\s*)?\**(\d+)[.)]\**(?:\s|$)", ln)
        if m and int(m.group(1)) not in nums:
            nums.append(int(m.group(1)))
    return nums


def _verify_word(s: str) -> str:
    s = s.strip().upper()
    for word in ("CONFIRMED", "FALSE POSITIVE", "UNVERIFIABLE"):
        if re.match(re.escape(word) + r"(?![A-Z])", s):
            return word
    return "UNVERIFIABLE"


def parse_verify(text: str | None) -> str:
    if text is None:
        return "UNVERIFIABLE"
    lines = _lines(text)
    if not lines:
        return "UNVERIFIABLE"
    m = re.match(r"VERIFY:\s*(.*)$", lines[0].strip())
    if m:
        return _verify_word(m.group(1))
    for ln in lines:
        m = re.match(r"verdict:\s*(.*)$", ln.strip(), re.I)
        if m:
            return _verify_word(m.group(1))
    return "UNVERIFIABLE"


def route(lens: str, f: Finding) -> str:
    if lens == "security":
        return "p2-security"
    if lens == "code":
        return "rerun" if f.check_cmd else "p2"
    return "browser"


def ui_applicable(paths: list[str], globs: list[str]) -> bool:
    pats = list(globs) + [g.replace("/**/", "/") for g in globs if "/**/" in g]
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatchcase(p, g) or fnmatch.fnmatchcase(base, g) for g in pats):
            return True
    return False


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def load_round(job_dir: Path, n: int) -> RoundData:
    rdir = job_dir / f"round-{n}"
    reviews = {lens: parse_review(_read(rdir / f"review-{lens}.md")) for lens in LENSES}
    verifies = {}
    for p in rdir.glob("verify-*-*.md"):
        m = re.fullmatch(r"verify-([a-z]+)-(\d+)\.md", p.name)
        if m and m.group(1) in LENSES:
            verifies[(m.group(1), int(m.group(2)))] = parse_verify(_read(p))
    status, criteria, unparsed, _ = parse_build_full(_read(job_dir / "build.md"))
    return RoundData(reviews, verifies, _read(rdir / "risk.md") or "", status, criteria,
                     unparsed, parse_spec_criteria(_read(job_dir / "spec.md")))


def draft_reasons(rd: RoundData, overlaps_default: list[str]) -> list[str]:
    reasons = []
    if rd.build_status != "READY":
        reasons.append(f"build status {rd.build_status}")
    for n, met, ev in rd.criteria:
        if not met:
            reasons.append(f"criterion {n} not met: {ev}".rstrip(": "))
    for ln in rd.build_unparsed:
        reasons.append(f"build unparsed criterion: {ln[:80]}")
    if rd.build_status == "READY" and not rd.criteria:
        reasons.append("build has no criteria")
    reported = {n for n, _, _ in rd.criteria}
    for n in rd.spec_criteria:
        if n not in reported:
            reasons.append(f"criterion {n} not reported")
    for lens in LENSES:
        r = rd.reviews.get(lens) or Review("MISSING")
        if r.verdict in ("MISSING", "MALFORMED"):
            reasons.append(f"{lens} review {r.verdict.lower()}")
        elif r.verdict == "NOT RUN":
            reasons.append(f"{lens} review not run" + (f": {r.reason}" if r.reason else ""))
        elif r.verdict == "CHANGES":
            if not r.findings:
                reasons.append(f"{lens} CHANGES without findings")
            seen = set()
            for f in r.findings:
                if f.n in seen:
                    reasons.append(f"{lens} duplicate finding {f.n}")
                seen.add(f.n)
                v = rd.verifies.get((lens, f.n), "UNVERIFIABLE")
                if v in ("CONFIRMED", "UNVERIFIABLE"):
                    reasons.append(f"{lens} finding {f.n} {v}")
        for ln in r.unparsed_findings:
            reasons.append(f"{lens} unparsed findings line: {ln[:80]}")
        if r.verdict == "PASS" and r.findings:
            reasons.append(f"{lens} PASS with findings")
        for ln in r.unparsed:
            reasons.append(f"{lens} unparsed check line: {ln[:80]}")
        if r.verdict == "PASS":
            for cmd, state in r.checks.items():
                if state == "fail":
                    reasons.append(f"{lens} PASS with failing check {cmd}")
        if lens == "code":
            if r.verdict in ("PASS", "CHANGES") and not r.checks:
                reasons.append("code review lists no checks")
            for cmd, state in r.checks.items():
                if state == "not run":
                    reasons.append(f"code check not run: {cmd}")
    if overlaps_default:
        reasons.append("overlaps with default branch: " + ", ".join(overlaps_default))
    return reasons


def _unquote(p: str) -> str:
    p = p.strip()
    if len(p) >= 2 and p[0] == p[-1] == '"':
        try:
            return (p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
                    .encode("latin-1").decode("utf-8", "replace"))
        except (UnicodeError, ValueError):
            return p[1:-1]
    return p


def _strip_prefix(p: str) -> str:
    return p[2:] if p[:2] in ("a/", "b/") else p


def _diff_paths(text: str) -> list[str]:
    paths = []
    for m in re.finditer(r"^diff --git (.*)$", text, re.M):
        rest = m.group(1)
        q = re.fullmatch(r'("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)', rest)
        if q and (q.group(1).startswith('"') or q.group(2).startswith('"')):
            paths += [_strip_prefix(_unquote(g)) for g in q.groups()]
        else:
            u = re.fullmatch(r"a/(.*) b/(.*)", rest)
            if u:
                paths += list(u.groups())
    for m in re.finditer(r"^(?:\+\+\+|---) (.*)$", text, re.M):
        raw = m.group(1).split("\t", 1)[0]
        p = _strip_prefix(_unquote(raw))
        if p and p != "/dev/null" and raw.strip() != "/dev/null":
            paths.append(p)
    return paths


RERUN_BAD_CHARS = set(";|&$`<>\n\r")
RERUN_BAD_PROGRAMS = {"git", "az", "curl", "wget", "ssh", "scp", "rm", "sudo", "doas", "su",
                      "runuser", "busybox", "sh", "bash", "zsh", "dash", "ksh", "mksh", "fish",
                      "csh", "tcsh", "pwsh", "powershell", "env", "xargs", "eval"}
# Wrappers that run their argument as the real program: skipped before argv[0] is judged.
# Value: options of that wrapper that take a separate argument.
RERUN_WRAPPERS = {
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "nohup": set(), "setsid": set(), "command": set(), "exec": set(),
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
}
# Interpreters and the short option letters / long options that take inline code.
RERUN_INTERPRETERS = (
    (re.compile(r"python[0-9.]*"), "c", {"--command"}),
    (re.compile(r"node(js)?"), "ep", {"--eval", "--print"}),
    (re.compile(r"perl[0-9.]*"), "eE", set()),
    (re.compile(r"ruby[0-9.]*"), "e", {"--eval"}),
)
_SECRET_ENV = re.compile(r"TOKEN|(?:^|_)PAT(?:_|$)", re.I)
_SECRET_NAMES = {"AZURE_DEVOPS_EXT_PAT", "GIT_ASKPASS"}


def scrubbed_env(env: dict | None = None) -> dict:
    """Environment without ADO PAT, askpass and *TOKEN* / *_PAT* variables (PATH is kept)."""
    env = dict(os.environ if env is None else env)
    return {k: v for k, v in env.items()
            if k.upper() not in _SECRET_NAMES and not _SECRET_ENV.search(k)}


def exec_tmpdir() -> Path:
    """Parent of the factory's temp dirs: $FACTORY_TMPDIR, else $XDG_CACHE_HOME/factory-tmp,
    else ~/.cache/factory-tmp. Never /tmp, which is often mounted noexec (test servers that
    unpack a binary there, git hooks)."""
    d = os.environ.get("FACTORY_TMPDIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "factory-tmp")
    p = Path(d)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def _program(argv: list[str]) -> tuple[str, list[str]] | None:
    """(basename of the real program, its arguments) after skipping wrapper tokens."""
    i = 0
    while i < len(argv):
        name = os.path.basename(argv[i]).lower()
        if name not in RERUN_WRAPPERS:
            return name, argv[i + 1:]
        takes_arg = RERUN_WRAPPERS[name]
        i += 1
        while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
            opt = argv[i]
            i += 1
            if opt in takes_arg:
                i += 1
        if i < len(argv) and argv[i] == "--":
            i += 1
        if name == "timeout":
            i += 1  # the duration
    return None


def rerun_refusal(cmd: str) -> str | None:
    """Why a reviewer's check command may not be re-run, or None when it may."""
    if any(c in RERUN_BAD_CHARS for c in cmd):
        return "forbidden character"
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return "unparseable"
    if not argv:
        return "empty"
    if any(set(t) & RERUN_BAD_CHARS for t in argv):
        return "forbidden character"
    if any(t.lower() == "push" for t in argv):
        return "push not allowed"
    prog = _program(argv)
    if prog is None:
        return "wrapper without a program"
    name, args = prog
    if name in RERUN_BAD_PROGRAMS:
        return f"program {name} not allowed"
    for rx, letters, longs in RERUN_INTERPRETERS:
        if rx.fullmatch(name):
            for t in args:
                if t == "--":
                    break
                if t.split("=", 1)[0] in longs or (
                        t.startswith("-") and not t.startswith("--") and set(t[1:]) & set(letters)):
                    return f"inline code for {name} not allowed"
    return None


def _kill_group(pgid: int, proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        if sig == signal.SIGTERM:
            try:  # leader gone; finish off any member that ignored TERM
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return


def _run_group(argv: list[str], cwd: Path, timeout: float) -> tuple[int | None, str]:
    """Run argv in its own session; on timeout kill the whole group. (None, out) on timeout.
    TMPDIR is a fresh dir under exec_tmpdir(), removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="factory-rerun-", dir=exec_tmpdir()) as tmp:
        return _run_group_in(argv, cwd, timeout, dict(scrubbed_env(), TMPDIR=tmp))


def _run_group_in(argv: list[str], cwd: Path, timeout: float, env: dict) -> tuple[int | None, str]:
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, text=True, errors="replace")
    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = ""
        return None, out or ""
    try:  # leftovers (background children) of a finished command
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return code, out


def rerun(job_dir: Path, rnd: int, lens: str, n: int, worktree: Path,
          timeout: float = 600) -> str:
    """Re-run one failing-check finding's command; write verify-<lens>-<n>.md; return line 1."""
    rd = load_round(job_dir, rnd)
    review = rd.reviews.get(lens)
    found = [f for f in (review.findings if review else []) if f.n == n]
    cmd = found[0].check_cmd if len(found) == 1 else None
    body = ""
    if not cmd:
        head = "VERIFY: UNVERIFIABLE — no check command for this finding"
    elif rerun_refusal(cmd):
        head = "VERIFY: UNVERIFIABLE — command refused"
        body = f"\nReason: {rerun_refusal(cmd)}\n"
    else:
        try:
            code, out = _run_group(shlex.split(cmd), worktree, timeout)
        except OSError as e:
            head = f"VERIFY: UNVERIFIABLE — could not start: {type(e).__name__}"
        else:
            if code is None:
                head = f"VERIFY: UNVERIFIABLE — timed out after {int(timeout)} s"
            else:
                tail = "\n".join(out.replace("`", "'").splitlines()[-40:])
                head = "VERIFY: CONFIRMED" if code != 0 else "VERIFY: FALSE POSITIVE"
                body = (f"\n## Evidence\n- Re-ran `{cmd}` in the worktree: exit {code}\n\n"
                        f"```text\n{tail}\n```\n")
    (job_dir / f"round-{rnd}" / f"verify-{lens}-{n}.md").write_text(
        head + "\n" + body, encoding="utf-8")
    return head


CHECK_KEYS = ("test", "build")   # the AGENTS.md commands run once per round, whole repo


def run_checks(job_dir: Path, rnd: int, worktree: Path, timeout: float = 900) -> str:
    """Run the repo's full test and build commands once (from the branch's AGENTS.md) and write
    round-<n>/checks.md for the reviewers; return its first line. Same refusals, environment and
    process-group kill as `rerun`. A failing command is a result, not an error."""
    import factory_init as fi
    agents = worktree / "AGENTS.md"
    cmds = fi.parse_agents(agents.read_text(encoding="utf-8", errors="replace")
                           if agents.is_file() else "")
    rows, failed, ran = [], False, 0
    for key in CHECK_KEYS:
        cmd = cmds.get(key)
        if not cmd:
            continue
        why = rerun_refusal(cmd)
        if why:
            rows.append(f"- `{cmd}` — not run: {why}")
            continue
        try:
            code, out = _run_group(shlex.split(cmd), worktree, timeout)
        except OSError as e:
            rows.append(f"- `{cmd}` — not run: could not start ({type(e).__name__})")
            continue
        if code is None:
            rows.append(f"- `{cmd}` — not run: timed out after {int(timeout)} s")
            continue
        ran += 1
        failed |= code != 0
        rows.append(f"- `{cmd}` — {'pass' if code == 0 else 'fail'}: exited {code}")
        if code != 0:
            tail = "\n".join(out.replace("`", "'").splitlines()[-40:])
            rows.append(f"\n```text\n{tail}\n```\n")
    if not rows:
        head = "CHECKS: NOT RUN — AGENTS.md names no test or build command"
    elif not ran:
        head = "CHECKS: NOT RUN — no named command could be run"
    else:
        head = "CHECKS: FAIL" if failed else "CHECKS: PASS"
    body = ("\n\n## Commands (run once on the whole repo by the coordinator)\n" + "\n".join(rows)
            + "\n") if rows else "\n"
    out_dir = job_dir / f"round-{rnd}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "checks.md").write_text(head + body, encoding="utf-8")
    return head


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory_review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("route")
    d = sub.add_parser("decide")
    for s in (r, d):
        s.add_argument("job")
        s.add_argument("round", type=int)
    d.add_argument("--overlaps", default="")
    rr = sub.add_parser("rerun")
    rr.add_argument("job")
    rr.add_argument("round", type=int)
    rr.add_argument("lens", choices=LENSES)
    rr.add_argument("n", type=int)
    rr.add_argument("--worktree", required=True)
    rr.add_argument("--timeout", type=float, default=600)
    ck = sub.add_parser("checks")
    ck.add_argument("job")
    ck.add_argument("round", type=int)
    ck.add_argument("--worktree", required=True)
    ck.add_argument("--timeout", type=float, default=900)
    sub.add_parser("tmpdir")
    u = sub.add_parser("ui-applicable")
    u.add_argument("diff")
    u.add_argument("--globs")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "route":
            rd = load_round(Path(a.job), a.round)
            out = [{"lens": lens, "n": f.n, "route": route(lens, f), "location": f.location,
                    "text": f.text, "check_cmd": f.check_cmd}
                   for lens in LENSES if rd.reviews[lens].verdict == "CHANGES"
                   for f in rd.reviews[lens].findings]
            print(json.dumps(out, indent=2))
        elif a.cmd == "tmpdir":
            print(exec_tmpdir())
        elif a.cmd == "checks":
            print(run_checks(Path(a.job), a.round, Path(a.worktree), a.timeout))
        elif a.cmd == "rerun":
            print(rerun(Path(a.job), a.round, a.lens, a.n, Path(a.worktree), a.timeout))
        elif a.cmd == "decide":
            rd = load_round(Path(a.job), a.round)
            overlaps = [x for x in a.overlaps.split(",") if x]
            reasons = draft_reasons(rd, overlaps)
            print(json.dumps({"draft": bool(reasons), "reasons": reasons}, indent=2))
        else:
            globs = [g for g in (a.globs or "").split(",") if g] or DEFAULT_UI_GLOBS
            text = Path(a.diff).read_text(encoding="utf-8", errors="replace")
            paths = _diff_paths(text)
            if text.strip() and not paths:
                print("no file paths found in non-empty diff; assuming UI applicable",
                      file=sys.stderr)
                print("true")
            else:
                print("true" if ui_applicable(paths, globs) else "false")
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
