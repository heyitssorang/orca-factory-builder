import json
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_push as fp  # noqa: E402

CLI = str(ROOT / "scripts" / "factory_push.py")
SECRET = "AKIAABCDEFGHIJKLMNOP"
_REAL_VALIDATE = fp.validate_origin
RUN_ID = "20261016-1200-abcd"


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=check, env=env)


class PushTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        t = Path(self._tmp.name)
        patcher = mock.patch.object(fp, "validate_origin", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.origin = t / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.wt = t / "clone"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.wt)],
                       check=True, capture_output=True)
        git(self.wt, "config", "user.name", "T")
        git(self.wt, "config", "user.email", "t@example.com")
        git(self.wt, "checkout", "-q", "-b", "main")
        (self.wt / "README").write_text("hi\n")
        self.commit("init")
        git(self.wt, "push", "-q", "origin", "main")
        self.base = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        git(self.wt, "checkout", "-q", "-b", "factory/001-a")
        self.url = str(self.origin)
        self.home = t / "home"
        env = mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def commit(self, msg):
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", msg)

    def write(self, name, text):
        (self.wt / name).write_text(text)

    def chk(self, branch="factory/001-a"):
        return fp.check(self.wt, branch=branch, base=self.base, default_branch="main",
                        origin_url=self.url)

    def cli(self, *args):
        """In-process CLI (validate_origin patched); returns (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fp.main(list(args))
        return subprocess.CompletedProcess(args, rc, out.getvalue(), err.getvalue())

    def ship_args(self, sha, base=None, url=None, default="main"):
        return ("ship", "--repo", str(self.wt), "--default", default, "--sha", sha,
                "--base", base or self.base, "--origin-url", url or self.url, "--run", RUN_ID)

    def write_run(self, sha, base=None, default="main", **over):
        """The run file factory_integrate leaves after a passed gate for sha; over replaces
        top-level keys, or ship keys when prefixed ship_."""
        ship = {"merge_sha": sha, "base": base or self.base, "run_tip": sha,
                "gate": {"merge_sha": sha, "status": "pass", "at": "x"}}
        run = {"id": RUN_ID, "repo": str(self.wt), "default_branch": default,
               "origin_url": self.url, "shipped": None, "ship": ship}
        for k, v in over.items():
            (ship if k.startswith("ship_") else run)[k.removeprefix("ship_")] = v
        (self.home / "runs").mkdir(parents=True, exist_ok=True)
        path = self.home / "runs" / f"{RUN_ID}.json"
        path.unlink(missing_ok=True)
        path.write_text(json.dumps(run))
        return path

    def ship_cli(self, sha, record=True, **kw):
        if record:
            self.write_run(sha, kw.get("base"), kw.get("default", "main"))
        return self.cli(*self.ship_args(sha, **kw))

    def advance_main(self, name="a.txt", text="a\n"):
        """Commit on local main (checked out), return the new sha; leaves main checked out."""
        git(self.wt, "checkout", "-q", "main")
        self.write(name, text)
        self.commit(name)
        return git(self.wt, "rev-parse", "HEAD").stdout.strip()

    def test_four_digit_job_branch_allowed(self):
        self.assertTrue(fp.BRANCH_RE.fullmatch("factory/1000-big-job"))
        self.assertFalse(fp.BRANCH_RE.fullmatch("factory/01-a"))

    def test_refuses_main_and_bad_pattern(self):
        for b in ("main", "factory/x", "factory/01-a"):
            git(self.wt, "checkout", "-q", "-B", b)
            r = self.chk(b)
            self.assertTrue(any(x.startswith("rule 1:") for x in r), (b, r))

    def test_refuses_branch_not_current(self):
        r = self.chk("factory/002-b")
        self.assertTrue(any(x.startswith("rule 1:") for x in r))

    def test_refuses_dirty_tree(self):
        self.write("untracked.txt", "x")
        self.assertTrue(any(x.startswith("rule 2:") for x in self.chk()))

    def test_refuses_non_ancestor_base(self):
        self.write("a.txt", "a\n")
        self.commit("a")
        git(self.wt, "checkout", "-q", "--orphan", "factory/003-c")
        git(self.wt, "rm", "-rf", "-q", ".")
        self.write("b.txt", "b\n")
        self.commit("b")
        r = self.chk("factory/003-c")
        self.assertTrue(any(x.startswith("rule 3:") for x in r), r)

    def test_refuses_merge_commit(self):
        self.write("a.txt", "a\n")
        self.commit("a")
        git(self.wt, "checkout", "-q", "-b", "side", self.base)
        self.write("s.txt", "s\n")
        self.commit("s")
        git(self.wt, "checkout", "-q", "factory/001-a")
        git(self.wt, "merge", "-q", "--no-ff", "-m", "m", "side")
        r = self.chk()
        self.assertTrue(any(x.startswith("rule 3:") and "merge" in x for x in r), r)

    def test_refuses_non_fast_forward_remote(self):
        self.write("a.txt", "a\n")
        self.commit("a")
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/factory/001-a")
        git(self.wt, "reset", "-q", "--hard", self.base)
        self.write("b.txt", "b\n")
        self.commit("b")
        r = self.chk()
        self.assertTrue(any(x.startswith("rule 3:") for x in r), r)

    def test_clean_branch_passes(self):
        self.write("a.txt", "a\n")
        self.commit("a")
        self.assertEqual(self.chk(), [])

    def test_secret_added_then_removed(self):
        self.write("c.py", f'k = "{SECRET}"\n')
        self.commit("add")
        self.write("c.py", "k = None\n")
        self.commit("remove")
        v = fp.scan_commits(self.wt, self.base)
        self.assertEqual([x.rule for x in v], ["C2"])
        self.assertEqual(v[0].path, "c.py")
        r = self.chk()
        self.assertTrue(any(x.startswith("rule 4:") for x in r))
        self.assertFalse(any(SECRET in x for x in r))

    def test_binary_pem_added_then_deleted(self):
        (self.wt / "key.pem").write_bytes(bytes(range(256)) * 4)
        self.commit("add")
        git(self.wt, "rm", "-q", "key.pem")
        self.commit("rm")
        self.assertEqual([x.rule for x in fp.scan_commits(self.wt, self.base)], ["P2"])

    def test_env_example_allowed(self):
        self.write(".env.example", "A=1\n")
        self.commit("ex")
        self.assertEqual(fp.scan_commits(self.wt, self.base), [])
        self.write(".env.local", "A=1\n")
        self.commit("local")
        self.assertEqual([x.rule for x in fp.scan_commits(self.wt, self.base)], ["P1"])

    def test_path_rules(self):
        (self.wt / "sub").mkdir()
        for n in ("sub/a.pfx", "b.p12", "id_rsa", "id_rsa.pub", ".env"):
            self.write(n, "x\n")
        self.commit("p")
        self.assertEqual(sorted(x.rule for x in fp.scan_commits(self.wt, self.base)),
                         ["P1", "P3", "P4", "P5", "P5"])

    def test_content_rules(self):
        cases = {
            "C1": "-----BEGIN RSA PRIVATE KEY-----",
            "C3": "x = ghp_" + "a" * 24,
            "C4": 'password = "hunter2hunter2"',
            "C5": "azure " + "A1" * 26,
        }
        for rule, line in cases.items():
            self.write("f.txt", line + "\n")
            self.commit(rule)
        got = [x.rule for x in fp.scan_commits(self.wt, self.base)]
        self.assertEqual(sorted(got), sorted(cases))
        # no false positives
        git(self.wt, "reset", "-q", "--hard", self.base)
        self.write("g.txt", 'password = "short"\n' + "A1" * 26 + "\n")
        self.commit("ok")
        self.assertEqual(fp.scan_commits(self.wt, self.base), [])

    def test_line_starting_plus_plus_is_scanned(self):
        self.write("h.txt", f"++ {SECRET}\n")
        self.commit("pp")
        self.assertEqual([x.rule for x in fp.scan_commits(self.wt, self.base)], ["C2"])

    def test_size_limits(self):
        self.write("big.txt", "x\n" * 5000)
        self.commit("big")
        self.assertTrue(any(x.startswith("rule 5:") for x in self.chk()))
        git(self.wt, "reset", "-q", "--hard", self.base)
        for i in range(200):
            self.write(f"f{i}.txt", "x\n")
        self.commit("many")
        self.assertTrue(any(x.startswith("rule 5:") for x in self.chk()))

    def test_gitattributes_nodiff_does_not_hide_secret(self):
        self.write(".gitattributes", "* -diff\n")
        self.write("c.py", f'k = "{SECRET}"\n')
        self.commit("add")
        self.assertEqual([x.rule for x in fp.scan_commits(self.wt, self.base)], ["C2"])

    def test_bigfilethreshold_does_not_hide_secret(self):
        git(self.wt, "config", "core.bigFileThreshold", "1")
        self.write("c.py", f'k = "{SECRET}"\n')
        self.commit("add")
        self.assertEqual([x.rule for x in fp.scan_commits(self.wt, self.base)], ["C2"])

    def test_nul_file_with_secret(self):
        (self.wt / "b.bin").write_bytes(b"\0\0\1" + SECRET.encode() + b"\n\0")
        self.commit("bin")
        self.assertEqual([x.rule for x in fp.scan_commits(self.wt, self.base)], ["C2"])

    def test_quoted_path_names_still_match(self):
        (self.wt / "d\tx").mkdir()
        (self.wt / "d\tx" / "server.pem").write_text("x\n")
        (self.wt / 'q"d').mkdir()
        (self.wt / 'q"d' / "id_rsa").write_text("x\n")
        self.commit("q")
        v = fp.scan_commits(self.wt, self.base)
        self.assertEqual(sorted(x.rule for x in v), ["P2", "P5"])

    def test_replace_ref_cannot_hide_secret(self):
        self.write("c.py", f'k = "{SECRET}"\n')
        self.commit("secret")
        bad = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        git(self.wt, "checkout", "-q", "-b", "side", self.base)
        self.write("c.py", "k = None\n")
        self.commit("clean")
        clean = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        git(self.wt, "checkout", "-q", "factory/001-a")
        git(self.wt, "replace", bad, clean)
        git(self.wt, "reset", "-q", "--hard")
        self.assertEqual(git(self.wt, "status", "--porcelain").stdout, "")
        r = self.chk()
        self.assertTrue(any(x.startswith("rule 4: C2 c.py") for x in r), r)
        git(self.wt, "branch", "-f", "main", bad)
        out = self.ship_cli(bad)
        self.assertEqual(out.returncode, 2)
        self.assertNotEqual(git(self.origin, "cat-file", "-e", bad, check=False).returncode, 0)

    def test_grafts_file_refused(self):
        self.write("a.txt", "a\n")
        self.commit("a")
        gd = Path(git(self.wt, "rev-parse", "--absolute-git-dir").stdout.strip())
        (gd / "info").mkdir(exist_ok=True)
        (gd / "info" / "grafts").write_text("")
        self.assertIn("rule 6: grafts file present", self.chk())

    def test_commit_marker_after_hunkless_file(self):
        self.write("empty.txt", "")
        self.commit("empty")
        self.write("c.py", f'k = "{SECRET}"\n')
        self.commit("secret")
        want = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        v = fp.scan_commits(self.wt, self.base)
        self.assertEqual([(x.rule, x.commit) for x in v], [("C2", want)])

    def test_rule1_trailing_newline(self):
        r = fp.check(self.wt, branch="factory/001-a\n", base=self.base,
                     default_branch="main", origin_url=self.url)
        self.assertTrue(any(x.startswith("rule 1:") for x in r))

    def test_validate_origin(self):
        orgs = {"example-org"}
        with mock.patch.object(fp, "validate_origin", _REAL_VALIDATE):
            ok = ("https://dev.azure.com/example-org/p/_git/r",
                  "https://EXAMPLE-ORG@dev.azure.com/Example-Org/p/_git/r",
                  "https://example-org.visualstudio.com/p/_git/r")
            bad = ("http://dev.azure.com/example-org/p/_git/r",
                   "https://dev.azure.com/other/p/_git/r",
                   "https://evil.example/example-org/p/_git/r",
                   "https://dev.azure.com.evil.example/example-org/p",
                   "https://other.visualstudio.com/p/_git/r",
                   "https://a.example-org.visualstudio.com/p",
                   "/tmp/some/bare.git", "file:///tmp/x.git",
                   "git@ssh.dev.azure.com:v3/example-org/p/r",
                   "ssh://git@ssh.dev.azure.com/v3/example-org/p/r",
                   "https://dev.azure.com/example-org/../other/p/_git/r",
                   "https://dev.azure.com/example-org/%2e%2e/other/_git/r",
                   "https://dev.azure.com/example-org/./p",
                   "https://dev.azure.com/example-org/p r", "")
            for u in ok:
                self.assertTrue(fp.validate_origin(u, orgs), u)
            for u in bad:
                self.assertFalse(fp.validate_origin(u, orgs), u)

    def test_unvalidated_origin_refused_via_real_cli(self):
        out = subprocess.run([sys.executable, CLI, *self.ship_args(self.base)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("rule 6: origin url not allowed", out.stderr)

    def test_origin_url_must_match_config(self):
        r = fp.check(self.wt, branch="factory/001-a", base=self.base, default_branch="main",
                     origin_url=self.url + "x")
        self.assertTrue(any(x.startswith("rule 6:") for x in r), r)

    def test_worktree_config_insteadof_and_credential_refused(self):
        fp.disable_push(self.wt, self.wt)
        self.assertEqual(self.chk(), [])  # the disabled pushurl is allowed
        git(self.wt, "config", "--worktree", "url.https://evil/.insteadOf", self.url)
        r = self.chk()
        self.assertTrue(any(x.startswith("rule 6: config url.") for x in r), r)
        git(self.wt, "config", "--worktree", "--unset-all", "url.https://evil/.insteadOf")
        git(self.wt, "config", "credential.helper", "!echo x")
        r = self.chk()
        self.assertIn("rule 6: config credential.helper", r)

    def test_main_checkout_without_config_worktree_after_disable(self):
        # disable on a linked worktree turns on extensions.worktreeConfig in the shared config;
        # the main checkout then has no config.worktree file, which is empty, not unreadable.
        linked = Path(self._tmp.name) / "linked"
        git(self.wt, "worktree", "add", "-q", "-b", "factory/002-b", str(linked), self.base)
        fp.disable_push(self.wt, linked)
        self.assertEqual(fp.config_violations(self.wt, self.url), [])
        self.assertEqual(fp.config_violations(linked, self.url), [])
        gd = Path(git(self.wt, "rev-parse", "--absolute-git-dir").stdout.strip())
        (gd / "config.worktree").mkdir()  # present but unreadable: still refused
        self.assertEqual(fp.config_violations(self.wt, self.url), ["rule 6: config unreadable"])

    def test_orca_push_autosetupremote_allowed(self):
        git(self.wt, "config", "push.autoSetupRemote", "true")  # Orca's worktree create sets it
        self.assertEqual(fp.config_violations(self.wt, self.url), [])
        git(self.wt, "config", "push.default", "matching")
        self.assertEqual(fp.config_violations(self.wt, self.url), ["rule 6: config push.default"])

    def test_dangerous_config_refused_before_status_runs(self):
        marker = Path(self._tmp.name) / "ran"
        git(self.wt, "config", "core.fsmonitor", f"touch {marker}")
        git(self.wt, "config", "core.hooksPath", "/x")
        git(self.wt, "config", "core.sshCommand", "x")
        r = self.chk()
        self.assertEqual(sorted(r), ["rule 6: config core.fsmonitor", "rule 6: config core.hookspath",
                                     "rule 6: config core.sshcommand"])
        self.assertFalse(marker.exists())

    def test_disable_push_blocks_plain_push(self):
        lw = Path(self._tmp.name) / "linked"
        git(self.wt, "worktree", "add", "-q", "-b", "factory/009-z", str(lw))
        fp.disable_push(self.wt, lw)
        (lw / "z.txt").write_text("z\n")
        git(lw, "add", "-A")
        git(lw, "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-q", "-m", "z")
        self.assertNotEqual(git(lw, "push", "origin", "HEAD", check=False).returncode, 0)
        ok = git(lw, "push", str(self.origin), "HEAD:refs/heads/factory/009-z", check=False)
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_compare_without_branch(self):
        a, b = Path(self._tmp.name) / "a.json", Path(self._tmp.name) / "b.json"
        a.write_text(json.dumps({"refs/heads/main": "1"}))
        b.write_text(json.dumps({"refs/heads/main": "1", "refs/heads/factory/001-a": "2"}))
        r = self.cli("compare", "--before", str(a), "--after", str(b), "--repo", str(self.wt))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(json.loads(r.stdout),
                         {"foreign": ["refs/heads/factory/001-a"], "suspicious": []})
        sha = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        b.write_text(json.dumps({"refs/heads/main": "1", "refs/heads/factory/001-a": sha}))
        r = self.cli("compare", "--before", str(a), "--after", str(b), "--repo", str(self.wt))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)["suspicious"], ["refs/heads/factory/001-a"])

    def test_push_cli_removed(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fp.main(["push", "--worktree", str(self.wt), "--branch", "factory/001-a",
                     "--base", self.base, "--default", "main", "--origin-url", self.url])
        self.assertFalse(hasattr(fp, "push_argv"))

    def test_ship_argv_safe(self):
        argv = fp.ship_argv("https://x", "main", "a" * 40)
        self.assertEqual(argv, ["git", "push", "--porcelain", "--no-follow-tags",
                                "--recurse-submodules=no", "https://x", "a" * 40 + ":refs/heads/main"])
        for bad in ("-f", "--force", "--force-with-lease", "--tags", "--all", "--mirror",
                    "--delete", "--prune", "--follow-tags", "--recurse-submodules=on-demand"):
            self.assertNotIn(bad, argv)

    def test_ship_pushes_fast_forward(self):
        sha = self.advance_main()
        spy_calls = []
        real = subprocess.run

        def spy(cmd, *a, **k):
            if "push" in cmd:
                spy_calls.append(cmd)
            return real(cmd, *a, **k)
        with mock.patch.object(fp.subprocess, "run", spy):
            out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(spy_calls[0][-1], f"{sha}:refs/heads/main")
        self.assertIn("refs/heads/main", out.stdout)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), sha)

    def test_ship_needs_gate_record_of_the_run(self):
        sha = self.advance_main()
        other = git(self.wt, "rev-parse", "HEAD~1").stdout.strip()
        cases = [
            ({}, "ship: cannot read run file"),  # no run file at all
            ({"ship_gate": None}, "ship: gate has not passed for --sha"),
            ({"ship_gate": {"merge_sha": other, "status": "pass"}},
             "ship: gate has not passed for --sha"),
            ({"ship_gate": {"merge_sha": sha, "status": "fail"}},
             "ship: gate has not passed for --sha"),
            ({"shipped": {"merge_sha": sha, "pushed": False, "at": "x"}},
             "ship: run already shipped"),
            ({"ship_merge_sha": other}, "ship: run's ship merge is not --sha"),
            ({"ship_base": sha}, "ship: run's ship base is not --base"),
            ({"default_branch": "dev"}, "ship: run's default branch is not --default"),
            ({"repo": str(self.origin)}, "ship: run's repo is not --repo"),
            ({"origin_url": "/elsewhere.git"}, "ship: run's origin url is not --origin-url"),
            ({"ship": None}, "ship: gate has not passed for --sha"),
        ]
        for i, (over, want) in enumerate(cases):
            if i:
                self.write_run(sha, **over)
            out = self.ship_cli(sha, record=False)
            self.assertEqual(out.returncode, 2, (over, out.stderr))
            self.assertIn(want, out.stderr.splitlines(), over)
            self.assertTrue(all(x.startswith("ship:") for x in out.stderr.splitlines()), over)
            self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(),
                             self.base)

    def test_ship_run_file_read_safely(self):
        sha = self.advance_main()
        out = self.cli(*self.ship_args(sha)[:-1], "../x")
        self.assertEqual((out.returncode, out.stderr), (2, "ship: invalid run id\n"))
        real = self.write_run(sha)
        link = real.with_name("20261016-1200-beef.json")
        link.symlink_to(real)
        out = self.cli(*self.ship_args(sha)[:-1], "20261016-1200-beef")
        self.assertEqual((out.returncode, out.stderr), (2, "ship: cannot read run file\n"))
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(),
                         self.base)

    def test_ship_refuses_non_fast_forward(self):
        sha = self.advance_main()
        other = Path(self._tmp.name) / "other"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(other)], check=True,
                       capture_output=True)
        (other / "o.txt").write_text("o\n")
        git(other, "add", "-A")
        git(other, "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-q", "-m", "o")
        git(other, "push", "-q", "origin", "HEAD:main")
        moved = git(other, "rev-parse", "HEAD").stdout.strip()
        git(self.wt, "fetch", "-q", "origin")
        out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 2)
        self.assertIn("is not an ancestor", out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), moved)

    def test_ship_refuses_base_not_origin_tip(self):
        sha = self.advance_main()
        out = self.ship_cli(sha, base=sha)
        self.assertEqual(out.returncode, 2)
        self.assertIn("ship: base is not origin/main", out.stderr)
        r = fp.ship_check(self.wt, default_branch="main", sha=sha, base="-x", origin_url=self.url)
        self.assertEqual(r, ["ship: invalid sha, base or default branch"])

    def test_ship_refuses_local_main_mismatch(self):
        sha = self.advance_main()
        self.write("b.txt", "b\n")
        self.commit("b")
        out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 2)
        self.assertIn("ship: local main is not", out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), self.base)

    def test_ship_refuses_non_commit_sha(self):
        out = self.ship_cli("a" * 40)
        self.assertEqual(out.returncode, 2)
        self.assertIn("ship: sha is not a commit", out.stderr)
        out = self.ship_cli("HEAD")
        self.assertEqual(out.returncode, 2)

    def test_ship_refuses_secret(self):
        sha = self.advance_main("c.py", f'k = "{SECRET}"\n')
        out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 2)
        self.assertIn("rule 4:", out.stderr)
        self.assertNotIn(SECRET, out.stdout + out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), self.base)

    def test_ship_refuses_secret_in_merge_only(self):
        git(self.wt, "checkout", "-q", "main")
        self.write("a.txt", "a\n")
        self.commit("a")
        x = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        side = git(self.wt, "rev-parse", self.base).stdout.strip()
        git(self.wt, "checkout", "-q", "--detach", x)
        self.write("evil.py", f'k = "{SECRET}"\n')
        git(self.wt, "add", "-A")
        tree = git(self.wt, "write-tree").stdout.strip()
        m = git(self.wt, "-c", "user.name=T", "-c", "user.email=t@e.com", "commit-tree", tree,
                "-p", x, "-p", side, "-m", "merge").stdout.strip()
        git(self.wt, "checkout", "-q", "main")
        git(self.wt, "reset", "-q", "--hard", m)
        out = self.ship_cli(m)
        self.assertEqual(out.returncode, 2)
        self.assertIn("rule 4: C2 evil.py", out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), self.base)

    def test_ship_ignores_forged_remote_tracking_ref(self):
        sha = self.advance_main("c.py", f'k = "{SECRET}"\n')
        self.write("d.txt", "d\n")
        self.commit("d")
        sha = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        git(self.wt, "update-ref", "refs/remotes/origin/main", sha)
        out = self.ship_cli(sha, base=sha)
        self.assertEqual(out.returncode, 2)
        out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 2)
        self.assertIn("rule 4:", out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), self.base)

    def test_ship_refuses_non_default_branch(self):
        git(self.wt, "push", "-q", "origin", "main:refs/heads/other")
        git(self.wt, "checkout", "-q", "-B", "other", "main")
        self.write("a.txt", "a\n")
        self.commit("a")
        sha = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        out = self.ship_cli(sha, default="other")
        self.assertEqual(out.returncode, 2)
        self.assertIn("ship: other is not origin's default branch", out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/other").stdout.strip(), self.base)

    def test_ship_refuses_bad_config(self):
        sha = self.advance_main()
        git(self.wt, "config", "credential.helper", "!echo x")
        out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 2)
        self.assertIn("rule 6: config credential.helper", out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), self.base)

    def test_ship_reports_rejected_push(self):
        sha = self.advance_main()
        (self.origin / "refs" / "heads" / "main.lock").write_text("")  # server cannot update main
        out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 1)
        self.assertIn("push rejected", out.stderr)

    BAD_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "url./nonexistent/x.git.insteadOf"}

    def test_remote_calls_ignore_injected_git_env(self):
        bad = dict(self.BAD_ENV, GIT_CONFIG_VALUE_0=self.url, GIT_DIR="/nonexistent",
                   GIT_WORK_TREE="/nonexistent")
        with mock.patch.dict(os.environ, bad):
            self.assertEqual(fp.remote_refs(self.wt, self.url)["refs/heads/main"], self.base)
            self.assertEqual(fp.server_head(self.url, "main"), ("refs/heads/main", self.base))

    def test_ship_push_ignores_injected_git_config_env(self):
        sha = self.advance_main()
        with mock.patch.dict(os.environ, dict(self.BAD_ENV, GIT_CONFIG_VALUE_0=self.url)):
            out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), sha)

    def rewrite_env(self, key):
        """A global config (GIT_CONFIG_GLOBAL, as a worker could write ~/.gitconfig) whose
        url.<other>.<key> rewrites the origin URL to another bare repo."""
        t = Path(self._tmp.name)
        other = t / f"other-{key}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
        cfg = t / f"gitconfig-{key}"
        cfg.write_text(f'[url "{other}"]\n\t{key} = {self.url}\n')
        return other, mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg)})

    def test_url_rewrites(self):
        self.assertEqual(fp.url_rewrites(self.wt, self.url), [])
        for key, kinds in (("insteadOf", ["fetch", "push"]), ("pushInsteadOf", ["push"])):
            _, env = self.rewrite_env(key)
            with env:
                self.assertEqual(fp.url_rewrites(self.wt, self.url), kinds)

    def test_ship_refuses_rewritten_origin(self):
        sha = self.advance_main()
        for key, kind in (("insteadOf", "fetch"), ("pushInsteadOf", "push")):
            other, env = self.rewrite_env(key)
            with env:
                out = self.ship_cli(sha)
            self.assertEqual(out.returncode, 2, out.stderr)
            self.assertIn(f"rule 6: origin url is rewritten by config ({kind})", out.stderr)
            self.assertEqual(git(other, "for-each-ref").stdout, "")
            self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(),
                             self.base)

    def test_ship_refuses_url_named_remote(self):
        """git takes a URL argument as a remote name too: remote."<url>".url/pushurl in any
        scope would redirect the push."""
        sha = self.advance_main()
        t = Path(self._tmp.name)
        for scope in ("local", "global"):
            for var in ("url", "pushurl"):
                other = t / f"named-{scope}-{var}.git"
                subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
                key = f"remote.{self.url}.{var}"
                if scope == "local":
                    git(self.wt, "config", key, str(other))
                    env = contextlib.nullcontext()
                    want = f"rule 6: config {key.lower()}"
                else:
                    cfg = t / f"gitconfig-named-{var}"
                    cfg.write_text(f'[remote "{self.url}"]\n\t{var} = {other}\n')
                    env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg)})
                    want = "rule 6: origin url is rewritten by config (remote)"
                with env:
                    out = self.ship_cli(sha)
                    if scope == "global":
                        self.assertIn("remote", fp.url_rewrites(self.wt, self.url))
                self.assertEqual(out.returncode, 2, (scope, var, out.stderr))
                self.assertIn(want, out.stderr.splitlines(), (scope, var))
                self.assertEqual(git(other, "for-each-ref").stdout, "")
                self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(),
                                 self.base)
                if scope == "local":
                    git(self.wt, "config", "--unset", key)

    def test_config_allows_other_remotes_refuses_url_named_one(self):
        upstream = Path(self._tmp.name) / "upstream.git"
        subprocess.run(["git", "init", "-q", "--bare", str(upstream)], check=True)
        git(self.wt, "config", "remote.upstream.url", str(upstream))
        git(self.wt, "config", "remote.upstream.pushurl", str(upstream))
        git(self.wt, "config", "remote.pushDefault", "upstream")
        self.assertEqual(fp.config_check(self.wt, self.url), [])
        sha = self.advance_main()
        out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), sha)
        self.assertEqual(git(upstream, "for-each-ref").stdout, "")
        git(self.wt, "config", f"remote.{self.url}.url", str(upstream))
        self.assertIn(f"rule 6: config remote.{self.url}.url".lower(),
                      [r.lower() for r in fp.config_check(self.wt, self.url)])

    def _timeout_on(self, word, expected):
        real = subprocess.run

        def slow(cmd, *a, **k):
            if word in cmd:
                self.assertEqual(k.get("timeout"), expected)
                raise subprocess.TimeoutExpired(cmd, k["timeout"])
            return real(cmd, *a, **k)
        return mock.patch.object(fp.subprocess, "run", slow)

    def test_remote_timeouts_are_clean(self):
        with self._timeout_on("ls-remote", fp.REMOTE_TIMEOUT):
            with self.assertRaisesRegex(fp.GitError, "timed out"):
                fp.remote_refs(self.wt, self.url)
            with self.assertRaisesRegex(fp.GitError, "timed out"):
                fp.server_head(self.url, "main")
            self.assertIn("rule 3: cannot read remote branch", self.chk())
        sha = self.advance_main()
        with self._timeout_on("push", fp.PUSH_TIMEOUT):
            out = self.ship_cli(sha)
        self.assertEqual(out.returncode, 1)
        self.assertIn("push timed out", out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(), self.base)

    def test_unexpected_changes(self):
        before = {"refs/heads/main": "1", "refs/heads/factory/001-a": "2"}
        after = {"refs/heads/main": "1", "refs/heads/factory/001-a": "3"}
        self.assertEqual(fp.unexpected_changes(before, after, "factory/001-a"), [])
        after["refs/tags/v1"] = "4"
        after["refs/heads/other"] = "5"
        del after["refs/heads/main"]
        self.assertEqual(fp.unexpected_changes(before, after, "factory/001-a"),
                         ["refs/heads/main", "refs/heads/other", "refs/tags/v1"])

    def test_unexpected_changes_several_branches(self):
        before = {"refs/heads/factory/001-a": "1", "refs/heads/main": "1"}
        after = {"refs/heads/factory/001-a": "2", "refs/heads/factory/002-b": "3",
                 "refs/heads/factory/003-c": "4", "refs/heads/main": "1"}
        self.assertEqual(fp.unexpected_changes(before, after, ["factory/001-a", "factory/002-b"]),
                         ["refs/heads/factory/003-c"])

    def test_compare_repeatable_branch_cli(self):
        a, b = Path(self._tmp.name) / "a.json", Path(self._tmp.name) / "b.json"
        a.write_text(json.dumps({"refs/heads/main": "1"}))
        b.write_text(json.dumps({"refs/heads/main": "1", "refs/heads/factory/001-a": "2",
                                 "refs/heads/factory/002-b": "3"}))
        # "3" is not a commit in the local store: foreign, listed, not a failure
        r = self.cli("compare", "--before", str(a), "--after", str(b),
                     "--branch", "factory/001-a", "--repo", str(self.wt))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout),
                         {"foreign": ["refs/heads/factory/002-b"], "suspicious": []})
        r = self.cli("compare", "--before", str(a), "--after", str(b),
                     "--branch", "factory/001-a", "--branch", "factory/002-b",
                     "--repo", str(self.wt))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(json.loads(r.stdout), {"foreign": [], "suspicious": []})

    def test_config_check_cli(self):
        fp.disable_push(self.wt, self.wt)
        r = self.cli("config-check", "--worktree", str(self.wt), "--origin-url", self.url)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        marker = Path(self._tmp.name) / "ran"
        git(self.wt, "config", "core.fsmonitor", f"touch {marker}")
        r = self.cli("config-check", "--worktree", str(self.wt), "--origin-url", self.url)
        self.assertEqual(r.returncode, 2)
        self.assertIn("rule 6: config core.fsmonitor", r.stderr)
        self.assertFalse(marker.exists())
        git(self.wt, "config", "--unset", "core.fsmonitor")
        r = self.cli("config-check", "--worktree", str(self.wt), "--origin-url", self.url + "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("rule 6: remote.origin.url differs", r.stderr)

    def test_config_check_rejects_untrusted_origin_via_real_cli(self):
        out = subprocess.run([sys.executable, CLI, "config-check", "--worktree", str(self.wt),
                              "--origin-url", self.url], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("rule 6: origin url not allowed", out.stderr)

    def test_refs_and_compare_cli(self):
        a, b = Path(self._tmp.name) / "a.txt", Path(self._tmp.name) / "b.txt"
        u = ("--origin-url", self.url)
        self.assertEqual(self.cli("refs", "--repo", str(self.wt), "--out", str(a), *u).returncode, 0)
        git(self.wt, "tag", "v1")
        git(self.wt, "push", "-q", "origin", "v1")
        self.cli("refs", "--repo", str(self.wt), "--out", str(b), *u)
        # the tag points at a commit made here: suspicious
        r = self.cli("compare", "--before", str(a), "--after", str(b), "--branch", "factory/001-a",
                     "--repo", str(self.wt))
        self.assertEqual(r.returncode, 2)
        self.assertIn("refs/tags/v1", r.stderr)
        self.assertEqual(json.loads(r.stdout)["suspicious"], ["refs/tags/v1"])
        r = self.cli("compare", "--before", str(a), "--after", str(a), "--branch", "factory/001-a",
                     "--repo", str(self.wt))
        self.assertEqual(r.returncode, 0)


    # --- C1: bare CR must not hide a secret -------------------------------------------
    def test_mid_line_cr_secret_is_scanned(self):
        (self.wt / "m.py").write_bytes(b'x = 1\rpassword = "supersecretvalue1"\n')
        (self.wt / "k.txt").write_bytes(b"k\r" + SECRET.encode() + b"\n")
        self.commit("cr")
        self.assertEqual(sorted(x.rule for x in fp.scan_commits(self.wt, self.base)),
                         ["C2", "C4"])

    def test_cr_only_file_secret_on_third_line(self):
        (self.wt / "cr.txt").write_bytes(b'a = 1\rb = 2\rpassword = "supersecretvalue1"\r')
        self.commit("cr-only")
        v = fp.scan_commits(self.wt, self.base)
        self.assertEqual([(x.rule, x.path) for x in v], [("C4", "cr.txt")])

    # --- I2: C5 is linear ---------------------------------------------------------------
    def test_c5_long_line_is_fast_and_still_matches(self):
        import time
        rx = dict(fp.CONTENT_RULES)["C5"]
        line = ("a" * 51 + " ") * 20165 + "token"
        self.assertGreaterEqual(len(line), 1 << 20)
        t = time.perf_counter()
        self.assertIsNone(fp._content_hit(line))
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertTrue(rx.search("x = 1; azure " + "A1" * 26))
        self.assertTrue(rx.search("A1" * 26 + " my PAT"))
        self.assertFalse(rx.search("A1" * 25 + " my PAT"))

    # --- L3: path rules are case-insensitive --------------------------------------------
    def test_path_rules_case_insensitive(self):
        for n in (".ENV", "KEY.PEM", "ID_RSA", "Cert.PFX", "a.P12", ".Env.Local"):
            self.assertIsNotNone(fp._path_hit(n), n)
        for n in (".env.example", ".ENV.EXAMPLE", ".Env.Example", "readme.md"):
            self.assertIsNone(fp._path_hit(n), n)
        self.write(".ENV", "x\n")
        self.write("KEY.PEM", "x\n")
        self.write(".ENV.EXAMPLE", "x\n")
        self.commit("upper")
        self.assertEqual(sorted(x.rule for x in fp.scan_commits(self.wt, self.base)),
                         ["P1", "P2"])

    def test_path_rule_with_control_char_in_name(self):
        self.assertEqual(fp._path_hit("id_rsa\rx"), "P5")
        self.assertEqual(fp._path_hit("a\nb.pem"), "P2")

    # --- I1: backstop classes ------------------------------------------------------------
    def _sha(self):
        return git(self.wt, "rev-parse", "HEAD").stdout.strip()

    def test_classify_pull_refs_ignored(self):
        before = {"refs/pull/7/merge": "a" * 40}
        after = {"refs/pull/7/merge": "b" * 40, "refs/pull/8/head": self.base}
        self.assertEqual(fp.unexpected_changes(before, after, "factory/001-a"), [])
        res = fp.classify_changes(before, after, "factory/001-a",
                                  has_commit=lambda s: fp.has_local_commit(self.wt, s))
        self.assertEqual(res, {"suspicious": [], "foreign": []})

    def test_classify_local_commit_is_suspicious(self):
        self.write("a.txt", "a\n")
        self.commit("a")
        local = self._sha()
        before = {"refs/heads/main": self.base}
        after = {"refs/heads/main": self.base, "refs/heads/sneaky": local}
        res = fp.classify_changes(before, after, "factory/001-a",
                                  has_commit=lambda s: fp.has_local_commit(self.wt, s))
        self.assertEqual(res, {"suspicious": ["refs/heads/sneaky"], "foreign": []})

    def test_classify_unknown_commit_and_deletion_are_foreign(self):
        before = {"refs/heads/main": self.base, "refs/heads/old": self.base}
        after = {"refs/heads/main": self.base, "refs/heads/teammate": "c" * 40}
        res = fp.classify_changes(before, after, "factory/001-a",
                                  has_commit=lambda s: fp.has_local_commit(self.wt, s))
        self.assertEqual(res, {"suspicious": [],
                               "foreign": ["refs/heads/old", "refs/heads/teammate"]})

    def test_classify_reachable_from_fetched_default_is_foreign(self):
        self.write("a.txt", "a\n")
        self.commit("a")
        moved = self._sha()
        before = {"refs/heads/main": self.base}
        after = {"refs/heads/main": moved, "refs/heads/teammate": moved}
        has = lambda s: fp.has_local_commit(self.wt, s)  # noqa: E731
        fetched = lambda s: fp.reachable_from(self.wt, s, moved)  # noqa: E731
        self.assertEqual(fp.classify_changes(before, after, "factory/001-a", has_commit=has,
                                             is_fetched=fetched),
                         {"suspicious": [], "foreign": ["refs/heads/main",
                                                        "refs/heads/teammate"]})
        old = lambda s: fp.reachable_from(self.wt, s, self.base)  # noqa: E731
        self.assertEqual(fp.classify_changes(before, after, "factory/001-a", has_commit=has,
                                             is_fetched=old),
                         {"suspicious": ["refs/heads/main", "refs/heads/teammate"],
                          "foreign": []})

    def test_reachable_from_rejects_non_sha(self):
        self.assertTrue(fp.reachable_from(self.wt, self.base, self.base))
        for bad in ("HEAD", "--all", "", None):
            self.assertFalse(fp.reachable_from(self.wt, bad, self.base), bad)

    def _teammate(self):
        """A second clone that branches and tags from main and pushes, like a colleague."""
        other = Path(self._tmp.name) / "teammate"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(other)],
                       check=True, capture_output=True)
        git(other, "config", "user.name", "U")
        git(other, "config", "user.email", "u@example.com")
        return other

    def test_remote_refs_drops_peeled_tags(self):
        other = self._teammate()
        git(other, "tag", "-a", "v1", "-m", "release", "main")
        git(other, "push", "-q", "origin", "v1")
        refs = fp.remote_refs(self.wt, self.url)
        self.assertIn("refs/tags/v1", refs)
        self.assertFalse([r for r in refs if r.endswith("^{}")], refs)

    def test_compare_teammate_branch_and_tags_from_main_are_foreign(self):
        a, b = Path(self._tmp.name) / "a.json", Path(self._tmp.name) / "b.json"
        u = ("--origin-url", self.url)
        self.assertEqual(self.cli("refs", "--repo", str(self.wt), "--out", str(a), *u).returncode, 0)
        other = self._teammate()
        git(other, "push", "-q", "origin", "main:refs/heads/teammate-feature")
        git(other, "tag", "-a", "v1", "-m", "release", "main")
        git(other, "tag", "light", "main")
        git(other, "push", "-q", "origin", "v1", "light")
        self.cli("refs", "--repo", str(self.wt), "--out", str(b), *u)
        r = self.cli("compare", "--before", str(a), "--after", str(b), "--branch",
                     "factory/001-a", "--repo", str(self.wt), "--default", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout),
                         {"foreign": ["refs/heads/teammate-feature", "refs/tags/light",
                                      "refs/tags/v1"], "suspicious": []})

    def test_compare_local_work_not_on_default_stays_suspicious(self):
        a, b = Path(self._tmp.name) / "a.json", Path(self._tmp.name) / "b.json"
        u = ("--origin-url", self.url)
        self.cli("refs", "--repo", str(self.wt), "--out", str(a), *u)
        self.write("w.txt", "worker\n")
        self.commit("worker")
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/sneaky")
        self.cli("refs", "--repo", str(self.wt), "--out", str(b), *u)
        r = self.cli("compare", "--before", str(a), "--after", str(b), "--branch",
                     "factory/001-a", "--repo", str(self.wt), "--default", "main")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)["suspicious"], ["refs/heads/sneaky"])

    def test_has_local_commit_rejects_non_sha(self):
        self.assertTrue(fp.has_local_commit(self.wt, self.base))
        for bad in ("HEAD", "--all", self.base[:12], "", None, "0" * 40):
            self.assertFalse(fp.has_local_commit(self.wt, bad), bad)

    def test_compare_cli_default_fetched(self):
        self.write("a.txt", "a\n")
        self.commit("a")
        moved = self._sha()
        git(self.wt, "update-ref", "refs/remotes/origin/main", moved)
        a, b = Path(self._tmp.name) / "a.json", Path(self._tmp.name) / "b.json"
        a.write_text(json.dumps({"refs/heads/main": self.base}))
        b.write_text(json.dumps({"refs/heads/main": moved, "refs/pull/1/merge": moved}))
        r = self.cli("compare", "--before", str(a), "--after", str(b), "--branch",
                     "factory/001-a", "--repo", str(self.wt), "--default", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"foreign": ["refs/heads/main"], "suspicious": []})
        r = self.cli("compare", "--before", str(a), "--after", str(b), "--branch",
                     "factory/001-a", "--repo", str(self.wt))
        self.assertEqual(r.returncode, 2)
        self.assertIn("suspicious change: refs/heads/main", r.stderr)


    def test_forged_remote_tracking_ref_does_not_hide_worker_push(self):
        a, b = Path(self._tmp.name) / "a.json", Path(self._tmp.name) / "b.json"
        u = ("--origin-url", self.url)
        self.cli("refs", "--repo", str(self.wt), "--out", str(a), *u)
        self.write("w.txt", "worker\n")
        self.commit("worker")
        x = self._sha()
        git(self.wt, "push", "-q", "origin", "HEAD:refs/heads/sneaky")
        git(self.wt, "tag", "-a", "wtag", "-m", "w", x)
        git(self.wt, "push", "-q", "origin", "wtag")
        git(self.wt, "update-ref", "refs/remotes/origin/main", x)  # forged locally by a worker
        self.cli("refs", "--repo", str(self.wt), "--out", str(b), *u)
        r = self.cli("compare", "--before", str(a), "--after", str(b), "--branch",
                     "factory/001-a", "--repo", str(self.wt), "--default", "main")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(json.loads(r.stdout)["suspicious"],
                         ["refs/heads/sneaky", "refs/tags/wtag"])


    # --- local mode -------------------------------------------------------------------
    def test_ship_refuses_local_run(self):
        sha = self.advance_main()
        self.write_run(sha, mode="local")
        out = self.cli(*self.ship_args(sha))
        self.assertEqual(out.returncode, 2)
        self.assertIn("ship: run is local; nothing is pushed", out.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/main").stdout.strip(),
                         self.base)

    def test_config_check_local_accepts_untrusted_recorded_origin(self):
        with mock.patch.object(fp, "validate_origin", _REAL_VALIDATE):
            self.assertEqual(fp.config_check(self.wt, self.url),
                             ["rule 6: origin url not allowed"])
            self.assertEqual(fp.config_check(self.wt, self.url, local=True), [])
            self.assertIn("rule 6: remote.origin.url differs from recorded origin url",
                          fp.config_check(self.wt, self.url + "x", local=True))
            git(self.wt, "config", "core.fsmonitor", "true")
            self.assertIn("rule 6: config core.fsmonitor",
                          fp.config_check(self.wt, self.url, local=True))

    def test_config_check_local_without_origin(self):
        self.assertIn("rule 6: remote.origin.url set but the run recorded no origin",
                      fp.config_check(self.wt, None, local=True))
        git(self.wt, "remote", "remove", "origin")
        fp.disable_push(self.wt, self.wt)
        self.assertEqual(fp.config_check(self.wt, None, local=True), [])
        git(self.wt, "config", "remote.origin.url", "https://example.com/x.git")
        self.assertIn("rule 6: remote.origin.url set but the run recorded no origin",
                      fp.config_check(self.wt, None, local=True))

    def test_config_check_cli_local_and_missing_origin(self):
        git(self.wt, "remote", "remove", "origin")
        r = self.cli("config-check", "--worktree", str(self.wt), "--local")
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        r = self.cli("config-check", "--worktree", str(self.wt))
        self.assertEqual(r.returncode, 2)
        self.assertIn("rule 6: origin url not allowed", r.stderr)

    def test_check_local_uses_local_default_and_skips_remote(self):
        git(self.wt, "remote", "remove", "origin")
        self.write("a.txt", "a\n")
        self.commit("work")
        with mock.patch.object(fp, "_git", wraps=fp._git) as g:
            self.assertEqual(fp.check(self.wt, branch="factory/001-a", base=self.base,
                                      default_branch="main", origin_url=None, local=True), [])
        self.assertFalse([c for c in g.call_args_list if "ls-remote" in c.args])
        other = git(self.wt, "commit-tree", "-m", "x", f"{self.base}^{{tree}}").stdout.strip()
        git(self.wt, "update-ref", "refs/heads/main", other)
        self.assertIn("rule 3: base is not an ancestor of the local default branch",
                      fp.check(self.wt, branch="factory/001-a", base=self.base,
                               default_branch="main", origin_url=None, local=True))

    def test_repo_info(self):
        git(self.wt, "remote", "set-head", "origin", "main")
        self.assertEqual(fp.repo_info(self.wt),
                         {"mode": "ado", "origin_url": self.url, "default": "main"})
        with mock.patch.object(fp, "validate_origin", _REAL_VALIDATE):
            self.assertEqual(fp.repo_info(self.wt)["mode"], "local")
        git(self.wt, "remote", "remove", "origin")
        self.assertEqual(fp.repo_info(self.wt),
                         {"mode": "local", "origin_url": None, "default": "main"})
        git(self.wt, "branch", "-m", "main", "trunk")
        self.assertEqual(fp.repo_info(self.wt)["default"], "factory/001-a")
        r = self.cli("mode", "--repo", str(self.wt))
        self.assertEqual(json.loads(r.stdout)["mode"], "local")


    def test_rule7_refuses_job_changing_factory_files(self):
        (self.wt / ".factory").mkdir()
        self.write(".factory/backlog.md", "- [ ] sneaked\n")
        self.commit("edit backlog")
        self.assertIn("rule 7: job changes .factory/backlog.md", self.chk())


if __name__ == "__main__":
    unittest.main()
