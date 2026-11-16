import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_devin as fd  # noqa: E402

SPEC = """# Add a count method

## Acceptance criteria
1. `count()` returns the number of items.
2. `count()` is covered by a test.
"""

GOOD = """STATUS: READY

## Criteria
1. met — test_count passes
2. met — tests/test_count.py exists

## Checks
- `pytest -q` — pass

## Not verified
- nothing
"""


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def fake_devin(directory: Path, body: str, rc: int = 0) -> str:
    """A `devin` stand-in: a shell script that runs `body` in the cwd, then prints $REPORT."""
    p = directory / "devin"
    p.write_text(f"#!/bin/sh\n{body}\nprintf '%s' \"$REPORT\"\nexit {rc}\n")
    p.chmod(0o755)
    return str(p)


class EngineTests(unittest.TestCase):
    OPT_IN = "- test: pytest\n- builder: devin\n"

    def which(self, _):
        return "/usr/bin/devin"

    def test_opt_in_round_0_and_1_use_devin(self):
        for r in (0, 1):
            self.assertEqual(fd.engine(r, self.OPT_IN, env={}, which=self.which), "devin")

    def test_round_2_falls_back_to_claude(self):
        self.assertEqual(fd.engine(2, self.OPT_IN, env={}, which=self.which), "claude")

    def test_no_opt_in_is_claude(self):
        self.assertEqual(fd.engine(0, "- test: pytest\n", env={}, which=self.which), "claude")
        self.assertEqual(fd.engine(0, "builder: claude\n", env={}, which=self.which), "claude")

    def test_env_override_and_missing_cli(self):
        self.assertEqual(fd.engine(0, self.OPT_IN, env={"FACTORY_BUILDER": "claude"},
                                   which=self.which), "claude")
        self.assertEqual(fd.engine(0, self.OPT_IN, env={}, which=lambda _: None), "claude")


class ScrubTests(unittest.TestCase):
    def test_drops_secret_shaped_variables(self):
        env = {"PATH": "/bin", "AZURE_DEVOPS_EXT_PAT": "x", "ORCA_AZURE_DEVOPS_TOKEN": "y",
               "GIT_ASKPASS": "z", "HOME": "/h", "GIT_CONFIG_KEY_0": "k", "MY_API_KEY": "q"}
        self.assertEqual(fd.scrub_env(env), {"PATH": "/bin", "HOME": "/h"})


class JudgeTests(unittest.TestCase):
    def test_good_report_passes(self):
        self.assertEqual(fd.judge(GOOD, [1, 2]), (True, ""))

    def test_rejections(self):
        for text, nums in (
            ("no status line\n", [1, 2]),
            ("STATUS: BLOCKED\n\n## Criteria\n1. not met — x\n2. not met — y\n", [1, 2]),
            (GOOD.replace("2. met — tests/test_count.py exists\n", ""), [1, 2]),
            (GOOD.replace("2. met", "2. not met"), [1, 2]),
            (GOOD.replace("## Criteria\n", "## Criteria\nprose line\n"), [1, 2]),
        ):
            ok, reason = fd.judge(text, nums)
            self.assertFalse(ok, text)
            self.assertTrue(reason)


class RunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT / "tests")  # /tmp may be noexec
        self.addCleanup(self._tmp.cleanup)
        t = Path(self._tmp.name)
        self.wt, self.job, self.bin = t / "wt", t / "job", t / "bin"
        for d in (self.wt, self.job, self.bin):
            d.mkdir()
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "config", "user.email", "t@t")
        git(self.wt, "config", "user.name", "t")
        (self.wt / "a.py").write_text("x = 1\n")
        git(self.wt, "add", "a.py")
        git(self.wt, "commit", "-qm", "init")
        git(self.wt, "switch", "-qc", "factory/001-count")
        (self.job / "spec.md").write_text(SPEC)
        self.head0 = git(self.wt, "rev-parse", "HEAD")

    def go(self, body, report=GOOD, rc=0, round_=0, rework=None):
        os.environ["REPORT"] = report
        self.addCleanup(os.environ.pop, "REPORT", None)
        cmd = fake_devin(self.bin, body, rc)
        return fd.run(self.job, self.wt, "001-count", round_, rework, timeout=30, devin_cmd=cmd)

    def build_md(self):
        return (self.job / "build.md").read_text()

    def test_happy_path_commits_and_reports(self):
        res = self.go("echo 'def count(): return 0' > count.py; "
                      "mkdir -p tests; echo 'def test_c(): pass' > tests/test_count.py")
        self.assertEqual(res["status"], "READY")
        self.assertTrue(self.build_md().startswith("STATUS: READY\n"))
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")
        self.assertEqual(git(self.wt, "rev-list", "--count", f"{self.head0}..HEAD"), "1")
        self.assertEqual(sorted(res["files"]), ["count.py", "tests/test_count.py"])
        self.assertTrue((self.job / "devin-brief.md").is_file())

    def test_ready_without_changes_is_blocked(self):
        res = self.go("true")
        self.assertEqual(res["status"], "BLOCKED")
        self.assertIn("changed no files", self.build_md())
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), self.head0)

    def test_report_missing_a_criterion_is_blocked_and_left_uncommitted(self):
        res = self.go("echo y > count.py", report=GOOD.replace("2. met — tests/test_count.py exists\n", ""))
        self.assertEqual(res["status"], "BLOCKED")
        self.assertTrue(self.build_md().startswith("STATUS: BLOCKED\n"))
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")  # reset for a retry
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), self.head0)
        left = self.job / "devin-leftover"
        self.assertEqual((left / "files" / "count.py").read_text(), "y\n")
        self.assertIn("count.py", (left / "paths.txt").read_text())

    def test_secret_path_is_never_committed(self):
        res = self.go("echo y > count.py; echo SECRET=1 > .env")
        self.assertEqual(res["status"], "BLOCKED")
        self.assertIn(".env", self.build_md())
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), self.head0)

    def test_secret_content_blocks_after_commit_and_says_so(self):
        res = self.go("printf 'k = \"-----BEGIN RSA PRIVATE KEY-----\"\\n' > count.py")
        self.assertEqual(res["status"], "BLOCKED")
        self.assertIn("secret-shaped content", self.build_md())
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), self.head0)  # commit dropped
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")
        self.assertFalse((self.job / "devin-leftover" / "changes.patch").exists())
        self.assertFalse((self.job / "devin-leftover" / "files").exists())

    def test_plan_md_goes_into_the_brief(self):
        (self.job / "plan.md").write_text("## Steps\n1. edit count.py\n")
        self.go("echo 'def count(): return 0' > count.py; "
                "mkdir -p tests; echo 'def test_c(): pass' > tests/test_count.py")
        self.assertIn("<plan>\n## Steps", (self.job / "devin-brief.md").read_text())

    def test_preamble_before_the_report_is_ignored(self):
        res = self.go("echo 'def count(): return 0' > count.py; "
                      "mkdir -p tests; echo 'def test_c(): pass' > tests/test_count.py",
                      report="working...\nran tools\n```\n" + GOOD + "```\n")
        self.assertEqual(res["status"], "READY")
        self.assertTrue(self.build_md().startswith("STATUS: READY\n"))

    def test_report_glued_to_narration_without_newline(self):
        res = self.go("echo 'def count(): return 0' > count.py; "
                      "mkdir -p tests; echo 'def test_c(): pass' > tests/test_count.py",
                      report="Files match the spec. Adding it and the tests." + GOOD)
        self.assertEqual(res["status"], "READY")
        self.assertTrue(self.build_md().startswith("STATUS: READY\n"))

    def test_timeout_kills_children_and_resets_worktree(self):
        marker = self.wt.parent / "late.txt"
        cmd = fake_devin(self.bin, f"echo y > count.py; (sleep 3; echo late > {marker}) & sleep 60")
        res = fd.run(self.job, self.wt, "001-count", 0, None, timeout=1, devin_cmd=cmd)
        self.assertEqual(res["status"], "BLOCKED")
        self.assertIn("timed out", self.build_md())
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")
        import time
        time.sleep(4)
        self.assertFalse(marker.exists())
        self.assertFalse((self.wt / "late.txt").exists())

    def test_wrong_branch_is_blocked_without_running_devin(self):
        git(self.wt, "switch", "-q", "main")
        res = self.go("echo y > count.py")
        self.assertEqual(res["status"], "BLOCKED")
        self.assertFalse((self.wt / "count.py").exists())

    def test_dirty_worktree_is_blocked(self):
        (self.wt / "stray.txt").write_text("x")
        res = self.go("echo y > count.py")
        self.assertEqual(res["status"], "BLOCKED")
        self.assertFalse((self.wt / "count.py").exists())

    def test_devin_missing_or_failed_untouched_is_unavailable(self):
        res = fd.run(self.job, self.wt, "001-count", 0, None, timeout=30,
                     devin_cmd=str(self.bin / "no-such-devin"))
        self.assertEqual(res["status"], "UNAVAILABLE")
        res = self.go("echo auth failed >&2", rc=1)
        self.assertEqual(res["status"], "UNAVAILABLE")
        self.assertFalse((self.job / "build.md").exists())

    def test_lock_file_change_is_listed(self):
        res = self.go("echo y > count.py; echo '{}' > package-lock.json")
        self.assertEqual(res["status"], "READY")
        self.assertIn("## Touched sensitive files\n- `package-lock.json`", self.build_md())

    def test_rework_brief_carries_the_findings(self):
        rw = self.job / "rework-1.md"
        rw.write_text("## Note\nfix it\n\n## Confirmed findings\n1.1 code a.py:1 — bad\n")
        self.go("echo y > count.py", rework=rw, round_=1)
        brief = (self.job / "devin-brief.md").read_text()
        self.assertIn("## Rework round 2", brief)
        self.assertIn("1.1 code a.py:1 — bad", brief)

    def test_cli_exit_code_3_when_unavailable(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "factory_devin.py"), "run",
                            "--job-dir", str(self.job), "--worktree", str(self.wt),
                            "--id", "001-count", "--round", "0"],
                           capture_output=True, text=True,
                           env={**os.environ, "PATH": "/usr/bin:/bin"})  # no devin on PATH
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
