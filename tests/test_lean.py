import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import factory_lean as fl  # noqa: E402

MCP_LIST = """Checking MCP server health…

claude.ai Gmail: https://gmail.mcp.claude.com/mcp - ✓ Connected
claude.ai monday.com: https://mcp.monday.com/mcp - ✓ Connected
plugin:vercel:vercel: https://mcp.vercel.com (HTTP) - ✓ Connected
thycotic: /usr/bin/node /x/server.js  - ✓ Connected
"""


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True).stdout


class LeanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        t = Path(self._tmp.name)
        self.home, self.wt, self.job = t / "home", t / "wt", t / "job"
        for d in (self.home, self.wt, self.job):
            d.mkdir()
        (self.home / "settings.json").write_text(json.dumps(
            {"enabledPlugins": {"a@m": True, "b@m": True}, "model": "x"}))
        (self.home / fl.LEAN_FILE).write_text(json.dumps({"deny_mcp_servers": ["claude.ai Gmail"]}))
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "config", "extensions.worktreeConfig", "true")
        (self.wt / "a.txt").write_text("x")
        git(self.wt, "add", "-A")
        git(self.wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")

    def test_parse_mcp_list(self):
        self.assertEqual(fl.parse_mcp_list(MCP_LIST),
                         ["claude.ai Gmail", "claude.ai monday.com", "plugin:vercel:vercel",
                          "thycotic"])

    def test_writes_lean_settings_untracked_and_excluded(self):
        r = fl.write(self.wt, self.job, env={}, home=self.home)
        self.assertEqual(r, {"status": "lean", "plugins_off": 2, "mcp_denied": 1})
        s = json.loads((self.wt / ".claude" / "settings.local.json").read_text())
        self.assertEqual(s["enabledPlugins"], {"a@m": False, "b@m": False})
        self.assertEqual(s["deniedMcpServers"], [{"serverName": "claude.ai Gmail"}])
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")

    def test_merges_with_existing_browser_config(self):
        (self.wt / ".claude").mkdir()
        (self.wt / ".claude" / "settings.local.json").write_text(
            json.dumps({"enabledMcpjsonServers": ["chrome-devtools"]}))
        fl.write(self.wt, self.job, env={}, home=self.home)
        s = json.loads((self.wt / ".claude" / "settings.local.json").read_text())
        self.assertEqual(s["enabledMcpjsonServers"], ["chrome-devtools"])
        self.assertIn("enabledPlugins", s)
        self.assertIn("/.mcp.json", (self.job / "worktree-exclude").read_text())

    def test_off_switch_and_tracked_file_and_no_settings(self):
        self.assertEqual(fl.write(self.wt, self.job, env={"FACTORY_LEAN": "off"}, home=self.home),
                         {"status": "off"})
        self.assertFalse((self.wt / ".claude").exists())
        (self.wt / ".claude").mkdir()
        (self.wt / ".claude" / "settings.local.json").write_text("{}")
        git(self.wt, "add", "-f", ".claude/settings.local.json")
        r = fl.write(self.wt, self.job, env={}, home=self.home)
        self.assertEqual(r["status"], "skipped")
        self.assertIn("tracked", r["reason"])
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(fl.write(self.wt, self.job, env={}, home=empty)["status"], "skipped")

    def test_cli(self):
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "factory_lean.py"), "write",
                            "--worktree", str(self.wt), "--job-dir", str(self.job)],
                           capture_output=True, text=True, env={"CLAUDE_CONFIG_DIR": str(self.home),
                                                                "PATH": "/usr/bin:/bin"})
        self.assertEqual(json.loads(p.stdout)["status"], "lean", p.stderr)


if __name__ == "__main__":
    unittest.main()
