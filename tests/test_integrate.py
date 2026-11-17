import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_backlog as fb  # noqa: E402
import factory_integrate as fi  # noqa: E402
import factory_job as fj  # noqa: E402
import factory_push as fp  # noqa: E402

SECRET = "AKIAABCDEFGHIJKLMNOP"
PASS = "VERDICT: PASS\n## Findings\nNone.\n"
CODE_PASS = "VERDICT: PASS\n## Checks\n- `make test` — pass\n## Findings\nNone.\n"
CODE_CHANGES = ("VERDICT: CHANGES\n## Checks\n- `make test` — pass\n## Findings\n"
                "1. a.py:1 — wrong value\n")


def _tmp_parent():
    """A temp parent where hooks can execute (/tmp is often mounted noexec), else default."""
    d = os.environ.get("XDG_RUNTIME_DIR")
    return d if d and os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK) else None


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=check, env=env)


class IntegrateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=_tmp_parent())
        self.addCleanup(self._tmp.cleanup)
        self.t = t = Path(self._tmp.name)
        patcher = mock.patch.object(fp, "validate_origin", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = t / "home"
        (self.home / "jobs").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.home), "FACTORY_TMPDIR": str(t)})
        env.start()
        self.addCleanup(env.stop)
        tmpdir = mock.patch.object(tempfile, "tempdir", str(t))  # gate worktrees stay in t
        tmpdir.start()
        self.addCleanup(tmpdir.stop)
        self.origin = t / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.url = str(self.origin)
        self.repo = t / "repo"
        subprocess.run(["git", "clone", "-q", self.url, str(self.repo)],
                       check=True, capture_output=True)
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "README").write_text("one\ntwo\nthree\n")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "push", "-q", "origin", "main")
        self.base = self.rev("HEAD")
        self.run_id = "20261016-1200-abcd"

    # --- helpers -------------------------------------------------------------

    def rev(self, ref, cwd=None):
        return git(cwd or self.repo, "rev-parse", ref).stdout.strip()

    def start(self, run_id=None):
        return fi.start_run(self.home, run_id or self.run_id, repo=self.repo,
                            default_branch="main", origin_url=self.url, owner="o")

    def run_tip(self, run_id=None):
        return self.rev(f"refs/heads/factory-run/{run_id or self.run_id}")

    def make_job(self, job_id="001-a", feature="Add a thing", files=None, draft=False,
                 run_id=None, rnd=1):
        files = files or {f"{job_id}.txt": "x\n"}
        wt = self.t / f"wt-{job_id}"
        git(self.repo, "worktree", "add", "-q", "-b", f"factory/{job_id}", str(wt), self.base)
        for name, text in files.items():
            (wt / name).write_text(text)
            git(wt, "add", name)
        git(wt, "commit", "-q", "-m", f"work {job_id}")
        jd = self.home / "jobs" / job_id
        jd.mkdir()
        fj.new_job(jd, feature=feature, repo=str(self.repo), owner="o", default_branch="main",
                   run=run_id or self.run_id)
        fj.update_job(jd, {"base": self.base, "branch": f"factory/{job_id}", "worktree": str(wt),
                           "stage": "verify", "round": rnd, "origin_url": self.url},
                      owner="o")
        (jd / "spec.md").write_text("# Spec\n## Acceptance criteria\n1. Thing exists.\n")
        (jd / "build.md").write_text("STATUS: READY\n## Criteria\n1. met — ok\n")
        self.write_round(jd, rnd, draft)
        self.assertEqual(fi.mark_review(self.home, job_id, owner="o"),
                         self.rev(f"factory/{job_id}"))
        return jd

    def write_round(self, jd, rnd, draft):
        r = jd / f"round-{rnd}"
        r.mkdir()
        for lens in ("security", "ux", "ui"):
            (r / f"review-{lens}.md").write_text(PASS)
        (r / "review-code.md").write_text(CODE_CHANGES if draft else CODE_PASS)
        if draft:
            (r / "verify-code-1.md").write_text("VERIFY: CONFIRMED\n")
        (r / "risk.md").write_text("RISK: NONE\n")

    def finish(self, job_id="001-a", run_id=None):
        return fi.finish_job(self.home, run_id or self.run_id, job_id, owner="o")

    def job(self, job_id="001-a"):
        return fj.read_job(self.home / "jobs" / job_id)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fi.main(list(args))
        return subprocess.CompletedProcess(args, rc, out.getvalue(), err.getvalue())

    # --- runs ----------------------------------------------------------------

    def test_new_run_id_format(self):
        rid = fi.new_run_id(datetime(2026, 10, 16, 9, 5, tzinfo=timezone.utc), "0a1b")
        self.assertEqual(rid, "20261016-0905-0a1b")
        self.assertTrue(fi.RUN_ID_RE.fullmatch(rid))
        a = fi.claim_run(self.home)
        self.assertTrue(fi.RUN_ID_RE.fullmatch(a))
        self.assertTrue((fi.runs_dir(self.home) / f"{a}.lock").is_dir())
        self.assertNotEqual(a, fi.claim_run(self.home))

    def test_start_run_creates_branch_at_origin_main(self):
        run = self.start()
        self.assertEqual(self.run_tip(), self.base)
        self.assertEqual(run["base"], self.base)
        self.assertEqual(run["branch"], f"factory-run/{self.run_id}")
        saved = fi.load_run(self.home, self.run_id)
        for k in ("id", "repo", "default_branch", "branch", "base", "owner", "merged", "held",
                  "shipped", "created", "updated"):
            self.assertIn(k, saved)
        self.assertEqual((saved["merged"], saved["held"], saved["shipped"]), ([], [], None))
        refs = json.loads((fi.runs_dir(self.home) / f"{self.run_id}.refs-before.json").read_text())
        self.assertEqual(refs["refs/heads/main"], self.base)

    def test_start_run_refuses_existing_branch(self):
        git(self.repo, "branch", f"factory-run/{self.run_id}")
        with self.assertRaises(fi.Refused):
            self.start()

    def test_start_run_keeps_scheduler_keys(self):
        path = fi.runs_dir(self.home) / f"{self.run_id}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"caps": {"jobs": 2}, "pending": [], "running": []}))
        self.start()
        data = json.loads(path.read_text())
        self.assertEqual(data["caps"], {"jobs": 2})
        self.assertEqual(data["owner"], "o")

    def test_owner_enforced_on_run_file(self):
        self.start()
        with self.assertRaises(fj.OwnerMismatch):
            fi.save_run(self.home, self.run_id, {"held": []}, owner="other")
        with self.assertRaises(fj.OwnerMismatch):
            fi.start_run(self.home, self.run_id, repo=self.repo, default_branch="main",
                         origin_url=self.url, owner="other")
        self.make_job()
        with self.assertRaises(fj.OwnerMismatch):
            fi.finish_job(self.home, self.run_id, "001-a", owner="other")
        self.assertEqual(self.run_tip(), self.base)

    def test_save_run_preserves_unknown_keys(self):
        self.start()
        path = fi.runs_dir(self.home) / f"{self.run_id}.json"
        data = json.loads(path.read_text())
        data["pending"] = ["fresh"]
        path.write_text(json.dumps(data))
        run = fi.load_run(self.home, self.run_id)
        stale = dict(run, pending=["stale"], held=[{"job": "x"}])
        fi.save_run(self.home, self.run_id, stale, owner="o")
        now = fi.load_run(self.home, self.run_id)
        self.assertEqual(now["pending"], ["fresh"])
        self.assertEqual(now["held"], [{"job": "x"}])

    # --- merging -------------------------------------------------------------

    def test_ready_job_lands_as_one_commit(self):
        self.start()
        jd = self.make_job()
        job_tip = self.rev("factory/001-a")
        r = self.finish()
        self.assertEqual(r.status, "merged", r)
        rng = f"{self.base}..factory-run/{self.run_id}"
        log = git(self.repo, "log", "--format=%s%n%b", rng).stdout
        self.assertTrue(log.startswith("001-a: Add a thing"), log)
        self.assertIn("Factory-Job: 001-a", log)
        self.assertIn("Factory-Round: 1", log)
        self.assertEqual(git(self.repo, "rev-list", "--count", rng).stdout.strip(), "1")
        self.assertEqual(git(self.repo, "show", f"factory-run/{self.run_id}:001-a.txt").stdout,
                         "x\n")
        self.assertEqual(self.rev("factory/001-a"), job_tip)
        job = fj.read_job(jd)
        self.assertEqual(job["stage"], "merged")
        self.assertEqual(job["merge_commit"], self.run_tip())
        run = fi.load_run(self.home, self.run_id)
        self.assertEqual(run["merged"], [{"job": "001-a", "commit": self.run_tip()}])
        st = fi.run_status(self.home, self.run_id)
        self.assertEqual(st["ahead"], 1)
        # a merged job is not merged again
        self.assertEqual(self.finish().status, "refused")
        self.assertEqual(fi.merge_job(self.home, self.run_id, "001-a", owner="o").status,
                         "refused")

    # --- base for a job that must follow another (`after:`) ---------------------

    def test_a_merged_dependency_is_missing_from_the_default_ref_but_present_on_the_run_tip(self):
        """The repro: job 002 follows 001, which is merged into the run but not shipped. A worktree
        based on the default ref (what step 3 used) has no 001 code; the run tip has it."""
        self.start()
        self.make_job("001-a", files={"a_api.txt": "def a(): ...\n"})
        self.assertEqual(self.finish("001-a").status, "merged")
        dref = "origin/main"
        self.assertNotEqual(git(self.repo, "cat-file", "-e", f"{dref}:a_api.txt", check=False).returncode, 0)
        base = fi.after_base(self.home, self.run_id, "001-a")
        self.assertEqual(base, self.run_tip())
        self.assertEqual(git(self.repo, "cat-file", "-e", f"{base}:a_api.txt").returncode, 0)

    def test_after_base_is_none_when_the_dependency_is_shipped(self):
        self.start()
        self.make_job("001-a")
        self.assertEqual(self.finish("001-a").status, "merged")
        fj.update_job(self.home / "jobs" / "001-a", {"stage": "shipped"}, owner="o")
        self.assertIsNone(fi.after_base(self.home, self.run_id, "001-a"))  # DREF has it

    def test_after_base_refuses_a_dependency_that_is_not_merged_or_is_in_another_run(self):
        self.start()
        self.make_job("001-a")
        with self.assertRaises(fi.Refused):                       # not merged yet
            fi.after_base(self.home, self.run_id, "001-a")
        self.assertEqual(self.finish("001-a").status, "merged")
        self.start("20261016-1300-beef")
        with self.assertRaises(fi.Refused):                       # merged into a different run
            fi.after_base(self.home, "20261016-1300-beef", "001-a")

    def test_merge_touches_no_checkout_and_runs_no_hook(self):
        self.start()
        self.make_job()
        marker = self.t / "hook-ran"
        hooks = self.repo / ".git" / "hooks"
        for h in ("pre-commit", "post-commit", "commit-msg", "reference-transaction",
                  "post-merge"):
            (hooks / h).write_text(f"#!/bin/sh\necho {h} >> {marker}\n")
            (hooks / h).chmod(0o755)
        git(self.repo, "update-ref", "refs/probe", self.base)  # hooks live here?
        if not marker.exists():
            self.skipTest("hooks cannot execute in this temp directory")
        marker.unlink()
        (self.repo / "README").write_text("local edit\n")
        status = git(self.repo, "status", "--porcelain").stdout
        head = self.rev("HEAD")
        self.assertEqual(self.finish().status, "merged")
        self.assertFalse(marker.exists())
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, status)
        self.assertEqual(self.rev("HEAD"), head)
        self.assertEqual((self.repo / "README").read_text(), "local edit\n")

    def test_merge_driver_config_refused(self):
        self.start()
        self.make_job()
        git(self.repo, "config", "merge.evil.driver", "touch /tmp/x")
        r = self.finish()
        self.assertEqual(r.status, "refused")
        self.assertTrue(any("merge.evil.driver" in x for x in r.reasons), r.reasons)
        self.assertEqual(self.run_tip(), self.base)

    def test_draft_job_is_held(self):
        self.start()
        self.make_job(draft=True)
        r = self.finish()
        self.assertEqual(r.status, "held")
        self.assertIn("code finding 1 CONFIRMED", r.reasons)
        job = self.job()
        self.assertEqual(job["stage"], "held")
        self.assertEqual(job["hold"]["kind"], "draft")
        self.assertEqual(job["hold"]["round"], 1)
        run = fi.load_run(self.home, self.run_id)
        self.assertEqual([h["job"] for h in run["held"]], ["001-a"])
        self.assertEqual(self.run_tip(), self.base)
        # merge_job alone refuses an unaccepted draft
        self.assertEqual(fi.merge_job(self.home, self.run_id, "001-a", owner="o").status,
                         "refused")
        self.assertEqual(self.run_tip(), self.base)

    def test_accept_then_merge(self):
        self.start()
        self.make_job(draft=True)
        self.finish()
        job = fi.accept(self.home, "001-a", owner="o", by="max", note="fine")
        self.assertEqual(job["accepted"]["round"], 1)
        self.assertEqual(job["accepted"]["by"], "max")
        self.assertIn("code finding 1 CONFIRMED", job["accepted"]["reasons"])
        r = self.finish()
        self.assertEqual(r.status, "merged", r)
        job = self.job()
        self.assertEqual((job["stage"], job["hold"]), ("merged", None))
        self.assertEqual(fi.load_run(self.home, self.run_id)["held"], [])
        body = git(self.repo, "log", "-1", "--format=%B", f"factory-run/{self.run_id}").stdout
        self.assertIn("Accepted", body)

    def test_accept_from_old_round_does_not_count(self):
        self.start()
        jd = self.make_job(draft=True)
        self.finish()
        fi.accept(self.home, "001-a", owner="o", by="max", note="ok")
        # rework: round 2, still a draft
        fj.update_job(jd, {"round": 2, "stage": "verify"}, owner="o")
        self.write_round(jd, 2, draft=True)
        fj.update_job(jd, {"stage": "held"}, owner="o")  # hold still from round 1
        with self.assertRaises(fi.Refused):
            fi.accept(self.home, "001-a", owner="o", by="max", note="")
        fj.update_job(jd, {"stage": "verify"}, owner="o")
        self.assertEqual(fi.merge_job(self.home, self.run_id, "001-a", owner="o").status,
                         "refused")
        r = self.finish()
        self.assertEqual(r.status, "held")
        self.assertEqual(self.job()["hold"]["round"], 2)
        self.assertEqual(self.run_tip(), self.base)

    def test_accept_refused_for_conflict_and_refused_holds(self):
        self.start()
        jd = self.make_job(draft=True)
        for kind in ("conflict", "refused"):
            fj.update_job(jd, {"stage": "held", "hold": {"kind": kind, "round": 1,
                                                         "reasons": ["x"]}}, owner="o")
            with self.assertRaises(fi.Refused):
                fi.accept(self.home, "001-a", owner="o", by="max", note="")
        fj.update_job(jd, {"stage": "verify", "hold": None}, owner="o")
        with self.assertRaises(fi.Refused):  # not held
            fi.accept(self.home, "001-a", owner="o", by="max", note="")

    def test_accept_refused_when_run_shipped(self):
        self.start()
        self.make_job(draft=True)
        self.finish()
        run = fi.load_run(self.home, self.run_id)
        run["shipped"] = {"merge_sha": self.base, "pushed": False, "at": "now"}
        fi.save_run(self.home, self.run_id, run, owner="o")
        with self.assertRaises(fi.Refused):
            fi.accept(self.home, "001-a", owner="o", by="max", note="")

    def test_secret_refused_even_when_accepted(self):
        self.start()
        self.make_job(draft=True, files={"c.py": f'k = "{SECRET}"\n'})
        self.finish()
        fi.accept(self.home, "001-a", owner="o", by="max", note="")
        r = self.finish()
        self.assertEqual(r.status, "refused")
        self.assertTrue(any(x.startswith("rule 4:") for x in r.reasons), r.reasons)
        self.assertNotIn(SECRET, json.dumps(r.reasons))
        job = self.job()
        self.assertEqual((job["stage"], job["hold"]["kind"]), ("held", "refused"))
        self.assertEqual(self.run_tip(), self.base)
        with self.assertRaises(fi.Refused):
            fi.accept(self.home, "001-a", owner="o", by="max", note="")

    def test_conflict_holds_and_leaves_run_branch(self):
        self.start()
        self.make_job("001-a", files={"README": "one\nTWO-A\nthree\n"})
        self.make_job("002-b", feature="Other", files={"README": "one\nTWO-B\nthree\n"})
        self.assertEqual(self.finish("001-a").status, "merged")
        tip = self.run_tip()
        r = self.finish("002-b")
        self.assertEqual(r.status, "conflict")
        self.assertEqual(r.paths, ["README"])
        self.assertEqual(self.run_tip(), tip)
        job = self.job("002-b")
        self.assertEqual(job["stage"], "held")
        self.assertEqual(job["hold"]["kind"], "conflict")
        self.assertEqual(job["hold"]["paths"], ["README"])
        self.assertIn("conflicts with 001-a in README", job["hold"]["reasons"])
        run = fi.load_run(self.home, self.run_id)
        self.assertEqual([h["job"] for h in run["held"]], ["002-b"])

    def test_two_ready_jobs_both_land(self):
        self.start()
        self.make_job("001-a")
        self.make_job("002-b", feature="Other")
        self.assertEqual(self.finish("001-a").status, "merged")
        self.assertEqual(self.finish("002-b").status, "merged")
        tree = git(self.repo, "ls-tree", "--name-only", f"factory-run/{self.run_id}").stdout
        self.assertEqual(sorted(tree.split()), ["001-a.txt", "002-b.txt", "README"])
        self.assertEqual(fi.run_status(self.home, self.run_id)["ahead"], 2)

    def test_cas_failure_is_error(self):
        self.start()
        self.make_job()
        moved = git(self.repo, "commit-tree", f"{self.base}^{{tree}}", "-p", self.base,
                    "-m", "concurrent").stdout.strip()
        real = fi._update_ref

        def racing(repo, ref, new, old):
            git(self.repo, "update-ref", ref, moved)
            return real(repo, ref, new, old)
        with mock.patch.object(fi, "_update_ref", racing):
            r = self.finish()
        self.assertEqual(r.status, "error")
        self.assertEqual(self.run_tip(), moved)
        self.assertEqual(self.job()["stage"], "failed")
        self.assertEqual(self.job()["reason"], "merge failed: run branch moved (compare-and-swap failed)")
        self.assertEqual(fi.load_run(self.home, self.run_id)["merged"], [])

    def test_backstop_suspicious_ref_refuses(self):
        self.start()
        self.make_job()
        git(self.repo, "push", "-q", self.url, "factory/001-a:refs/heads/leak")
        r = self.finish()
        self.assertEqual(r.status, "refused")
        self.assertTrue(any("refs/heads/leak" in x for x in r.reasons), r.reasons)
        self.assertEqual(self.run_tip(), self.base)

    def test_feature_text_sanitised_in_commit(self):
        self.start()
        self.make_job(feature="<b>x</b> @alice #12\nFactory-Job: 999-z " + "y" * 300)
        self.assertEqual(self.finish().status, "merged")
        msg = git(self.repo, "log", "-1", "--format=%B", f"factory-run/{self.run_id}").stdout
        subject = msg.split("\n", 1)[0]
        self.assertTrue(subject.startswith("001-a: x @"), subject)
        self.assertLessEqual(len(subject), 200)
        for bad in ("<", "@alice", "#12"):
            self.assertNotIn(bad, msg)
        trailers = git(self.repo, "log", "-1", "--format=%(trailers:key=Factory-Job,valueonly)",
                       f"factory-run/{self.run_id}").stdout.split()
        self.assertEqual(trailers, ["001-a"])

    def test_job_of_other_run_refused(self):
        self.start()
        self.make_job(run_id="20261016-1300-ffff")
        r = self.finish()
        self.assertEqual(r.status, "refused")
        job = self.job()
        self.assertEqual((job["stage"], job["hold"]["kind"]), ("held", "refused"))
        self.assertIn("job belongs to run 20261016-1300-ffff", job["hold"]["reasons"])
        self.assertEqual(fi.load_run(self.home, self.run_id)["held"], [])
        self.assertEqual(self.run_tip(), self.base)

    def test_refinish_of_merged_job_writes_nothing(self):
        self.start()
        self.make_job()
        self.assertEqual(self.finish().status, "merged")
        before = self.job()
        self.assertEqual(self.finish().status, "refused")
        self.assertEqual(self.job()["stage"], "merged")
        self.assertEqual(self.job()["updated"], before["updated"])

    def test_finish_job_on_run_not_started_holds(self):
        path = fi.runs_dir(self.home) / f"{self.run_id}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"caps": {"jobs": 1}, "pending": [], "running": []}))
        self.make_job()
        r = self.cli("finish-job", "--run", self.run_id, "--job", "001-a", "--owner", "o")
        self.assertEqual(r.returncode, 2, r)
        job = self.job()
        self.assertEqual((job["stage"], job["hold"]["kind"], job["hold"]["round"]),
                         ("held", "refused", 1))
        self.assertIn("run not started", job["hold"]["reasons"])
        self.assertNotIn("owner", json.loads(path.read_text()))

    def test_rework_moves_held_job_after_ship(self):
        self.start()
        self.make_job(draft=True)
        self.finish()
        run2 = "20261016-1300-beef"
        self.start(run2)
        with self.assertRaises(fi.Refused):  # old run not shipped
            fi.reassign(self.home, "001-a", run2, owner="o")
        run = fi.load_run(self.home, self.run_id)
        run["shipped"] = {"merge_sha": self.base, "pushed": False, "at": "now"}
        fi.save_run(self.home, self.run_id, run, owner="o")
        job = fi.reassign(self.home, "001-a", run2, owner="o")
        self.assertEqual((job["run"], job["hold"], job["accepted"], job["review_head"],
                          job["review_round"]), (run2, None, None, None, None))
        self.assertEqual(fi.load_run(self.home, self.run_id)["held"], [])
        with self.assertRaises(fi.Refused):  # target shipped
            fi.reassign(self.home, "001-a", self.run_id, owner="o", force_move=True)

    def test_commit_after_review_refused(self):
        self.start()
        self.make_job()
        wt = self.t / "wt-001-a"
        (wt / "late.txt").write_text("unreviewed\n")
        git(wt, "add", "late.txt")
        git(wt, "commit", "-q", "-m", "late")
        r = self.finish()
        self.assertEqual(r.status, "refused")
        self.assertIn("branch moved since review", r.reasons)
        self.assertEqual(self.run_tip(), self.base)
        self.assertEqual(fi.merge_job(self.home, self.run_id, "001-a", owner="o").status,
                         "refused")
        self.assertEqual(self.run_tip(), self.base)

    def test_missing_review_head_refused(self):
        self.start()
        jd = self.make_job()
        fj.update_job(jd, {"review_head": None}, owner="o")
        r = self.finish()
        self.assertEqual(r.status, "refused")
        self.assertIn("review head not recorded", r.reasons)
        self.assertEqual(self.run_tip(), self.base)
        r = self.cli("mark-review", "--job", "001-a", "--owner", "o")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["review_head"], self.rev("factory/001-a"))
        self.assertEqual(self.finish().status, "merged")

    def test_bogus_run_owner_refused(self):
        self.start()
        for bad in ("None", ""):
            with self.assertRaises(fj.BadRequest):
                fi.takeover_run(self.home, self.run_id, bad, reason="resume")
            with self.assertRaises(fj.BadRequest):
                fi.save_run(self.home, self.run_id, {}, owner=bad)
        self.assertEqual(fi.load_run(self.home, self.run_id)["owner"], "o")
        r = self.cli("takeover-run", "--run", self.run_id, "--owner", "None", "--reason", "x")
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_takeover_run(self):
        self.start()
        self.make_job()
        fj.takeover(self.home / "jobs" / "001-a", "o2", reason="resume")
        with self.assertRaises(fj.OwnerMismatch):
            fi.finish_job(self.home, self.run_id, "001-a", owner="o2")
        r = self.cli("takeover-run", "--run", self.run_id, "--owner", "o2", "--reason", "resume")
        self.assertEqual(r.returncode, 0, r.stderr)
        run = fi.load_run(self.home, self.run_id)
        self.assertEqual(run["owner"], "o2")
        self.assertEqual(run["takeovers"][-1]["from"], "o")
        self.assertEqual(run["takeovers"][-1]["to"], "o2")
        self.assertEqual(run["takeovers"][-1]["reason"], "resume")
        self.assertEqual(fi.finish_job(self.home, self.run_id, "001-a", owner="o2").status,
                         "merged")
        with self.assertRaises(fj.OwnerMismatch):
            fi.save_run(self.home, self.run_id, {}, owner="o")

    def test_schedule_write_waits_for_lock_and_keeps_merged(self):
        import threading
        import factory_schedule as fs
        self.start()
        path = fi.runs_dir(self.home) / f"{self.run_id}.json"
        done = threading.Event()

        def request():
            with contextlib.redirect_stdout(io.StringIO()):
                fs.main(["request", "--run", str(path), "--job", "001-a", "--kind", "k",
                         "--workers", "1"])
            done.set()
        with fi._run_lock(self.home, self.run_id):
            th = threading.Thread(target=request)
            th.start()
            self.assertFalse(done.wait(0.5))  # scheduler blocked on the run lock
            run = fi.load_run(self.home, self.run_id)
            run["merged"] = [{"job": "001-a", "commit": self.base}]
            fi.save_run(self.home, self.run_id, run, owner="o")
        th.join(10)
        self.assertTrue(done.is_set())
        data = json.loads(path.read_text())
        self.assertEqual(data["merged"], [{"job": "001-a", "commit": self.base}])
        self.assertEqual(len(data["pending"]), 1)

    def test_status(self):
        self.start()
        self.make_job(draft=True)
        self.finish()
        st = fi.run_status(self.home, self.run_id)
        self.assertEqual(st["ahead"], 0)
        self.assertEqual([h["job"] for h in st["held"]], ["001-a"])
        self.assertIsNone(st["shipped"])

    def test_cli_exit_codes(self):
        r = self.cli("start-run", "--repo", str(self.repo), "--run", self.run_id,
                     "--default", "main", "--origin-url", self.url, "--owner", "o")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.make_job("001-a", files={"README": "one\nTWO-A\nthree\n"})
        self.make_job("002-b", files={"README": "one\nTWO-B\nthree\n"})
        self.make_job("003-c", files={"k.py": f'k = "{SECRET}"\n'})
        args = ("--run", self.run_id, "--owner", "o", "--job")
        r = self.cli("finish-job", *args, "001-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "merged")
        r = self.cli("finish-job", *args, "002-b")
        self.assertEqual(r.returncode, 3, r.stderr)
        r = self.cli("finish-job", *args, "003-c")
        self.assertEqual(r.returncode, 2, r.stderr)
        r = self.cli("status", "--run", self.run_id)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["ahead"], 1)
        r = self.cli("new-run")
        self.assertTrue(fi.RUN_ID_RE.fullmatch(json.loads(r.stdout)["run"]))
        r = self.cli("start-run", "--repo", str(self.repo), "--run", self.run_id,
                     "--default", "main", "--origin-url", self.url, "--owner", "o")
        self.assertEqual(r.returncode, 2)
        r = self.cli("status", "--run", "../x")
        self.assertEqual(r.returncode, 2)

    # --- ship ----------------------------------------------------------------

    def commit_main(self, files, msg="main change"):
        """Commit on the checked-out local main and push it to origin."""
        for name, text in files.items():
            (self.repo / name).write_text(text)
            git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", msg)
        git(self.repo, "push", "-q", "origin", "main")
        return self.rev("HEAD")

    def merged_run(self, agents=None, **job):
        if agents is not None:
            self.base = self.commit_main({"AGENTS.md": agents}, "agents")
        self.start()
        self.make_job(**job)
        self.assertEqual(self.finish(job.get("job_id", "001-a")).status, "merged")

    def prepare(self):
        return fi.ship_prepare(self.home, self.run_id, owner="o")

    def gate(self, allow_install=False):
        return fi.ship_gate(self.home, self.run_id, owner="o", allow_install=allow_install)

    def origin_main(self):
        return git(self.origin, "rev-parse", "main").stdout.strip()

    def gate_dirs(self):
        return [p for p in self.t.iterdir() if p.name.startswith("factory-gate-")]

    def test_read_agents_commands(self):
        sha = self.commit_main({"AGENTS.md": "# Agents\n- install: `npm ci`\n* test: npm test\n"
                                             "build:  make  \ndev: x {port}\ntest: second\n"})
        self.assertEqual(fi.read_agents_commands(self.repo, sha),
                         {"install": "npm ci", "test": "npm test", "build": "make"})
        self.assertEqual(fi.read_agents_commands(self.repo, self.base), {})

    def test_ship_refuses_without_merged_jobs(self):
        self.start()
        with self.assertRaisesRegex(fi.ShipRefused, "no merged jobs"):
            self.prepare()
        self.assertNotIn("ship", fi.load_run(self.home, self.run_id))
        r = self.cli("ship-prepare", "--run", self.run_id, "--owner", "o")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no merged jobs", r.stderr)

    def test_ship_prepare_merges_onto_server_tip(self):
        self.merged_run()
        other = self.commit_main({"other.txt": "o\n"})  # origin main moved after run start
        r = self.cli("ship-prepare", "--run", self.run_id, "--owner", "o")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        m = out["merge_sha"]
        self.assertEqual((out["run_base"], out["origin_moved"]), (self.base, 1))
        parents = git(self.repo, "rev-list", "--parents", "-n1", m).stdout.split()[1:]
        self.assertEqual(parents, [other, self.run_tip()])
        msg = git(self.repo, "log", "-1", "--format=%B", m).stdout
        self.assertTrue(msg.startswith(f"factory: ship run {self.run_id}\n\n"), msg)
        self.assertIn("001-a: Add a thing", msg)
        self.assertEqual(fi.load_run(self.home, self.run_id)["ship"],
                         {"merge_sha": m, "base": other, "run_tip": self.run_tip()})
        self.assertEqual((self.rev("main"), self.origin_main()), (other, other))

    def test_ship_prepare_refuses_foreign_commit_on_run_branch(self):
        self.merged_run()
        c = git(self.repo, "commit-tree", f"{self.run_tip()}^{{tree}}", "-p", self.run_tip(),
                "-m", "sneaked").stdout.strip()
        git(self.repo, "update-ref", f"refs/heads/factory-run/{self.run_id}", c)
        with self.assertRaisesRegex(fi.ShipRefused, "run branch"):
            self.prepare()

    def test_ship_conflict_with_main_changes_nothing(self):
        self.merged_run(files={"README": "one\nTWO-A\nthree\n"})
        main = self.commit_main({"README": "one\nTWO-M\nthree\n"})
        tip = self.run_tip()
        with self.assertRaises(fi.ShipRefused) as cm:
            self.prepare()
        self.assertIn("README", str(cm.exception))
        self.assertIn("001-a", str(cm.exception))
        self.assertEqual((self.rev("main"), self.run_tip(), self.origin_main()), (main, tip, main))
        self.assertNotIn("ship", fi.load_run(self.home, self.run_id))

    def test_gate_reads_agents_from_default_not_run(self):
        marker, ok, inst = self.t / "marker", self.t / "ok", self.t / "inst"
        self.merged_run(agents=f"# Agents\n- install: touch {inst}\n- test: `touch {ok}`\n",
                        files={"AGENTS.md": f"test: touch {marker}\nbuild: touch {marker}\n"})
        self.prepare()
        res = self.gate()
        self.assertEqual(res["test"]["status"], "pass")
        self.assertEqual(res["test"]["exit"], 0)
        self.assertEqual(res["install"]["status"], "not run")
        self.assertEqual(res["build"]["status"], "not run")
        self.assertEqual(res["scan"], [])
        self.assertTrue(ok.exists())
        self.assertFalse(marker.exists())
        self.assertFalse(inst.exists())
        self.assertEqual(self.gate_dirs(), [])
        self.assertNotIn("factory-gate-", git(self.repo, "worktree", "list").stdout)
        self.assertEqual(self.rev("main"), self.base)
        self.gate(allow_install=True)
        self.assertTrue(inst.exists())

    def test_gate_failing_test_stops(self):
        built = self.t / "built"
        self.merged_run(agents='test: python3 -c "import sys; print(\'boom\'); sys.exit(1)"\n'
                               f"build: touch {built}\n")
        self.prepare()
        with self.assertRaisesRegex(fi.ShipRefused, "test") as cm:
            self.gate()
        d = cm.exception.detail
        self.assertEqual((d["test"]["status"], d["test"]["exit"]), ("fail", 1))
        self.assertIn("boom", d["test"]["tail"])
        self.assertEqual(d["build"]["status"], "not run")
        self.assertFalse(built.exists())
        self.assertEqual(self.gate_dirs(), [])
        self.assertEqual(self.rev("main"), self.base)
        r = self.cli("ship-gate", "--run", self.run_id, "--owner", "o")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)["test"]["status"], "fail")

    def test_gate_timeout_kills_group(self):
        pids = self.t / "pids"
        self.merged_run(agents=f"test: sh -c 'sleep 30 & echo $! > {pids}; "
                               f"echo $$ >> {pids}; sleep 30'\n")
        self.prepare()
        with mock.patch.object(fi, "GATE_TIMEOUT", 2), self.assertRaises(fi.ShipRefused) as cm:
            self.gate()
        self.assertEqual(cm.exception.detail["test"]["status"], "timeout")
        self.assertIsNone(cm.exception.detail["test"]["exit"])
        found = [int(x) for x in pids.read_text().split()]
        self.assertEqual(len(found), 2)

        def alive(pid):
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
            except OSError:
                return False
            return stat.rsplit(")", 1)[1].split()[0] != "Z"
        for _ in range(50):
            if not any(alive(p) for p in found):
                break
            import time
            time.sleep(0.1)
        self.assertFalse([p for p in found if alive(p)])
        self.assertEqual(self.gate_dirs(), [])

    def test_gate_env_scrubbed(self):
        dump = self.t / "env"
        self.merged_run(agents=f"test: env > {dump}\n")
        self.prepare()
        extra = {"AZURE_DEVOPS_EXT_PAT": "x", "FOO_TOKEN": "y", "GIT_ASKPASS": "z", "MY_PAT": "p",
                 "KEEP_ME": "k"}
        with mock.patch.dict(os.environ, extra):
            self.gate()
        keys = {line.split("=", 1)[0] for line in dump.read_text().splitlines()}
        for k in ("AZURE_DEVOPS_EXT_PAT", "FOO_TOKEN", "GIT_ASKPASS", "MY_PAT"):
            self.assertNotIn(k, keys)
        self.assertIn("KEEP_ME", keys)
        self.assertIn("PATH", keys)

    def test_gate_tmpdir_outside_tmp(self):
        dump = self.t / "tmpdir"
        self.merged_run(agents=f'test: echo "$TMPDIR" > {dump} && touch "$TMPDIR/x"\n')
        self.prepare()
        self.gate()
        tmp = Path(dump.read_text().strip())
        self.assertEqual(tmp.name, "tmp")
        self.assertTrue(tmp.parent.name.startswith("factory-gate-"))
        self.assertEqual(tmp.parent.parent, self.t)
        self.assertEqual(self.gate_dirs(), [])

    def test_gate_secret_in_run_stops(self):
        ran = self.t / "ran"
        self.merged_run(agents=f"test: touch {ran}\n")
        # a commit carrying a secret that reached the run branch outside merge-job
        wt = self.t / "wt-leak"
        git(self.repo, "worktree", "add", "-q", "--detach", str(wt), self.run_tip())
        (wt / "leak.py").write_text(f'k = "{SECRET}"\n')
        git(wt, "add", "leak.py")
        git(wt, "commit", "-q", "-m", "leak")
        c = self.rev("HEAD", wt)
        git(self.repo, "worktree", "remove", "--force", str(wt))
        git(self.repo, "update-ref", f"refs/heads/factory-run/{self.run_id}", c)
        run = fi.load_run(self.home, self.run_id)
        run["merged"].append({"job": "002-x", "commit": c})
        fi.save_run(self.home, self.run_id, run, owner="o")
        self.prepare()
        with self.assertRaises(fi.ShipRefused) as cm:
            self.gate()
        scan = cm.exception.detail["scan"]
        self.assertTrue(any(x.startswith("rule 4: C2 leak.py") for x in scan), scan)
        self.assertNotIn(SECRET, json.dumps(cm.exception.detail) + str(cm.exception))
        self.assertFalse(ran.exists())
        self.assertEqual(self.rev("main"), self.base)

    def test_gate_and_local_refuse_when_run_moved_after_prepare(self):
        self.merged_run()
        self.prepare()
        self.make_job("002-b", feature="Other")
        self.assertEqual(self.finish("002-b").status, "merged")
        with self.assertRaisesRegex(fi.ShipRefused, "prepare"):
            self.gate()
        with self.assertRaisesRegex(fi.ShipRefused, "prepare"):
            fi.ship_local(self.home, self.run_id, owner="o")
        self.assertEqual(self.rev("main"), self.base)

    def test_ship_local_needs_gate_pass_for_this_merge(self):
        self.merged_run(agents="test: false\n")
        self.prepare()
        with self.assertRaisesRegex(fi.ShipRefused, "gate has not passed"):
            fi.ship_local(self.home, self.run_id, owner="o")
        with self.assertRaises(fi.ShipRefused):
            self.gate()
        gate = fi.load_run(self.home, self.run_id)["ship"]["gate"]
        self.assertEqual(gate["status"], "fail")
        with self.assertRaisesRegex(fi.ShipRefused, "gate has not passed"):
            fi.ship_local(self.home, self.run_id, owner="o")
        self.assertEqual(self.rev("main"), self.base)
        with self.assertRaises(fj.OwnerMismatch):
            fi.ship_gate(self.home, self.run_id, owner="other", allow_install=False)

    def test_reprepare_after_gate_pass_needs_gate_again(self):
        self.merged_run()
        m1 = self.prepare()["merge_sha"]
        self.gate()
        self.assertEqual(fi.load_run(self.home, self.run_id)["ship"]["gate"]["merge_sha"], m1)
        self.commit_main({"other.txt": "o\n"})  # origin moved: a new merge
        m2 = self.prepare()["merge_sha"]
        self.assertNotEqual(m1, m2)
        self.assertNotIn("gate", fi.load_run(self.home, self.run_id)["ship"])
        with self.assertRaisesRegex(fi.ShipRefused, "gate has not passed"):
            fi.ship_local(self.home, self.run_id, owner="o")
        self.gate()
        self.assertTrue(fi.ship_local(self.home, self.run_id, owner="o").startswith("ff-"))
        self.assertEqual(self.rev("main"), m2)

    def test_gate_reprepared_during_gate_not_recorded(self):
        self.merged_run()
        self.prepare()
        real = fi._gate_run

        def racing(*a):
            res = real(*a)
            self.commit_main({"other.txt": "o\n"})
            self.prepare()
            return res
        with mock.patch.object(fi, "_gate_run", racing):
            with self.assertRaisesRegex(fi.ShipRefused, "re-prepared"):
                self.gate()
        self.assertNotIn("gate", fi.load_run(self.home, self.run_id)["ship"])
        with self.assertRaisesRegex(fi.ShipRefused, "gate has not passed"):
            fi.ship_local(self.home, self.run_id, owner="o")

    def test_gate_checkout_runs_no_global_filter(self):
        marker = self.t / "smudged"
        self.merged_run(files={".gitattributes": "* filter=evil\n", "f.txt": "data\n"})
        self.prepare()
        cfg = self.t / "global.gitconfig"
        cfg.write_text(f'[filter "evil"]\n\tsmudge = "touch {marker}; cat"\n'
                       f'\tclean = cat\n\trequired = true\n')
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg)}):
            # the filter is live for a plain checkout in this setup
            probe = self.t / "probe"
            fp._git(self.repo, "worktree", "add", "--detach", str(probe),
                    f"refs/heads/factory-run/{self.run_id}")
            self.assertTrue(marker.exists())
            fp._git(self.repo, "worktree", "remove", "--force", str(probe))
            marker.unlink()
            self.gate()
        self.assertFalse(marker.exists())

    def test_gate_checkout_runs_no_hook(self):
        self.merged_run()
        self.prepare()
        marker = self.t / "hooked"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        git(self.repo, "checkout", "-q", "-b", "probe")  # hooks live here?
        git(self.repo, "checkout", "-q", "main")
        if not marker.exists():
            self.skipTest("hooks cannot execute in this temp directory")
        marker.unlink()
        self.gate()
        self.assertFalse(marker.exists())

    def test_ship_local_updates_unchecked_out_main_by_cas(self):
        self.merged_run()
        git(self.repo, "checkout", "-q", "--detach")
        m = self.prepare()["merge_sha"]
        self.gate()
        moved = git(self.repo, "commit-tree", f"{self.base}^{{tree}}", "-p", self.base,
                    "-m", "concurrent").stdout.strip()
        real = fi._update_ref

        def racing(repo, ref, new, old):
            git(self.repo, "update-ref", ref, moved)
            return real(repo, ref, new, old)
        with mock.patch.object(fi, "_update_ref", racing), self.assertRaises(fi.ShipRefused):
            fi.ship_local(self.home, self.run_id, owner="o")
        self.assertEqual(self.rev("refs/heads/main"), moved)
        git(self.repo, "update-ref", "refs/heads/main", self.base)
        r = self.cli("ship-local", "--run", self.run_id, "--owner", "o")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["local"], "update-ref")
        self.assertEqual(self.rev("refs/heads/main"), m)
        self.assertEqual(self.origin_main(), self.base)
        out = fi.ship_finish(self.home, self.run_id, owner="o", pushed=False)
        self.assertTrue(out["branch_deleted"])
        self.assertIsNone(fi._resolve(self.repo, f"refs/heads/factory-run/{self.run_id}"))

    def test_ship_local_ff_in_checkout(self):
        self.merged_run()
        m = self.prepare()["merge_sha"]
        self.gate()
        r = fi.ship_local(self.home, self.run_id, owner="o")
        kind, _, path = r.partition(":")
        self.assertEqual(kind, "ff-checkout")
        self.assertEqual(Path(path).resolve(), self.repo.resolve())
        self.assertEqual(self.rev("HEAD"), m)
        self.assertEqual((self.repo / "001-a.txt").read_text(), "x\n")
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")
        base = fi.load_run(self.home, self.run_id)["ship"]["base"]
        self.assertEqual(fp.ship_check(self.repo, default_branch="main", sha=m, base=base,
                                       origin_url=self.url), [])

    def test_ship_local_refuses_dirty_overlap(self):
        self.merged_run(files={"README": "one\nTWO-A\nthree\n"})
        self.prepare()
        self.gate()
        (self.repo / "README").write_text("mine\n")
        with self.assertRaises(fi.ShipRefused):
            fi.ship_local(self.home, self.run_id, owner="o")
        self.assertEqual((self.repo / "README").read_text(), "mine\n")
        self.assertEqual(self.rev("main"), self.base)

    def test_ship_local_refuses_diverged_main(self):
        self.merged_run()
        self.prepare()
        self.gate()
        (self.repo / "local.txt").write_text("l\n")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-q", "-m", "local only")
        local = self.rev("HEAD")
        with self.assertRaisesRegex(fi.ShipRefused, "local main has diverged"):
            fi.ship_local(self.home, self.run_id, owner="o")
        self.assertEqual(self.rev("main"), local)

    def test_ship_twice_refused(self):
        self.merged_run()
        m = self.prepare()["merge_sha"]
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        fi.ship_finish(self.home, self.run_id, owner="o", pushed=False)
        with self.assertRaisesRegex(fi.ShipRefused, "already shipped"):
            self.prepare()
        with self.assertRaisesRegex(fi.ShipRefused, "already shipped"):
            self.gate()
        with self.assertRaisesRegex(fi.ShipRefused, "already shipped"):
            fi.ship_local(self.home, self.run_id, owner="o")
        with self.assertRaisesRegex(fi.ShipRefused, "already shipped"):
            fi.ship_finish(self.home, self.run_id, owner="o", pushed=True)
        self.assertEqual(self.rev("main"), m)
        self.assertEqual(fi.load_run(self.home, self.run_id)["shipped"]["pushed"], False)
        r = self.cli("ship-prepare", "--run", self.run_id, "--owner", "o")
        self.assertEqual(r.returncode, 2)

    def test_finish_job_on_shipped_run_holds_as_refused(self):
        self.merged_run()
        self.prepare()
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        fi.ship_finish(self.home, self.run_id, owner="o", pushed=False)
        self.make_job("002-b", feature="Late")
        r = self.cli("finish-job", "--run", self.run_id, "--job", "002-b", "--owner", "o")
        self.assertEqual(r.returncode, 2, r)
        job = self.job("002-b")
        self.assertEqual((job["stage"], job["hold"]["kind"]), ("held", "refused"))
        self.assertIn("run shipped", job["hold"]["reasons"])
        self.assertEqual(fi.load_run(self.home, self.run_id)["held"], [])
        with self.assertRaises(fj.OwnerMismatch):
            fi.finish_job(self.home, self.run_id, "002-b", owner="other")

    def test_ship_finish_marks_jobs_and_deletes_run_branch(self):
        self.start()
        self.make_job("001-a")
        self.make_job("002-b", feature="Other")
        self.make_job("003-c", feature="Held", draft=True)
        self.make_job("004-d", feature="Fourth")
        lines = [f"- [~] {self.repo} :: {f} → {j}" for j, f in
                 (("001-a", "Add a thing"), ("002-b", "Other"), ("003-c", "Held"),
                  ("004-d", "Fourth"))]
        (self.home / "backlog.md").write_text("\n".join(lines) + "\n")
        for j in ("001-a", "002-b", "004-d"):
            self.assertEqual(self.finish(j).status, "merged")
        self.assertEqual(self.finish("003-c").status, "held")
        m = self.prepare()["merge_sha"]
        msg = git(self.repo, "log", "-1", "--format=%B", m).stdout
        self.assertIn("002-b: Other", msg)
        self.assertNotIn("003-c", msg)
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        fj.takeover(self.home / "jobs" / "004-d", "o2", reason="elsewhere")
        r = self.cli("ship-finish", "--run", self.run_id, "--owner", "o", "--pushed", "yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["jobs"], ["001-a", "002-b"])
        self.assertEqual([s["job"] for s in out["skipped"]], ["004-d"])
        self.assertEqual([self.job(j)["stage"] for j in ("001-a", "002-b", "003-c", "004-d")],
                         ["shipped", "shipped", "held", "merged"])
        self.assertIsNone(fi._resolve(self.repo, f"refs/heads/factory-run/{self.run_id}"))
        shipped = fi.load_run(self.home, self.run_id)["shipped"]
        self.assertEqual((shipped["merge_sha"], shipped["pushed"]), (m, True))
        self.assertIn("at", shipped)
        items = {it.job_id: it for it in fb.parse((self.home / "backlog.md").read_text())}
        self.assertEqual((items["001-a"].state, items["001-a"].stage), ("x", "shipped"))
        self.assertEqual(items["002-b"].state, "x")
        self.assertEqual(items["003-c"].state, "~")
        self.assertEqual(self.rev("main"), m)

    # --- final-review fixes ---------------------------------------------------

    def test_reship_after_local_resumes_without_new_merge(self):
        self.merged_run()
        out = self.prepare()
        m = out["merge_sha"]
        self.assertEqual(out["origin_moved"], 0)
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        # The coordinator dies here; the user runs /factory ship again.
        with self.assertRaisesRegex(fi.ShipRefused,
                                    "local main already moved to the ship merge"):
            self.prepare()
        self.assertEqual(fi.load_run(self.home, self.run_id)["ship"]["merge_sha"], m)
        out = fi.ship_finish(self.home, self.run_id, owner="o", pushed=False)
        self.assertEqual(out["jobs"], ["001-a"])
        self.assertEqual(self.rev("main"), m)

    def test_reship_after_push_makes_no_second_merge(self):
        self.merged_run()
        m = self.prepare()["merge_sha"]
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        git(self.repo, "push", "-q", "origin", "main")
        run = fi.load_run(self.home, self.run_id)
        run["ship"].pop("local")  # moved, but the coordinator died before recording it
        fi.save_run(self.home, self.run_id, run, owner="o")
        r = self.cli("ship-prepare", "--run", self.run_id, "--owner", "o")
        self.assertEqual(r.returncode, 2, r)
        self.assertIn("already moved to the ship merge", r.stderr)
        detail = json.loads(r.stdout)
        self.assertEqual((detail["merge_sha"], detail["origin_has_merge"]), (m, True))
        self.assertEqual(fi.load_run(self.home, self.run_id)["ship"]["merge_sha"], m)
        self.assertEqual(git(self.repo, "rev-list", "--merges", "--count", "main").stdout.strip(),
                         "1")
        self.assertEqual(self.origin_main(), m)
        fi.ship_finish(self.home, self.run_id, owner="o", pushed=True)

    def ship_to_local(self):
        self.merged_run()
        m = self.prepare()["merge_sha"]
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        return m

    def status_cli(self):
        r = self.cli("ship-status", "--run", self.run_id)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_ship_status_not_pushed(self):
        self.merged_run()
        with self.assertRaisesRegex(fi.ShipRefused, "ship not prepared"):
            fi.ship_status(self.home, self.run_id)
        m = self.prepare()["merge_sha"]
        self.assertEqual(self.status_cli(), {"mode": "ado", "merge_sha": m, "base": self.base, "local": False,
                                             "gate": None, "origin_has_merge": False})
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        self.assertEqual(self.status_cli(), {"mode": "ado", "merge_sha": m, "base": self.base, "local": True,
                                             "gate": "pass", "origin_has_merge": False})
        with mock.patch.object(fp, "server_head", side_effect=fp.GitError("x")):
            st = self.status_cli()  # origin unreadable
        self.assertEqual((st["origin_has_merge"], st["origin_error"]), (None, "x"))

    def test_ship_finish_refuses_ungated_merge(self):
        self.merged_run()
        m = self.prepare()["merge_sha"]
        fp._git(self.repo, "update-ref", "refs/heads/main", m)  # D moved, never gated
        with self.assertRaisesRegex(fi.ShipRefused, "gate has not passed for this merge"):
            fi.ship_finish(self.home, self.run_id, owner="o", pushed=False)
        self.assertIsNone(fi.load_run(self.home, self.run_id)["shipped"])
        self.gate()  # the gate still runs after D moved; then finish is allowed
        self.assertEqual(fi.ship_finish(self.home, self.run_id, owner="o",
                                        pushed=False)["jobs"], ["001-a"])

    def test_ship_status_null_on_guard_refusal(self):
        self.ship_to_local()
        git(self.repo, "config", "credential.helper", "!echo x")
        st = self.status_cli()
        self.assertIsNone(st["origin_has_merge"])
        self.assertIn("rule 6: config credential.helper", st["origin_error"])

    def test_ship_status_after_push_then_crash(self):
        m = self.ship_to_local()
        git(self.repo, "push", "-q", "origin", "main")  # the push succeeded; the coordinator died
        st = self.status_cli()
        self.assertEqual((st["local"], st["origin_has_merge"]), (True, True))
        out = fi.ship_finish(self.home, self.run_id, owner="o", pushed=st["origin_has_merge"])
        self.assertEqual(out["shipped"]["pushed"], True)
        self.assertEqual(self.origin_main(), m)

    def test_ship_status_origin_moved_past_merge_not_fetched(self):
        m = self.ship_to_local()
        git(self.repo, "push", "-q", "origin", "main")
        other = self.t / "other"
        subprocess.run(["git", "clone", "-q", self.url, str(other)], check=True,
                       capture_output=True)
        (other / "o.txt").write_text("o\n")
        git(other, "add", "o.txt")
        git(other, "-c", "user.name=T", "-c", "user.email=t@e.com", "commit", "-q", "-m", "o")
        git(other, "push", "-q", "origin", "HEAD:main")
        tip = self.origin_main()
        self.assertNotEqual(tip, m)
        self.assertFalse(fp.has_local_commit(self.repo, tip))  # not fetched locally
        r = self.cli("ship-prepare", "--run", self.run_id, "--owner", "o")
        self.assertEqual(r.returncode, 2, r)
        self.assertEqual(json.loads(r.stdout)["origin_has_merge"], True)
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        git(self.repo, "reflog", "expire", "--expire=now", "--all")
        git(self.repo, "prune", "--expire=now")
        self.assertFalse(fp.has_local_commit(self.repo, tip))

        def refs():
            out = git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout
            return [x for x in out.splitlines() if not x.startswith("refs/remotes/origin/main ")]
        before = refs()
        self.assertEqual(self.status_cli()["origin_has_merge"], True)
        self.assertEqual(refs(), before)  # only refs/remotes/origin/main may change

    def test_review_head_from_older_round_refused(self):
        self.start()
        jd = self.make_job()
        self.assertEqual(self.job()["review_round"], 1)
        self.write_round(jd, 2, False)
        fj.update_job(jd, {"round": 2}, owner="o")  # rework round bumped, no mark-review yet
        r = self.finish()
        self.assertEqual(r.status, "refused")
        self.assertEqual(r.reasons, ["review head is from another round; rework the job"])
        self.assertEqual(self.run_tip(), self.base)
        fi.mark_review(self.home, "001-a", owner="o")
        self.assertEqual(self.job()["review_round"], 2)
        self.assertEqual(self.finish().status, "merged")

    def test_review_head_without_review_round_refused(self):
        self.start()
        jd = self.make_job()
        data = json.loads((jd / "job.json").read_text())
        data.pop("review_round")  # a review head recorded before review_round existed
        (jd / "job.json").write_text(json.dumps(data))
        r = self.finish()
        self.assertEqual((r.status, r.reasons),
                         ("refused", ["review round not recorded; rework the job"]))
        self.assertEqual(self.run_tip(), self.base)
        self.assertEqual(self.job()["hold"]["reasons"],
                         ["review round not recorded; rework the job"])

    def test_run_branch_moved_outside_factory_refused(self):
        self.start()
        self.make_job()
        forged = git(self.repo, "commit-tree", f"{self.base}^{{tree}}", "-p", self.base,
                     "-m", "worker").stdout.strip()
        git(self.repo, "update-ref", f"refs/heads/factory-run/{self.run_id}", forged)
        r = self.finish()
        self.assertEqual(r.status, "refused")
        self.assertIn("run branch moved outside the factory", r.reasons)
        self.assertEqual(self.run_tip(), forged)
        self.assertEqual(fi.load_run(self.home, self.run_id)["merged"], [])

    def test_run_branch_moved_after_a_merge_refused(self):
        self.start()
        self.make_job("001-a")
        self.make_job("002-b", feature="Other")
        self.assertEqual(self.finish("001-a").status, "merged")
        tip = self.run_tip()
        forged = git(self.repo, "commit-tree", f"{tip}^{{tree}}", "-p", tip,
                     "-m", "x\n\nFactory-Job: 002-b\nFactory-Round: 1\n").stdout.strip()
        git(self.repo, "update-ref", f"refs/heads/factory-run/{self.run_id}", forged)
        r = self.finish("002-b")
        self.assertEqual(r.status, "refused")
        self.assertIn("run branch moved outside the factory", r.reasons)
        self.assertEqual(self.run_tip(), forged)

    def test_merge_retry_after_crash_records_existing_commit(self):
        self.start()
        self.make_job()
        with mock.patch.object(fi, "save_run", side_effect=RuntimeError("crash")), \
                self.assertRaises(RuntimeError):
            self.finish()
        tip = self.run_tip()
        self.assertNotEqual(tip, self.base)
        self.assertEqual(fi.load_run(self.home, self.run_id)["merged"], [])
        r = self.finish()
        self.assertEqual((r.status, r.commit), ("merged", tip))
        self.assertEqual(self.run_tip(), tip)
        self.assertEqual(fi.load_run(self.home, self.run_id)["merged"],
                         [{"job": "001-a", "commit": tip}])
        self.assertEqual(self.job()["merge_commit"], tip)

    def test_no_merge_into_run_mid_ship(self):
        self.merged_run()
        m = self.prepare()["merge_sha"]
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        tip = self.rev(f"factory-run/{self.run_id}")
        self.make_job("002-b", feature="Late")
        r = self.finish("002-b")
        self.assertEqual((r.status, r.reasons), ("refused", ["run is being shipped"]))
        job = self.job("002-b")
        self.assertEqual((job["stage"], job["hold"]["kind"]), ("held", "refused"))
        self.assertEqual(self.rev(f"factory-run/{self.run_id}"), tip)
        out = fi.ship_finish(self.home, self.run_id, owner="o", pushed=False)
        self.assertEqual((out["jobs"], out["skipped"]), (["001-a"], []))
        self.assertEqual(self.rev("main"), m)
        # After the ship, rework moves the held job to a newer run (no --force-move needed).
        new = "20261017-0900-beef"
        fs_init = subprocess.run([sys.executable, str(ROOT / "scripts/factory_schedule.py"),
                                  "init", "--run", str(fi.runs_dir(self.home) / f"{new}.json")],
                                 capture_output=True, text=True)
        self.assertEqual(fs_init.returncode, 0, fs_init.stderr)
        fi.start_run(self.home, new, repo=self.repo, default_branch="main",
                     origin_url=self.url, owner="o")
        job = fi.reassign(self.home, "002-b", new, owner="o")
        self.assertEqual((job["run"], job["hold"]), (new, None))

    def test_mid_ship_guard_fails_closed_without_local_default(self):
        self.merged_run()
        self.prepare()
        run = fi.load_run(self.home, self.run_id)
        run["default_branch"] = "nope"
        self.assertTrue(fi._shipping(run))

    def test_no_merge_into_run_after_unrecorded_local_move(self):
        self.merged_run()
        self.prepare()
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        run = fi.load_run(self.home, self.run_id)
        run["ship"].pop("local")  # D moved, but the coordinator died before recording it
        fi.save_run(self.home, self.run_id, run, owner="o")
        self.make_job("002-b", feature="Late")
        r = self.finish("002-b")
        self.assertEqual((r.status, r.reasons), ("refused", ["run is being shipped"]))

    def test_ship_finish_skips_job_merged_after_prepare(self):
        self.merged_run()
        m = self.prepare()["merge_sha"]
        self.make_job("002-b", feature="Late")
        self.assertEqual(self.finish("002-b").status, "merged")  # before local: allowed
        run = fi.load_run(self.home, self.run_id)
        run["ship"]["local"] = "update-ref"  # simulate a ship that moved D before 002-b
        run["ship"]["gate"] = {"merge_sha": m, "status": "pass"}
        fi.save_run(self.home, self.run_id, run, owner="o")
        fp._git(self.repo, "update-ref", "refs/heads/main", m)
        out = fi.ship_finish(self.home, self.run_id, owner="o", pushed=False)
        self.assertEqual(out["jobs"], ["001-a"])
        self.assertEqual(out["skipped"], [{"job": "002-b", "reason": "not in the ship merge"}])
        self.assertEqual((self.job("001-a")["stage"], self.job("002-b")["stage"]),
                         ("shipped", "merged"))

    def test_remote_timeout_is_clean_error(self):
        real = subprocess.run

        def slow(cmd, *a, **k):
            if "ls-remote" in cmd or "fetch" in cmd:
                self.assertEqual(k.get("timeout"), fp.REMOTE_TIMEOUT)
                raise subprocess.TimeoutExpired(cmd, k["timeout"])
            return real(cmd, *a, **k)
        with mock.patch.object(fp.subprocess, "run", slow):
            r = self.cli("start-run", "--repo", str(self.repo), "--run", self.run_id,
                         "--default", "main", "--origin-url", self.url, "--owner", "o")
        self.assertEqual(r.returncode, 1, r)
        self.assertIn("timed out", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def rewrite_env(self, key):
        other = self.t / f"other-{key}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
        cfg = self.t / f"gitconfig-{key}"
        cfg.write_text(f'[url "{other}"]\n\t{key} = {self.url}\n')
        return other, mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg)})

    def test_start_run_and_ship_prepare_refuse_rewritten_origin(self):
        other, env = self.rewrite_env("insteadOf")
        with env, self.assertRaisesRegex(fi.Refused,
                                         r"origin url is rewritten by config \(fetch\)"):
            self.start()
        self.assertIsNone(fi._resolve(self.repo, f"refs/heads/factory-run/{self.run_id}"))
        self.merged_run()
        for key in ("insteadOf", "pushInsteadOf"):
            other, env = self.rewrite_env(key)
            with env, self.assertRaisesRegex(fi.ShipRefused, "origin url is rewritten by config"):
                self.prepare()
            self.assertEqual(git(other, "for-each-ref").stdout, "")
        self.assertNotIn("ship", fi.load_run(self.home, self.run_id))

    def test_start_run_ignores_injected_git_config_env(self):
        bad = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "url./nonexistent/x.git.insteadOf",
               "GIT_CONFIG_VALUE_0": self.url,
               "GIT_CONFIG_PARAMETERS": "'url./nonexistent/y.git.insteadof'='" + self.url + "'"}
        with mock.patch.dict(os.environ, bad):
            run = self.start()
        self.assertEqual(run["base"], self.base)


    # --- rebase-job (a conflict hold restarted on the new run's base) ----------

    def conflict_after_ship(self):
        """001-a merged and shipped (origin main holds it), 002-b held by conflict, moved to a
        new run started on the new origin main. Returns the new run id."""
        self.start()
        self.make_job("001-a", files={"README": "one\nTWO-A\nthree\n"})
        self.make_job("002-b", feature="Other", files={"README": "one\nTWO-B\nthree\n"})
        self.assertEqual(self.finish("001-a").status, "merged")
        self.assertEqual(self.finish("002-b").status, "conflict")
        git(self.repo, "push", "-q", "origin", f"factory-run/{self.run_id}:main")
        run = fi.load_run(self.home, self.run_id)
        run["shipped"] = {"merge_sha": self.run_tip(), "pushed": True, "at": "now"}
        fi.save_run(self.home, self.run_id, run, owner="o")
        new = "20261017-0900-beef"
        self.start(new)
        job = fi.reassign(self.home, "002-b", new, owner="o")
        self.assertEqual((job["stage"], job["hold"]["kind"]), ("held", "conflict"))
        self.assertEqual(job["moved_from"], self.run_id)
        return new

    def test_rebase_job_restarts_branch_on_run_base(self):
        new = self.conflict_after_ship()
        old_tip = self.rev("factory/002-b")
        old_base = self.job("002-b")["base"]
        run_base = fi.load_run(self.home, new)["base"]
        self.assertNotEqual(run_base, old_base)
        r = self.cli("rebase-job", "--job", "002-b", "--owner", "o")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        job = self.job("002-b")
        self.assertEqual(job["base"], run_base)
        self.assertIsNone(job["review_head"])
        self.assertIsNone(job["review_round"])
        self.assertIsNone(job["hold"])
        self.assertEqual(job["rebased"]["from_base"], old_base)
        self.assertEqual(job["rebased"]["old_tip"], old_tip)
        self.assertEqual(job["rebased"]["backup_ref"], "refs/heads/factory/002-b-r1")
        self.assertEqual(job["rebased"]["round"], 1)
        self.assertEqual(out["rebased"], job["rebased"])
        self.assertEqual(self.rev("refs/heads/factory/002-b-r1"), old_tip)
        wt = self.t / "wt-002-b"
        self.assertEqual(self.rev("HEAD", cwd=wt), run_base)
        self.assertEqual(git(wt, "symbolic-ref", "--short", "HEAD").stdout.strip(),
                         "factory/002-b")
        self.assertEqual(git(wt, "status", "--porcelain").stdout, "")
        # A new commit re-applying the change on the new base merges cleanly.
        (wt / "README").write_text("one\nTWO-A\nTWO-B\nthree\n")
        git(wt, "add", "README")
        git(wt, "commit", "-q", "-m", "re-apply")
        jd = self.home / "jobs" / "002-b"
        fj.update_job(jd, {"stage": "verify", "round": 2}, owner="o")
        self.write_round(jd, 2, False)
        fi.mark_review(self.home, "002-b", owner="o")
        r = self.finish("002-b", run_id=new)
        self.assertEqual(r.status, "merged", r.reasons)
        job = self.job("002-b")  # a later rework must not take the rebase as current
        self.assertNotEqual(job["rebased"]["round"], job["round"])
        self.assertEqual(git(self.repo, "show", f"{self.run_tip(new)}:README").stdout,
                         "one\nTWO-A\nTWO-B\nthree\n")

    def test_rebase_job_refusals(self):
        new = self.conflict_after_ship()
        jd = self.home / "jobs" / "002-b"
        wt = self.t / "wt-002-b"
        tip = self.rev("factory/002-b")

        def refused(pattern):
            with self.assertRaisesRegex(fi.Refused, pattern):
                fi.rebase_job(self.home, "002-b", owner="o")
            self.assertEqual(self.rev("factory/002-b"), tip)
            self.assertEqual(self.rev("HEAD", cwd=wt), tip)

        with self.assertRaises(fj.OwnerMismatch):
            fi.rebase_job(self.home, "002-b", owner="other")
        # Dirty worktree.
        (wt / "stray.txt").write_text("x\n")
        refused("not clean")
        (wt / "stray.txt").unlink()
        # Backup ref already exists.
        git(self.repo, "branch", "factory/002-b-r1", self.base)
        refused("exists")
        git(self.repo, "branch", "-D", "factory/002-b-r1")
        self.assertIsNone(fi._resolve(self.repo, "refs/heads/factory/002-b-r1"))
        # Not a conflict hold.
        hold = self.job("002-b")["hold"]
        fj.update_job(jd, {"hold": dict(hold, kind="draft")}, owner="o")
        refused("conflict hold")
        fj.update_job(jd, {"hold": hold, "stage": "build"}, owner="o")
        refused("conflict hold")
        fj.update_job(jd, {"stage": "held"}, owner="o")
        # Base already equal to the run's base.
        run_base = fi.load_run(self.home, new)["base"]
        fj.update_job(jd, {"base": run_base}, owner="o")
        refused("already")
        fj.update_job(jd, {"base": self.base}, owner="o")
        # Run shipped, or not started.
        fj.update_job(jd, {"run": self.run_id}, owner="o")
        refused("shipped")
        path = fi.runs_dir(self.home) / "20261017-1000-cafe.json"
        path.write_text(json.dumps({"owner": "o", "caps": {}, "pending": [], "running": []}))
        fj.update_job(jd, {"run": "20261017-1000-cafe"}, owner="o")
        refused("not started")
        fj.update_job(jd, {"run": new}, owner="o")
        r = self.cli("rebase-job", "--job", "002-b", "--owner", "o")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("rebase-job", "--job", "002-b", "--owner", "o")
        self.assertEqual(r.returncode, 2)
        self.assertIn("refused:", r.stderr)

    def test_rebase_job_runs_no_global_filter(self):
        new = self.conflict_after_ship()
        marker = self.t / "smudged"
        attrs = self.t / "attributes"
        attrs.write_text("* filter=evil\n")
        cfg = self.t / "global.gitconfig"
        cfg.write_text(f'[filter "evil"]\n\tsmudge = "touch {marker}; cat"\n'
                       f'\tclean = "touch {marker}; cat"\n\trequired = true\n'
                       f'[core]\n\tattributesFile = {attrs}\n')
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(cfg)}):
            self.assertEqual(fp.config_violations(self.t / "wt-002-b", self.url), [])
            fi.rebase_job(self.home, "002-b", owner="o")
        self.assertFalse(marker.exists())
        self.assertEqual(self.rev("HEAD", cwd=self.t / "wt-002-b"),
                         fi.load_run(self.home, new)["base"])

    def test_rebase_job_resumes_after_partial_run(self):
        new = self.conflict_after_ship()
        tip = self.rev("factory/002-b")
        run_base = fi.load_run(self.home, new)["base"]
        # Backup written, switch never happened: a retry continues.
        git(self.repo, "update-ref", "refs/heads/factory/002-b-r1", tip)
        # Switch done, job record not written: a retry only writes the record.
        with mock.patch.object(fj, "update_job", side_effect=RuntimeError("crash")), \
                self.assertRaises(RuntimeError):
            fi.rebase_job(self.home, "002-b", owner="o")
        self.assertEqual(self.rev("factory/002-b"), run_base)
        out = fi.rebase_job(self.home, "002-b", owner="o")
        self.assertEqual((out["base"], out["rebased"]["old_tip"]), (run_base, tip))
        self.assertEqual(self.rev("refs/heads/factory/002-b-r1"), tip)

    def test_rebase_job_requires_conflicting_jobs_in_new_base(self):
        new = self.conflict_after_ship()
        jd = self.home / "jobs" / "002-b"
        tip = self.rev("factory/002-b")
        fj.update_job(jd, {"moved_from": None}, owner="o")
        with self.assertRaisesRegex(fi.Refused, "not in the new base yet"):
            fi.rebase_job(self.home, "002-b", owner="o")
        fj.update_job(jd, {"moved_from": self.run_id}, owner="o")
        run = fi.load_run(self.home, self.run_id)
        shipped = run["shipped"]
        run["shipped"] = dict(shipped, merge_sha=tip)  # not reachable from the new base
        fi.save_run(self.home, self.run_id, run, owner="o")
        with self.assertRaisesRegex(fi.Refused, "not in the new base yet"):
            fi.rebase_job(self.home, "002-b", owner="o")
        run["shipped"] = None
        fi.save_run(self.home, self.run_id, run, owner="o")
        with self.assertRaisesRegex(fi.Refused, "not in the new base yet"):
            fi.rebase_job(self.home, "002-b", owner="o")
        run["shipped"] = shipped
        fi.save_run(self.home, self.run_id, run, owner="o")
        self.assertEqual(self.rev("factory/002-b"), tip)
        fi.rebase_job(self.home, "002-b", owner="o")
        self.assertEqual(self.rev("factory/002-b"), fi.load_run(self.home, new)["base"])

    def test_rebase_job_more_refusals(self):
        new = self.conflict_after_ship()
        jd = self.home / "jobs" / "002-b"
        wt = self.t / "wt-002-b"
        tip = self.rev("factory/002-b")
        git(wt, "switch", "-q", "--detach")
        with self.assertRaisesRegex(fi.Refused, "not on factory/002-b"):
            fi.rebase_job(self.home, "002-b", owner="o")
        git(wt, "switch", "-q", "factory/002-b")
        repo = self.job("002-b")["repo"]
        fj.update_job(jd, {"repo": str(self.t)}, owner="o")
        with self.assertRaisesRegex(fi.Refused, "repo differs"):
            fi.rebase_job(self.home, "002-b", owner="o")
        fj.update_job(jd, {"repo": repo}, owner="o")
        base = self.job("002-b")["base"]
        fj.update_job(jd, {"base": tip}, owner="o")  # not an ancestor of the run base
        with self.assertRaisesRegex(fi.Refused, "not older than the run base"):
            fi.rebase_job(self.home, "002-b", owner="o")
        fj.update_job(jd, {"base": base}, owner="o")
        run = fi.load_run(self.home, new)
        fi.takeover_run(self.home, new, "other", reason="test")
        with self.assertRaises(fj.OwnerMismatch):
            fi.rebase_job(self.home, "002-b", owner="o")
        fi.takeover_run(self.home, new, "o", reason="test")
        self.assertEqual(self.rev("factory/002-b"), tip)
        self.assertIsNone(fi._resolve(self.repo, "refs/heads/factory/002-b-r1"))
        self.assertEqual(run["base"], fi.load_run(self.home, new)["base"])


class LocalIntegrateTest(unittest.TestCase):
    """Local-mode runs: a repo with no origin (or an untrusted one) never fetches or pushes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=_tmp_parent())
        self.addCleanup(self._tmp.cleanup)
        self.t = t = Path(self._tmp.name)
        self.home = t / "home"
        (self.home / "jobs").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.home), "FACTORY_TMPDIR": str(t)})
        env.start()
        self.addCleanup(env.stop)
        tmpdir = mock.patch.object(tempfile, "tempdir", str(t))
        tmpdir.start()
        self.addCleanup(tmpdir.stop)
        self.url = None
        self.repo = t / "repo"
        git(t, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "config", "user.email", "t@example.com")
        (self.repo / "README").write_text("one\ntwo\nthree\n")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "-q", "-m", "init")
        self.base = self.rev("HEAD")
        self.run_id = "20261016-1200-abcd"

    rev = IntegrateTest.rev
    run_tip = IntegrateTest.run_tip
    make_job = IntegrateTest.make_job
    write_round = IntegrateTest.write_round
    finish = IntegrateTest.finish
    job = IntegrateTest.job
    cli = IntegrateTest.cli
    prepare = IntegrateTest.prepare
    gate = IntegrateTest.gate

    def start(self, run_id=None):
        return fi.start_run(self.home, run_id or self.run_id, repo=self.repo,
                            default_branch="main", origin_url=self.url, owner="o", local=True)

    def commit_main(self, files, msg="main change"):
        for name, text in files.items():
            (self.repo / name).write_text(text)
            git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", msg)
        return self.rev("HEAD")

    def test_start_run_bases_on_local_main(self):
        with mock.patch.object(fp, "remote_refs", side_effect=AssertionError("remote")):
            run = self.start()
        self.assertEqual((run["mode"], run["base"], run["origin_url"]),
                         ("local", self.base, None))
        self.assertEqual(self.run_tip(), self.base)
        self.assertFalse((fi.runs_dir(self.home) / f"{self.run_id}.refs-before.json").exists())

    def test_start_run_cli_requires_local_without_origin(self):
        a = ("start-run", "--repo", str(self.repo), "--run", self.run_id, "--default", "main",
             "--owner", "o")
        r = self.cli(*a)
        self.assertEqual(r.returncode, 2)
        self.assertIn("origin url not allowed", r.stderr)
        r = self.cli(*a, "--local")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["mode"], "local")

    def test_start_run_refuses_origin_not_recorded(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/x/y.git")
        with self.assertRaisesRegex(fi.Refused, "run recorded no origin"):
            self.start()
        self.url = "https://github.com/x/y.git"
        self.assertEqual(self.start()["origin_url"], self.url)

    def test_local_run_merges_and_ships_without_push(self):
        self.start()
        jd = self.make_job()
        fp.disable_push(self.repo, Path(self.job()["worktree"]))
        with mock.patch.object(fp, "remote_refs", side_effect=AssertionError("remote")):
            self.assertEqual(self.finish().status, "merged")
        other = self.commit_main({"other.txt": "o\n"})  # local main moved after run start
        with mock.patch.object(fp, "server_head", side_effect=AssertionError("remote")):
            out = self.prepare()
        self.assertEqual((out["base"], out["origin_moved"]), (other, 1))
        st = fi.ship_status(self.home, self.run_id)
        self.assertEqual((st["mode"], st["origin_has_merge"], st["origin_error"]),
                         ("local", None, "local run: no origin"))
        self.gate()
        fi.ship_local(self.home, self.run_id, owner="o")
        self.assertEqual(self.rev("main"), out["merge_sha"])
        with self.assertRaisesRegex(fi.ShipRefused, "nothing was pushed"):
            fi.ship_finish(self.home, self.run_id, owner="o", pushed=True)
        res = fi.ship_finish(self.home, self.run_id, owner="o", pushed=False)
        self.assertEqual((res["jobs"], res["shipped"]["pushed"]), (["001-a"], False))
        self.assertEqual(fj.read_job(jd)["stage"], "shipped")

    def test_local_merge_refuses_origin_added_by_worker(self):
        self.start()
        self.make_job()
        git(self.repo, "remote", "add", "origin", "https://evil.example/x.git")
        res = self.finish()
        self.assertEqual((res.status, self.job()["stage"]), ("refused", "held"))
        self.assertIn("rule 6: remote.origin.url set but the run recorded no origin",
                      res.reasons)

    def test_local_ship_conflict_names_local_branch(self):
        self.start()
        self.make_job(files={"README": "one\nTWO-A\nthree\n"})
        self.assertEqual(self.finish().status, "merged")
        self.commit_main({"README": "one\nTWO-M\nthree\n"})
        with self.assertRaisesRegex(fi.ShipRefused, r"conflict with main in README"):
            self.prepare()

    def test_local_overlap_label_makes_draft(self):
        self.start()
        jd = self.make_job()
        fj.update_job(jd, {"overlaps": [{"with": "local/main", "paths": ["README"]}]}, owner="o")
        self.assertEqual(self.finish().status, "held")

    def test_start_run_refuses_other_repos_home(self):
        other = self.t / "other" / ".factory"
        with self.assertRaisesRegex(fi.Refused, "belongs to"):
            fi.start_run(other, self.run_id, repo=self.repo, default_branch="main",
                         origin_url=None, owner="o", local=True)
        own = self.repo / ".factory"
        self.assertEqual(fi.start_run(own, self.run_id, repo=self.repo, default_branch="main",
                                      origin_url=None, owner="o", local=True)["mode"], "local")

if __name__ == "__main__":
    unittest.main()
