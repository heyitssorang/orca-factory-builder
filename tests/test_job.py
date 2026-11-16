import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_job as fj  # noqa: E402


class JobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.jobs = fj.jobs_dir(self.home)
        self.jobs.mkdir(parents=True)

    def mk(self, owner="run-a"):
        d = self.jobs / "001-x"
        d.mkdir()
        fj.new_job(d, feature="f", repo="/r", owner=owner)
        return d

    def test_home_env(self):
        old = os.environ.get("FACTORY_HOME")
        os.environ["FACTORY_HOME"] = str(self.home)
        try:
            self.assertEqual(fj.home(), self.home)
        finally:
            if old is None:
                del os.environ["FACTORY_HOME"]
            else:
                os.environ["FACTORY_HOME"] = old

    def test_repo_home_resolves_main_checkout(self):
        repo = (self.home / "repo").resolve()
        g = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run([*g, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "i"],
                       check=True)
        wt = self.home / "wt"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
        (repo / "sub").mkdir()
        for where in (repo, repo / "sub", wt):
            self.assertEqual(fj.repo_home(where), repo / ".factory", where)
        with self.assertRaisesRegex(fj.BadRequest, "not in a git repo"):
            fj.repo_home(self.home)
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_HOME"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(fj.home(wt), repo / ".factory")
        self.assertEqual(fj.repo_of(repo / ".factory"), repo)

    def test_prepare_home_writes_gitignore_once(self):
        h = self.home / "r" / ".factory"
        fj.prepare_home(h)
        self.assertEqual((h / ".gitignore").read_text(), fj.HOME_GITIGNORE)
        (h / ".gitignore").write_text("custom\n")
        fj.prepare_home(h)
        self.assertEqual((h / ".gitignore").read_text(), "custom\n")

    def test_slugify(self):
        self.assertEqual(fj.slugify("Add a 404 page for unknown routes"), "add-a-404-page")
        self.assertEqual(fj.slugify("Ärger über €"), "rger-ber")
        self.assertEqual(fj.slugify("!!!"), "job")
        self.assertLessEqual(len(fj.slugify("a" * 100 + " b")), 40)

    def test_claim_sequence_and_collision(self):
        a, _ = fj.claim_job_dir(self.jobs, "Footer links")
        self.assertEqual(a, "001-footer-links")
        (self.jobs / "002-taken").mkdir()
        b, p = fj.claim_job_dir(self.jobs, "Reading time")
        self.assertEqual(b, "003-reading-time")
        self.assertTrue(p.is_dir())
        self.assertEqual(fj.job_number(b), 3)

    def test_claim_race_two_processes(self):
        code = (
            "import sys; sys.path.insert(0, %r); import factory_job as fj, pathlib;"
            "[print(fj.claim_job_dir(pathlib.Path(%r), 'race')[0]) for _ in range(10)]"
            % (str(ROOT / "scripts"), str(self.jobs))
        )
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
                 for _ in range(2)]
        ids = []
        for p in procs:
            out, _ = p.communicate()
            self.assertEqual(p.returncode, 0)
            ids += out.split()
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)
        self.assertEqual(len([p for p in self.jobs.iterdir() if not p.name.startswith(".")]), 20)

    def test_claim_race_distinct_slugs(self):
        code = (
            "import sys; sys.path.insert(0, %r); import factory_job as fj, pathlib;"
            "[print(fj.claim_job_dir(pathlib.Path(%r), 'feat %s ' + str(i))[0]) for i in range(10)]"
        )
        procs = [subprocess.Popen(
            [sys.executable, "-c", code % (str(ROOT / "scripts"), str(self.jobs), tag)],
            stdout=subprocess.PIPE, text=True) for tag in ("alpha", "beta")]
        ids = []
        for p in procs:
            out, _ = p.communicate()
            self.assertEqual(p.returncode, 0)
            ids += out.split()
        self.assertEqual(len(ids), 20)
        self.assertEqual(len({fj.job_number(i) for i in ids}), 20)

    def test_new_job_fields_and_mode(self):
        d = self.mk()
        j = fj.read_job(d)
        for k in ("id", "feature", "repo", "default_branch", "base", "branch", "worktree", "port",
                  "stage", "round", "owner", "reviews", "verified", "risk", "overlaps", "run", "hold", "accepted",
                  "merge_commit", "workers", "takeovers", "created", "updated"):
            self.assertIn(k, j)
        self.assertNotIn("pr", j)
        self.assertEqual((j["run"], j["hold"], j["accepted"], j["merge_commit"]),
                         (None, None, None, None))
        self.assertEqual(j["stage"], "spec")
        self.assertEqual(j["round"], 0)
        self.assertEqual(j["verified"], {"confirmed": 0, "false_positive": 0, "unverifiable": 0})
        self.assertEqual(os.stat(d / "job.json").st_mode & 0o777, 0o600)

    def test_new_job_run(self):
        d = self.mk()
        d2 = d.parent / "002-x"
        d2.mkdir()
        fj.new_job(d2, feature="f", repo="/r", owner="o", run="20261016-1530-ab12")
        self.assertEqual(fj.read_job(d2)["run"], "20261016-1530-ab12")

    def test_update_refuses_non_owner(self):
        d = self.mk()
        with self.assertRaises(fj.OwnerMismatch):
            fj.update_job(d, {"stage": "build"}, owner="run-b")
        self.assertEqual(fj.read_job(d)["stage"], "spec")
        j = fj.update_job(d, {"stage": "build"}, owner="run-a")
        self.assertEqual(j["stage"], "build")
        self.assertEqual(fj.read_job(d)["stage"], "build")

    def test_takeover_logged(self):
        d = self.mk()
        j = fj.takeover(d, "run-b", reason="resume")
        self.assertEqual(j["owner"], "run-b")
        self.assertEqual(j["takeovers"][-1]["from"], "run-a")
        self.assertEqual(j["takeovers"][-1]["to"], "run-b")
        self.assertEqual(j["takeovers"][-1]["reason"], "resume")
        fj.update_job(d, {"stage": "build"}, owner="run-b")

    def test_bogus_owner_refused(self):
        d = self.mk()
        for bad in ("", " ", "None", "null", "undefined", "run a", "run\n"):
            with self.assertRaises(fj.BadRequest, msg=repr(bad)):
                fj.takeover(d, bad, reason="resume")
            with self.assertRaises(fj.BadRequest, msg=repr(bad)):
                fj.new_job(self.home / "jobs" / "002-x", feature="f", repo="r", owner=bad)
        self.assertEqual(fj.read_job(d)["owner"], "run-a")
        self.assertFalse((self.home / "jobs" / "002-x").exists())

    def test_cli(self):
        env = dict(os.environ, FACTORY_HOME=str(self.home))
        run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts/factory_job.py"), *a],
                                        env=env, capture_output=True, text=True)
        r = run("claim", "--feature", "Hello world", "--repo", "/r", "--owner", "o1")
        self.assertEqual(r.returncode, 0, r.stderr)
        jid = r.stdout.strip()
        self.assertEqual(jid, "001-hello-world")
        self.assertEqual(json.loads(run("show", jid).stdout)["owner"], "o1")
        self.assertEqual(run("update", jid, "--owner", "o2", "--patch", '{"stage":"build"}').returncode, 1)
        self.assertEqual(run("update", jid, "--owner", "o1", "--patch", '{"stage":"build"}').returncode, 0)
        self.assertEqual(run("takeover", jid, "--owner", "o2", "--reason", "r").returncode, 0)

    def test_failed_needs_reason(self):
        d = self.mk()
        self.assertIsNone(fj.read_job(d)["reason"])
        for bad in ({"stage": "failed"}, {"stage": "failed", "reason": "  "},
                    {"stage": "failed", "reason": ["x"]}):
            with self.assertRaises(fj.BadRequest, msg=repr(bad)):
                fj.update_job(d, bad, owner="run-a")
        self.assertEqual(fj.read_job(d)["stage"], "spec")
        j = fj.update_job(d, {"stage": "failed", "reason": " spec.md missing \n"}, owner="run-a")
        self.assertEqual((j["stage"], j["reason"]), ("failed", "spec.md missing"))
        j = fj.update_job(d, {"stage": "failed", "reason": "x" * 5000}, owner="run-a")
        self.assertEqual(len(j["reason"]), fj.REASON_MAX)
        j = fj.update_job(d, {"round": 2}, owner="run-a")  # no stage change: reason kept
        self.assertEqual(len(j["reason"]), fj.REASON_MAX)
        j = fj.update_job(d, {"stage": "build"}, owner="run-a")  # rework clears it
        self.assertIsNone(j["reason"])

    def test_update_refuses_protected_fields(self):
        d = self.mk()
        for key in ("owner", "id", "takeovers"):
            with self.assertRaises(ValueError):
                fj.update_job(d, {key: "x", "stage": "build"}, owner="run-a")
        with self.assertRaises(ValueError):
            fj.update_job(d, ["stage"], owner="run-a")
        j = fj.read_job(d)
        self.assertEqual((j["owner"], j["id"], j["stage"], j["takeovers"]),
                         ("run-a", "001-x", "spec", []))

    def _cli(self):
        env = dict(os.environ, FACTORY_HOME=str(self.home))
        return lambda *a: subprocess.run(
            [sys.executable, str(ROOT / "scripts/factory_job.py"), *a],
            env=env, capture_output=True, text=True)

    def test_cli_protected_fields_exit_2(self):
        run = self._cli()
        jid = run("claim", "--feature", "Hello", "--repo", "/r", "--owner", "o1").stdout.strip()
        for patch in ('{"owner":"o2"}', '{"id":"002-x"}', '{"takeovers":[]}', '[1]', '{bad'):
            r = run("update", jid, "--owner", "o1", "--patch", patch)
            self.assertEqual(r.returncode, 2, (patch, r.stderr))
        self.assertEqual(json.loads(run("show", jid).stdout)["owner"], "o1")

    def test_cli_rejects_bad_job_id(self):
        run = self._cli()
        outside = self.home / "x"
        outside.mkdir()
        fj.new_job(outside, feature="f", repo="/r", owner="o1")
        for cmd in (("show",), ("next",), ("update", "--owner", "o1", "--patch", '{"stage":"x"}'),
                    ("takeover", "--owner", "o2", "--reason", "r")):
            for bad in ("../x", "1-x", "001-X", "001-x/../../x", "001-x\n", "001_x"):
                r = run(cmd[0], bad, *cmd[1:])
                self.assertEqual(r.returncode, 2, (cmd, bad, r.stdout, r.stderr))
        self.assertEqual(fj.read_job(outside)["owner"], "o1")
        self.assertEqual(fj.read_job(outside)["stage"], "spec")

    def test_job_numbers_past_999(self):
        for n in range(1, 1000):
            (self.jobs / f".n-{n:03d}").mkdir()
        jid, d = fj.claim_job_dir(self.jobs, "Big job")
        self.assertEqual(jid, "1000-big-job")
        self.assertEqual(fj.job_number(jid), 1000)
        fj.new_job(d, feature="f", repo="/r", owner="o1")
        run = self._cli()
        r = run("show", jid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["id"], jid)

    def test_deeply_nested_job_json_is_unreadable(self):
        d = self.mk()
        (d / "job.json").write_text("[" * 100000 + "]" * 100000)
        with self.assertRaises(ValueError):
            fj.read_job(d)
        self.assertEqual(fj.next_step(d), "spec")
        run = self._cli()
        r = run("show", "001-x")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)

    def test_next_step(self):
        d = self.mk()
        st = lambda **kw: fj.update_job(d, kw, owner="run-a")
        self.assertEqual(fj.next_step(d), "spec")
        (d / "spec.md").write_text("spec\n")
        self.assertEqual(fj.next_step(d), "build")  # stage spec, spec written
        st(stage="build")
        self.assertEqual(fj.next_step(d), "build")
        (d / "build.md").write_text("STATUS: READY\n")
        self.assertEqual(fj.next_step(d), "build")  # builder not accepted yet
        st(stage="review", round=1)
        (d / "build.md").write_text("STATUS: BLOCKED\nneeds input\n")
        self.assertEqual(fj.next_step(d), "build")  # not finished: stage is not blocked
        st(stage="blocked")
        self.assertEqual(fj.next_step(d), "none")
        st(stage="review")
        (d / "build.md").write_text("STATUS: READY\n")
        self.assertEqual(fj.next_step(d), "review")
        r = d / "round-1"
        r.mkdir()
        (r / "review-security.md").write_text("VERDICT: PASS\n")
        self.assertEqual(fj.next_step(d), "review")
        (r / "review-code.md").write_text(
            "VERDICT: CHANGES\n\n## Findings\n1. src/a.py:3 — problem\n")
        (r / "review-ux.md").write_text("VERDICT: PASS\n")
        (r / "review-ui.md").write_text("VERDICT: PASS\n")
        self.assertEqual(fj.next_step(d), "verify")
        (r / "verify-code-1.md").write_text("CONFIRMED\n")
        self.assertEqual(fj.next_step(d), "verify")  # risk.md missing
        (r / "risk.md").write_text("low\n")
        self.assertEqual(fj.next_step(d), "finish")
        self.assertEqual(fj.STAGES, ("spec", "build", "review", "verify", "held", "merged",
                                     "shipped", "blocked", "failed", "closed"))
        for done in fj.DONE_STAGES:
            st(stage=done, **({"reason": "r"} if done == "failed" else {}))
            self.assertEqual(fj.next_step(d), "none", done)

    def test_next_step_rework_round(self):
        d = self.mk()
        (d / "spec.md").write_text("spec\n")
        (d / "build.md").write_text("STATUS: READY\n")
        r = d / "round-1"
        r.mkdir()
        for lens in fj.LENSES:
            (r / f"review-{lens}.md").write_text("VERDICT: PASS\n")
        (r / "risk.md").write_text("RISK: NONE\n")
        fj.update_job(d, {"stage": "merged", "round": 1}, owner="run-a")
        self.assertEqual(fj.next_step(d), "none")
        fj.update_job(d, {"stage": "build"}, owner="run-a")  # rework started
        self.assertEqual(fj.next_step(d), "build")
        fj.update_job(d, {"stage": "review", "round": 2}, owner="run-a")  # build accepted
        self.assertEqual(fj.next_step(d), "review")  # round 1 being complete does not count


if __name__ == "__main__":
    unittest.main()
