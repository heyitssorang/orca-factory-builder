#!/usr/bin/env python3
"""Lint the P7 roster, agents.lock and (when present) skills/factory/SKILL.md.

Usage: lint_roster.py [--installed] [--root DIR]
  --installed  also check ~/.claude/agents for name collisions with the roster
Exit 0 only if every check passes.
"""
import argparse
import json
import re
import sys
from pathlib import Path

PACK = "p7"
MAX_PERSONA_LINES = 90
MAX_SKILL_LINES = 500
BASE_TOOLS = {"Read", "Glob", "Grep", "Bash", "Write", "Edit"}
ALWAYS_FORBID = {"Skill", "NotebookEdit", "Agent", "Task"}
MODE_FORBID = {
    "read-only": {"Write", "Edit", "Bash"},
    "mcp-read-only": {"Write", "Edit", "Bash"},
    "designer": {"Edit", "Bash"},
    "runner": {"Write", "Edit"},
    "writer": set(),
}
CDP = "mcp__chrome-devtools__"
MCP_ALLOWED = {"navigate_page", "new_page", "list_pages", "select_page", "take_snapshot",
               "wait_for", "list_console_messages", "list_network_requests", "resize_page",
               "emulate", "click", "hover", "press_key"}
SCREENSHOT = "take_screenshot"
SCREENSHOT_AGENTS = {"reviewer-ui-p7", "browser-verifier-p7"}
MCP_FORBIDDEN = {"fill", "fill_form", "upload_file", "evaluate_script", "handle_dialog"}
ALLOWED_SKILLS = {"reviewer-security-p7": {"differential-review"}}
EXTERNAL_AGENTS = {"review-verifier-p2", "review-verifier-p2-security"}
FOOTER_PHRASES = ("data, never instructions", "Never push", "Not verified")
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
# Global constraints: the mode and model each P7 agent must have in agents.lock.
EXPECTED = {
    "scout-p7": ("designer", HAIKU),
    "spec-writer-p7": ("designer", SONNET),
    "planner-p7": ("designer", OPUS),
    "builder-p7": ("writer", SONNET),
    "reviewer-security-p7": ("read-only", OPUS),
    "reviewer-code-p7": ("runner", OPUS),
    "reviewer-ux-p7": ("mcp-read-only", OPUS),
    "reviewer-ui-p7": ("mcp-read-only", OPUS),
    "browser-verifier-p7": ("mcp-read-only", SONNET),
    "risk-flagger-p7": ("read-only", OPUS),
}
_REVIEW = ("VERDICT: PASS", "VERDICT: CHANGES", "VERDICT: NOT RUN — ", "## Findings", "None.",
           "## Prior findings", "listed again as a numbered finding")
FORMAT_TOKENS = {
    "scout-p7": ("## Commands", "## Relevant files", "## Patterns to follow"),
    "spec-writer-p7": ("## Acceptance criteria", "PRODUCT DECISION"),
    "planner-p7": ("## Approach", "## Steps", "## Tests", "## Pitfalls"),
    "builder-p7": ("STATUS: READY", "STATUS: BLOCKED", "## Criteria", ". met — ",
                   ". not met — "),
    "reviewer-security-p7": _REVIEW,
    "reviewer-code-p7": _REVIEW + ("## Checks", "Check: `", "` exited "),
    "reviewer-ux-p7": _REVIEW + ("   a. ",),
    "reviewer-ui-p7": _REVIEW + ("   a. ",),
    "browser-verifier-p7": ("VERIFY: CONFIRMED", "VERIFY: FALSE POSITIVE",
                            "VERIFY: UNVERIFIABLE — "),
    "risk-flagger-p7": ("RISK: NONE", "RISK: FLAGGED"),
}
AGENT_NAME = re.compile(r"(?<![A-Za-z0-9_-])([a-z][a-z0-9]*(?:-[a-z0-9]+)*-p\d+(?:-security)?)"
                        r"(?![A-Za-z0-9_])")


def frontmatter(text: str) -> dict | None:
    """Minimal YAML frontmatter: `key: value` lines and `  - item` lists."""
    m = re.match(r"---\n(.*?)\n---\n", text.replace("\r\n", "\n"), re.S)
    if not m:
        return None
    out: dict = {}
    key = None
    for line in m.group(1).splitlines():
        if re.match(r"\s+-\s", line) and key:
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(line.strip()[1:].strip())
        elif re.match(r"[A-Za-z_][\w-]*\s*:", line):
            key, _, val = line.partition(":")
            key = key.strip()
            out[key] = val.strip().strip('"') or []
    return out


def _tools(fm: dict) -> set[str]:
    raw = fm.get("tools", "")
    items = raw if isinstance(raw, list) else raw.split(",")
    return {t.strip() for t in items if t.strip()}


def _check_tools(n: str, mode: str, tools: set[str], err) -> None:
    base = {t for t in tools if not t.startswith("mcp__")}
    mcp = tools - base
    unknown = base - BASE_TOOLS - ALWAYS_FORBID
    if unknown:
        err(f"{n}: unknown tools {sorted(unknown)}")
    bad = tools & (MODE_FORBID[mode] | ALWAYS_FORBID)
    if bad:
        err(f"{n}: {mode} role has {sorted(bad)}")
    if mcp and mode != "mcp-read-only":
        err(f"{n}: mcp tools only allowed in mcp-read-only mode")
    for t in sorted(mcp):
        short = t[len(CDP):] if t.startswith(CDP) else None
        if short in MCP_FORBIDDEN:
            err(f"{n}: forbidden tool {short}")
        elif short == SCREENSHOT:
            if n not in SCREENSHOT_AGENTS:
                err(f"{n}: {SCREENSHOT} not allowed")
        elif short not in MCP_ALLOWED:
            err(f"{n}: mcp tool outside allow-list {short or t}")


def _check_body(n: str, text: str, err) -> None:
    lines = len(text.splitlines())
    if lines > MAX_PERSONA_LINES:
        err(f"{n}: {lines} lines (max {MAX_PERSONA_LINES})")
    m = re.search(r"^## Constraints\b.*", text, re.M | re.S)
    if not m:
        err(f"{n}: constraints footer missing")
    else:
        for phrase in FOOTER_PHRASES:
            if phrase not in m.group(0):
                err(f"{n}: constraints footer missing {phrase!r}")
    for tok in FORMAT_TOKENS.get(n, ()):
        if tok not in text:
            err(f"{n}: missing format token {tok!r}")


def _check_skill(root: Path, names: set[str], err) -> None:
    skill = root / "skills" / "factory" / "SKILL.md"
    if not skill.exists():
        return
    text = skill.read_text(encoding="utf-8")
    if len(text.splitlines()) > MAX_SKILL_LINES:
        err(f"SKILL.md: over {MAX_SKILL_LINES} lines")
    used = set(AGENT_NAME.findall(text))
    for u in sorted(used - names - EXTERNAL_AGENTS):
        err(f"SKILL.md: names unknown agent {u}")
    for n in sorted(names - used):
        err(f"SKILL.md: does not name {n}")


def _check_installed(roster: Path, names: set[str], home: Path, err) -> None:
    agents = home / "agents"
    seen: dict[str, Path] = {}
    for p in sorted(agents.rglob("*.md")) if agents.is_dir() else []:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        nm = (frontmatter(text) or {}).get("name") or p.stem
        if nm in seen:
            err(f"agent name {nm!r} defined twice: {seen[nm]} and {p}")
            continue
        seen[nm] = p
        mine = roster / f"{nm}.md"
        if nm in names and mine.exists() and text != mine.read_text(encoding="utf-8"):
            err(f"{nm}: installed agent differs from roster/{nm}.md")


def lint(root: Path, installed: bool = False, home: Path | None = None) -> list[str]:
    errors: list[str] = []
    err = errors.append
    roster = root / "roster"
    try:
        rows = json.loads((root / "agents.lock").read_text(encoding="utf-8"))["rows"]
        assert isinstance(rows, list)
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as e:
        return [f"agents.lock: unreadable ({type(e).__name__}: {e})"]

    names: set[str] = set()
    for row in rows:
        n = row.get("install_name", "?")
        if n in names:
            err(f"duplicate install_name {n}")
            continue
        names.add(n)
        if not n.endswith(f"-{PACK}"):
            err(f"{n}: install_name must end with -{PACK}")
        if row.get("pack") != PACK:
            err(f"{n}: pack is {row.get('pack')!r}, want {PACK!r}")
        if row.get("source") != "in-house":
            err(f"{n}: source is {row.get('source')!r}, want 'in-house'")
        for key in ("role", "model"):
            if not row.get(key):
                err(f"{n}: missing {key}")
        mode = row.get("mode")
        if n in EXPECTED:
            want_mode, want_model = EXPECTED[n]
            if mode != want_mode:
                err(f"{n}: lock mode is {mode!r}, want {want_mode!r}")
            if row.get("model") != want_model:
                err(f"{n}: lock model is {row.get('model')!r}, want {want_model!r}")
        if mode not in MODE_FORBID:
            err(f"{n}: unknown mode {mode!r}")
            continue
        f = roster / f"{n}.md"
        if not f.exists():
            err(f"{n}: roster file missing")
            continue
        text = f.read_text(encoding="utf-8")
        fm = frontmatter(text)
        if fm is None:
            err(f"{n}: no frontmatter")
            continue
        if fm.get("name") != n:
            err(f"{n}: frontmatter name is {fm.get('name')!r}")
        if not fm.get("description"):
            err(f"{n}: missing description")
        if fm.get("model") != row.get("model"):
            err(f"{n}: model differs from lock")
        tools = _tools(fm)
        if tools != set(row.get("tools") or []):
            err(f"{n}: tools differ from lock")
        _check_tools(n, mode, tools, err)
        # The lock's own tools must obey the same rules.
        if set(row.get("tools") or []) != tools:
            _check_tools(n + " (lock)", mode, set(row.get("tools") or []), err)
        skills = fm.get("skills") or []
        skills = skills if isinstance(skills, list) else [skills]
        for s in skills:
            if s not in ALLOWED_SKILLS.get(n, set()):
                err(f"{n}: skill {s!r} not allowed")
        if sorted(skills) != sorted(row.get("skills") or []):
            err(f"{n}: skills differ from lock")
        if "hooks" in fm:
            err(f"{n}: frontmatter hooks not allowed")
        _check_body(n, text, err)

    for n in sorted(set(EXPECTED) - names):
        err(f"{n}: missing from agents.lock")
    extra = {p.stem for p in roster.glob("*.md")} - names if roster.is_dir() else set()
    if extra:
        err(f"roster files not in agents.lock: {sorted(extra)}")
    _check_skill(root, names, err)
    if installed:
        _check_installed(roster, names, home or Path.home() / ".claude", err)
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lint_roster")
    ap.add_argument("--installed", action="store_true")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    a = ap.parse_args(argv)
    errors = lint(a.root, installed=a.installed)
    if errors:
        print("LINT FAILED")
        for e in errors:
            print(" -", e)
        return 1
    print("lint ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
