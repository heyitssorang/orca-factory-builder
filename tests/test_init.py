import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_init as fin  # noqa: E402
import factory_integrate as fi  # noqa: E402


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=check)


class InitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.t = Path(self._tmp.name)
        cfg = self.t / "gitconfig"
        cfg.write_text("[user]\n\tname = T\n\temail = t@example.com\n")
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg),
                                           "GIT_CONFIG_SYSTEM": "/dev/null"})
        env.start()
        self.addCleanup(env.stop)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fin.main(list(args))
        return subprocess.CompletedProcess(args, rc, out.getvalue(), err.getvalue())

    def write(self, repo, files):
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)

    # --- git-init / inspect ----------------------------------------------------

    def test_git_init_new_directory(self):
        p = self.t / "new" / "repo"
        self.assertEqual(fin.inspect(p)["exists"], False)
        out = fin.git_init(p)
        self.assertEqual((out["created"], out["branch"], out["untracked"]), (True, "main", 0))
        self.assertEqual(git(p, "rev-parse", "HEAD").stdout.strip(), out["initial_commit"])
        info = fin.inspect(p)
        self.assertEqual((info["git"], info["commits"], info["mode"], info["default"],
                          info["origin_url"]), (True, True, "local", "main", None))
        again = fin.git_init(p)
        self.assertEqual((again["created"], again["initial_commit"]), (False, None))

    def test_git_init_existing_files_left_untracked(self):
        p = self.t / "proj"
        self.write(p, {"a.py": "x\n"})
        out = fin.git_init(p)
        self.assertEqual(out["untracked"], 1)
        self.assertEqual(git(p, "ls-files").stdout, "")

    def test_git_init_unborn_repo_gets_commit(self):
        p = self.t / "proj"
        p.mkdir()
        git(p, "init", "-q", "-b", "trunk")
        out = fin.git_init(p)
        self.assertEqual((out["created"], out["branch"]), (False, "trunk"))
        self.assertTrue(out["initial_commit"])

    def test_git_init_refuses_inside_other_repo(self):
        outer = self.t / "outer"
        fin.git_init(outer)
        r = self.cli("git-init", str(outer / "sub"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("is inside the git repo", r.stderr)
        self.assertFalse((outer / "sub").exists())
        self.assertEqual(fin.inspect(outer / "sub")["inside_repo"], str(outer.resolve()))

    def test_git_init_refuses_bad_branch(self):
        r = self.cli("git-init", str(self.t / "x"), "--branch", "a..b")
        self.assertEqual(r.returncode, 2)

    def test_inspect_reports_agents_and_browser_config(self):
        p = self.t / "proj"
        fin.git_init(p)
        self.write(p, {"AGENTS.md": "- test: `make test`\n", ".mcp.json": "{}\n"})
        git(p, "add", "AGENTS.md", ".mcp.json")
        git(p, "commit", "-q", "-m", "agents")
        self.write(p, {"AGENTS.md": "- test: `make test`\n- build: make\n"})
        info = json.loads(self.cli("inspect", str(p)).stdout)
        self.assertEqual(info["agents_md"], {"file": True, "keys": ["build", "test"],
                                             "committed_keys": ["test"]})
        self.assertEqual(info["browser_config_tracked"], [".mcp.json"])

    def test_inspect_ado_origin(self):
        p = self.t / "proj"
        fin.git_init(p)
        url = "https://example-org@dev.azure.com/example-org/Infra/_git/proj"
        git(p, "remote", "add", "origin", url)
        info = fin.inspect(p)
        self.assertEqual((info["mode"], info["origin_url"]), ("ado", url))
        git(p, "remote", "set-url", "origin", "https://github.com/x/proj.git")
        self.assertEqual(fin.inspect(p)["mode"], "local")

    # --- detect / agents -------------------------------------------------------

    def test_detect_node_npm(self):
        p = self.t / "n"
        self.write(p, {"package.json": json.dumps({"scripts": {
            "test": "vitest", "build": "vite build", "dev": "vite"}}), "package-lock.json": "{}"})
        self.assertEqual(fin.detect(p), {"install": "npm ci", "test": "npm test",
                                         "build": "npm run build",
                                         "dev": "npm run dev -- --port {port}"})

    def test_detect_node_pnpm_and_placeholder_test(self):
        p = self.t / "n"
        self.write(p, {"package.json": json.dumps({"scripts": {
            "test": 'echo "Error: no test specified" && exit 1', "start": "next start"}}),
            "pnpm-lock.yaml": ""})
        self.assertEqual(fin.detect(p), {"install": "pnpm install --frozen-lockfile",
                                         "dev": "pnpm run start --port {port}"})

    def test_detect_makefile_wins(self):
        p = self.t / "m"
        self.write(p, {"Makefile": "X := 1\ntest:\n\tgo test\nbuild: dep\n\tgo build\n",
                       "go.mod": "module x\n"})
        self.assertEqual(fin.detect(p), {"test": "make test", "build": "make build"})

    def test_detect_python_and_static(self):
        p = self.t / "py"
        self.write(p, {"notes.py": "", "tests/test_x.py": "", "web/index.html": "<h1>"})
        self.assertEqual(fin.detect(p), {
            "test": "python3 -m unittest discover -s tests -v",
            "dev": "python3 -m http.server {port} --directory web", "ui_globs": "web/*"})
        self.write(p, {"pyproject.toml": "[tool.pytest.ini_options]\n"})
        self.assertEqual(fin.detect(p)["test"], "python3 -m pytest -q")

    def test_detect_cargo(self):
        p = self.t / "r"
        self.write(p, {"Cargo.toml": "[package]\n"})
        self.assertEqual(fin.detect(p), {"test": "cargo test", "build": "cargo build"})

    def test_agents_creates_file_readable_by_gate(self):
        p = self.t / "py"
        fin.git_init(p)
        self.write(p, {"tests/test_x.py": "", "a.py": ""})
        out = json.loads(self.cli("agents", str(p), "--set", "build=python3 -m compileall -q a.py",
                                  "--write").stdout)
        self.assertEqual(out["proposed"], {"test": "python3 -m unittest discover -s tests -v",
                                           "build": "python3 -m compileall -q a.py"})
        self.assertEqual(out["missing"], ["install", "dev", "ui_globs"])
        self.assertTrue(out["written"])
        git(p, "add", "AGENTS.md")
        git(p, "commit", "-q", "-m", "agents")
        self.assertEqual(fi.read_agents_commands(p, "HEAD"),
                         {"test": "python3 -m unittest discover -s tests -v",
                          "build": "python3 -m compileall -q a.py"})

    def test_agents_appends_only_missing_keys(self):
        p = self.t / "py"
        self.write(p, {"AGENTS.md": "# Repo\n\n- test: `pytest`", "tests/t.py": "",
                       "Makefile": "build:\n\ttrue\n"})
        out = fin.agents(p, {}, write=True)
        self.assertEqual(out["existing"], {"test": "pytest"})
        self.assertEqual(out["lines"], ["- build: `make build`"])
        self.assertEqual((p / "AGENTS.md").read_text(),
                         "# Repo\n\n- test: `pytest`\n\n## Factory\n\n- build: `make build`\n")
        self.assertFalse(fin.agents(p, {}, write=True)["written"])

    def test_agents_set_empty_drops_and_dry_run_writes_nothing(self):
        p = self.t / "r"
        self.write(p, {"Cargo.toml": ""})
        out = fin.agents(p, {"build": ""}, write=False)
        self.assertEqual((out["proposed"], out["written"]), ({"test": "cargo test"}, False))
        self.assertFalse((p / "AGENTS.md").exists())

    def test_agents_refuses_bad_values(self):
        p = self.t / "r"
        p.mkdir()
        r = self.cli("agents", str(p), "--set", "test=a`b")
        self.assertEqual(r.returncode, 2)
        r = self.cli("agents", str(p), "--set", "nope=x")
        self.assertEqual(r.returncode, 2)


    # --- home / migrate --------------------------------------------------------

    def test_home_creates_ignore_that_tracks_only_backlog(self):
        p = self.t / "proj"
        fin.git_init(p)
        out = fin.make_home(p)
        self.assertEqual(out["tracked"], {".gitignore": False, "backlog.md": False})
        h = p / ".factory"
        (h / "jobs" / "001-a").mkdir(parents=True)
        (h / "jobs" / "001-a" / "job.json").write_text("{}")
        (h / "backlog.md.lock").touch()
        st = git(p, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
        self.assertEqual(sorted(st), ["?? .factory/.gitignore", "?? .factory/backlog.md"])
        git(p, "add", ".factory")
        git(p, "commit", "-q", "-m", "factory")
        self.assertEqual(fin.inspect(p)["factory_home"],
                         {"exists": True, "tracked": {".gitignore": True, "backlog.md": True}})
        with self.assertRaisesRegex(fin.InitError, "not the top of a git repo"):
            fin.make_home(p / ".factory")

    def test_migrate_copies_into_each_repo(self):
        a, b = self.t / "a", self.t / "b"
        fin.git_init(a)
        fin.git_init(b)
        old = self.t / "old"
        for jid, repo in (("001-x", a), ("002-y", b), ("003-z", self.t / "gone")):
            d = old / "jobs" / jid
            d.mkdir(parents=True)
            (d / "job.json").write_text(json.dumps({"id": jid, "repo": str(repo), "stage": "shipped"}))
            (d / "report.md").write_text("r\n")
        (old / "jobs" / ".n-001").mkdir()
        (old / "runs").mkdir()
        rid = "20261017-0536-fb8b"
        (old / "runs" / f"{rid}.json").write_text(json.dumps({"id": rid, "repo": str(a)}))
        (old / "runs" / f"{rid}.refs-before.json").write_text("{}")
        (old / "backlog.md").write_text(
            f"- [x] {a} :: One → 001-x shipped\n- [ ] {b} :: Two wi:3\n- [ ] /nope :: Lost\n")
        dry = fin.migrate(old, dry_run=True)
        self.assertEqual(len(dry["copied"]["jobs"]), 2)
        self.assertFalse((a / ".factory").exists())
        out = json.loads(self.cli("migrate", "--from", str(old)).stdout)
        self.assertEqual((len(out["copied"]["jobs"]), len(out["copied"]["runs"]),
                          len(out["copied"]["backlog"])), (2, 1, 2))
        self.assertEqual(sorted(x["item"] for x in out["skipped"]),
                         ["backlog line '- [ ] /nope :: Lost'", "job 003-z"])
        self.assertEqual((a / ".factory/jobs/001-x/report.md").read_text(), "r\n")
        self.assertTrue((b / ".factory/jobs/.n-002").is_dir())
        self.assertTrue((a / ".factory/runs" / f"{rid}.refs-before.json").exists())
        self.assertEqual((a / ".factory/backlog.md").read_text(), "- [x] One → 001-x shipped\n")
        self.assertEqual((b / ".factory/backlog.md").read_text(), "- [ ] Two wi:3\n")
        self.assertTrue((old / "jobs" / "001-x" / "job.json").exists())  # old left in place
        again = fin.migrate(old)
        self.assertEqual(again["copied"], {"jobs": [], "runs": [], "backlog": []})
        self.assertIn("exists", again["skipped"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
