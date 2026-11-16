#!/usr/bin/env python3
"""Lean worker context for the factory skill (P7).

Every Orca worker is a fresh Claude session that writes the user's whole setup (enabled plugins,
MCP servers, hooks) into the prompt cache before it does anything: ~66k tokens on a trivial
call, ~30k of it never used by a factory worker. This writes a per-worktree, untracked
`.claude/settings.local.json` that disables the user's plugins and denies the MCP servers a
worker does not need, which measured -52% (66.4k -> 31.6k).

Usage:
  factory_lean.py learn [--claude claude]
      Record the MCP servers this machine has (`claude mcp list`) in <claude home>/factory-lean.json.
      Without it only plugins are disabled (-29%).
  factory_lean.py write --worktree WT --job-dir J
      Merge the lean keys into WT/.claude/settings.local.json and keep it out of `git status`
      (J/worktree-exclude + the worktree's core.excludesFile). Prints JSON {status, ...}.
      Exit 0 always; `status` is `lean`, `off` or `skipped` (reason given). Never edits a tracked file.

Off switch: FACTORY_LEAN=off in the environment. The project `.mcp.json` servers (chrome-devtools
for the browser lenses) are not user-level servers, so they are untouched.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

LEAN_FILE = "factory-lean.json"
SETTINGS = ".claude/settings.local.json"
EXCLUDE_LINES = ("/.mcp.json", "/.claude/settings.local.json")


def claude_home(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def parse_mcp_list(text: str) -> list[str]:
    """Server names from `claude mcp list`: lines like `claude.ai Gmail: https://… - ✓ Connected`
    or `plugin:vercel:vercel: https://… (HTTP) - …`; the name ends at the first `: `."""
    names = []
    for line in text.splitlines():
        m = re.match(r"^(\S.*?): \S", line)
        if m and not line.startswith(("Checking", "No MCP")):
            names.append(m.group(1))
    return sorted(set(names))


def learn(claude: str = "claude", home: Path | None = None) -> dict:
    home = home or claude_home()
    p = subprocess.run([claude, "mcp", "list"], capture_output=True, text=True, timeout=180,
                       stdin=subprocess.DEVNULL)
    names = parse_mcp_list(p.stdout)
    # Plugin servers are covered by disabling their plugin; deny only the rest by name.
    deny = [n for n in names if not n.startswith("plugin:")]
    out = {"deny_mcp_servers": deny}
    (home / LEAN_FILE).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def lean_settings(home: Path) -> dict:
    s = {}
    try:
        user = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        plugins = {k: False for k in (user.get("enabledPlugins") or {})}
        if plugins:
            s["enabledPlugins"] = plugins
    except (OSError, ValueError):
        pass
    try:
        deny = json.loads((home / LEAN_FILE).read_text(encoding="utf-8")).get("deny_mcp_servers")
    except (OSError, ValueError):
        deny = None
    if deny:
        s["deniedMcpServers"] = [{"serverName": n} for n in deny]
    return s


def _git(wt: Path, *a, check=False):
    return subprocess.run(["git", "-C", str(wt), "-c", "core.hooksPath=/dev/null", *a],
                          capture_output=True, text=True, check=check)


def write(worktree: Path, job_dir: Path, env=None, home: Path | None = None) -> dict:
    env = os.environ if env is None else env
    if env.get("FACTORY_LEAN", "").lower() in ("off", "0", "false", "no"):
        return {"status": "off"}
    target = worktree / SETTINGS
    if _git(worktree, "ls-files", "--error-unmatch", SETTINGS).returncode == 0:
        return {"status": "skipped", "reason": f"{SETTINGS} is tracked in this repo"}
    lean = lean_settings(home or claude_home(env))
    if not lean:
        return {"status": "skipped", "reason": "no user plugins or MCP list to turn off"}
    cur = {}
    if target.is_file():
        try:
            cur = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            return {"status": "skipped", "reason": f"{SETTINGS} is not valid JSON"}
    cur.update(lean)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
    # Keep it (and the browser config) out of `git status` without touching the shared info/exclude.
    ex = job_dir / "worktree-exclude"
    have = ex.read_text(encoding="utf-8").splitlines() if ex.is_file() else []
    ex.write_text("\n".join(have + [x for x in EXCLUDE_LINES if x not in have]) + "\n",
                  encoding="utf-8")
    _git(worktree, "config", "--worktree", "core.excludesFile", str(ex.resolve()))
    return {"status": "lean", "plugins_off": len(lean.get("enabledPlugins", {})),
            "mcp_denied": len(lean.get("deniedMcpServers", []))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    le = sub.add_parser("learn")
    le.add_argument("--claude", default="claude")
    w = sub.add_parser("write")
    w.add_argument("--worktree", required=True)
    w.add_argument("--job-dir", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "learn":
        print(json.dumps(learn(a.claude)))
    else:
        print(json.dumps(write(Path(a.worktree), Path(a.job_dir))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
