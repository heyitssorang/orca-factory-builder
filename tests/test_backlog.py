import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_backlog as fb  # noqa: E402
import factory_job as fj  # noqa: E402

CLI = str(ROOT / "scripts" / "factory_backlog.py")


class BacklogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        self.home = self.repo / ".factory"
        self.jobs = fj.jobs_dir(self.home)
        self.jobs.mkdir(parents=True)
        self.bl = self.home / "backlog.md"

    def mkjob(self, job_id, stage="spec", updated=None):
        d = self.jobs / job_id
        d.mkdir()
        job = fj.new_job(d, feature="f", repo="/r", owner="o")
        job["stage"] = stage
        if updated:
            job["updated"] = updated.isoformat(timespec="seconds")
        fj._write(d, job)

    def cli(self, *args):
        env = dict(os.environ, FACTORY_HOME=str(self.home))
        return subprocess.run([sys.executable, CLI, *args], env=env,
                              capture_output=True, text=True)

    def test_parse_spaces_and_markers(self):
        it = fb.parse('- [~] Use a::b → c after:001-x default:main → 004-use-a-b\n')[0]
        self.assertEqual((it.feature, it.after, it.default, it.job_id),
                         ("Use a::b → c", "001-x", "main", "004-use-a-b"))
        self.assertEqual(it.state, "~")
        self.assertEqual(it.line_no, 1)

    def test_parse_four_digit_job_marker(self):
        it = fb.parse("- [x] Big → 1000-big-job merged\n")[0]
        self.assertEqual((it.feature, it.job_id, it.stage), ("Big", "1000-big-job", "merged"))

    def test_parse_stage_and_ignored_lines(self):
        items = fb.parse("# title\n- [x] F → 003-f merged\nnot an item\n- [ ] G\n")
        self.assertEqual([i.line_no for i in items], [2, 4])
        self.assertEqual((items[0].job_id, items[0].stage), ("003-f", "merged"))
        self.assertEqual((items[1].job_id, items[1].stage), (None, None))

    def test_install_quoted(self):
        it = fb.parse('- [ ] Feat after:001-a install:"npm ci --prefer-offline"\n')[0]
        self.assertEqual(it.install, "npm ci --prefer-offline")
        self.assertEqual((it.feature, it.after), ("Feat", "001-a"))

    def test_work_item_option(self):
        it = fb.parse('- [ ] Feat wi:1234 after:001-a install:"npm ci"\n')[0]
        self.assertEqual((it.feature, it.work_item, it.after, it.install),
                         ("Feat", 1234, "001-a", "npm ci"))
        self.assertIsNone(fb.parse("- [ ] Feat\n")[0].work_item)
        bad = fb.parse("- [ ] Feat wi:abc\n")[0]
        self.assertEqual((bad.feature, bad.work_item), ("Feat wi:abc", None))

    def test_claim_records_work_item(self):
        self.bl.write_text("- [ ] Feat default:main wi:77\n")
        item, job_id = fb.claim(self.home, "o")
        self.assertEqual(item.work_item, 77)
        self.assertEqual(fj.read_job(self.jobs / job_id)["work_item"], 77)
        self.assertIn("wi:77 → " + job_id, self.bl.read_text())
        r = self.cli("add", "Other wi:5")
        got = json.loads(self.cli("claim", "--owner", "o").stdout)
        self.assertEqual((got["feature"], got["work_item"]), ("Other", 5))
        self.assertEqual(fj.read_job(self.jobs / got["job_id"])["work_item"], 5)

    def test_add_creates_files_and_returns_line(self):
        fresh = self.home / "new"
        self.assertEqual(fb.add(fresh, "One"), 1)
        self.assertEqual(fb.add(fresh, " Two "), 2)
        self.assertEqual((fresh / "backlog.md").read_text(), "- [ ] One\n- [ ] Two\n")
        self.assertTrue((fresh / "backlog.md.lock").exists())
        self.assertEqual((fresh / ".gitignore").read_text(), fj.HOME_GITIGNORE)

    def test_claim_rewrites_line_and_creates_job(self):
        self.bl.write_text("- [ ] Add 404 default:main\n")
        item, job_id = fb.claim(self.home, "run-a")
        self.assertEqual(job_id, "001-add-404")
        self.assertEqual(self.bl.read_text(),
                         f"- [~] Add 404 default:main → {job_id}\n")
        job = fj.read_job(self.jobs / job_id)
        self.assertEqual((job["owner"], job["default_branch"], job["repo"]),
                         ("run-a", "main", str(self.repo)))
        self.assertIsNone(fb.claim(self.home, "run-a"))

    def test_claim_race_one_line_two_processes(self):
        self.bl.write_text("- [ ] Only one\n")
        env = dict(os.environ, FACTORY_HOME=str(self.home))
        procs = [subprocess.Popen([sys.executable, CLI, "claim", "--owner", o], env=env,
                                  stdout=subprocess.PIPE, text=True) for o in ("a", "b")]
        outs = [p.communicate()[0].strip() for p in procs]
        self.assertEqual(sorted(o == "null" for o in outs), [False, True])
        got = json.loads([o for o in outs if o != "null"][0])
        self.assertEqual(got["repo"], str(self.repo))
        dirs = [p.name for p in self.jobs.iterdir() if not p.name.startswith(".")]
        self.assertEqual(len(dirs), 1)

    def test_after_blocks_until_merged(self):
        self.mkjob("001-a", stage="build")
        self.bl.write_text("- [ ] B after:001-a\n- [ ] C\n")
        item, _ = fb.claim(self.home, "o")
        self.assertEqual(item.line_no, 2)
        self.assertIsNone(fb.claim(self.home, "o"))
        fj.update_job(self.jobs / "001-a", {"stage": "merged"}, owner="o")
        item, _ = fb.claim(self.home, "o")
        self.assertEqual(item.line_no, 1)

    def test_after_waits_on_held_and_closed(self):
        for stage in ("held", "closed"):
            self.mkjob("001-a", stage=stage) if not (self.jobs / "001-a").exists() else \
                fj.update_job(self.jobs / "001-a", {"stage": stage}, owner="o")
            self.bl.write_text("- [ ] B after:001-a\n")
            self.assertIsNone(fb.claim(self.home, "o"), stage)
        fj.update_job(self.jobs / "001-a", {"stage": "shipped"}, owner="o")
        self.assertIsNotNone(fb.claim(self.home, "o"))

    def test_finish_held_and_shipped(self):
        self.mkjob("001-a", stage="held")
        self.mkjob("002-b", stage="shipped")
        self.bl.write_text("- [~] A → 001-a\n- [~] B → 002-b\n")
        fb.finish(self.home, "001-a")
        fb.finish(self.home, "002-b")
        self.assertEqual(self.bl.read_text().splitlines(),
                         ["- [!] A → 001-a held", "- [x] B → 002-b shipped"])

    def test_claim_run(self):
        self.bl.write_text("- [ ] A\n")
        _, job_id = fb.claim(self.home, "o", run="20261016-1530-ab12")
        self.assertEqual(fj.read_job(self.jobs / job_id)["run"], "20261016-1530-ab12")
        self.bl.write_text("- [ ] A\n")
        got = json.loads(self.cli("claim", "--owner", "o", "--run", "20261016-1530-ab12").stdout)
        self.assertEqual(fj.read_job(self.jobs / got["job_id"])["run"], "20261016-1530-ab12")

    def test_claim_refuses_bogus_owner(self):
        self.bl.write_text("- [ ] A\n")
        with self.assertRaises(fj.BadRequest):
            fb.claim(self.home, "None")
        self.assertEqual(self.bl.read_text(), "- [ ] A\n")
        self.assertFalse(self.jobs.exists() and any(self.jobs.iterdir()))
        r = self.cli("claim", "--owner", "None")
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_after_missing_job_not_claimable(self):
        self.bl.write_text("- [ ] B after:009-nope\n")
        self.assertIsNone(fb.claim(self.home, "o"))

    def test_claim_given_line(self):
        self.bl.write_text("- [ ] A\n- [ ] B\n")
        item, _ = fb.claim(self.home, "o", line_no=2)
        self.assertEqual(item.feature, "B")
        self.assertTrue(self.bl.read_text().startswith("- [ ] A\n- [~] B → "))
        self.assertIsNone(fb.claim(self.home, "o", line_no=2))

    def test_finish_and_stale(self):
        now = datetime.now(timezone.utc)
        self.mkjob("001-a", stage="merged")
        self.mkjob("002-b", stage="blocked")
        self.mkjob("003-c", stage="build", updated=now - timedelta(hours=3))
        self.mkjob("004-d", stage="build", updated=now - timedelta(minutes=5))
        self.bl.write_text("- [~] A → 001-a\n- [~] B → 002-b\n"
                           "- [~] C → 003-c\n- [~] D → 004-d\n"
                           "- [~] E → 005-gone\n")
        self.assertEqual(fb.stale(self.home, set(), now), ["003-c", "005-gone"])
        self.assertEqual(fb.stale(self.home, {"003-c"}, now), ["005-gone"])
        fb.finish(self.home, "001-a")
        fb.finish(self.home, "002-b")
        lines = self.bl.read_text().splitlines()
        self.assertEqual(lines[0], "- [x] A → 001-a merged")
        self.assertEqual(lines[1], "- [!] B → 002-b blocked")

    def test_cli_add_finish_stale(self):
        r = self.cli("add", "Feat :: x")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "1"))
        r = self.cli("claim", "--owner", "o")
        got = json.loads(r.stdout)
        self.assertEqual((got["repo"], got["feature"], got["install"], got["default"]),
                         (str(self.repo), "Feat :: x", None, None))
        self.assertEqual(self.cli("stale", "--live", got["job_id"]).stdout.strip(), "")
        self.assertEqual(self.cli("finish", got["job_id"]).returncode, 0)
        self.assertIn("[~]", self.bl.read_text())  # stage is still spec: unchanged


    # --- list / edit / remove ---------------------------------------------------------

    def test_numbers_skip_other_lines(self):
        self.bl.write_text("# Backlog\n\n- [x] A → 001-a shipped\nnote\n- [ ] B wi:3\n")
        items = fb.list_items(self.home)
        self.assertEqual([(i.n, i.line_no, i.feature) for i in items], [(1, 3, "A"), (2, 5, "B")])
        self.assertEqual(fb.add(self.home, "C"), 3)

    def test_edit_todo_only(self):
        self.bl.write_text("# B\n- [ ] Old after:001-a\n- [~] Busy → 002-b\n")
        it = fb.edit(self.home, 1, "New text default:dev")
        self.assertEqual((it.feature, it.default, it.after), ("New text", "dev", None))
        self.assertEqual(self.bl.read_text(), "# B\n- [ ] New text default:dev\n- [~] Busy → 002-b\n")
        with self.assertRaisesRegex(fb.BacklogError, "only todo items"):
            fb.edit(self.home, 2, "x")
        with self.assertRaisesRegex(fb.BacklogError, "no backlog item 9"):
            fb.edit(self.home, 9, "x")
        with self.assertRaisesRegex(fb.BacklogError, "job marker"):
            fb.edit(self.home, 1, "sneak → 003-c shipped")
        with self.assertRaisesRegex(fb.BacklogError, "one non-empty line"):
            fb.add(self.home, "a\nb")

    def test_remove_refuses_claimed(self):
        self.bl.write_text("- [x] Done → 001-a shipped\n- [~] Busy → 002-b\n- [ ] Todo\n")
        with self.assertRaisesRegex(fb.BacklogError, "claimed by job 002-b"):
            fb.remove(self.home, 2)
        self.assertEqual(fb.remove(self.home, 3).feature, "Todo")
        self.assertEqual(fb.remove(self.home, 1).job_id, "001-a")
        self.assertEqual(self.bl.read_text(), "- [~] Busy → 002-b\n")

    def test_cli_list_edit_remove(self):
        self.cli("add", "One")
        self.cli("add", 'Two install:"npm ci"')
        got = json.loads(self.cli("list").stdout)
        self.assertEqual(got["repo"], str(self.repo))
        self.assertEqual([(i["n"], i["state"], i["feature"], i["install"]) for i in got["items"]],
                         [(1, "todo", "One", None), (2, "todo", "Two", "npm ci")])
        r = self.cli("edit", "1", "Uno")
        self.assertEqual((r.returncode, json.loads(r.stdout)["feature"]), (0, "Uno"))
        r = self.cli("remove", "2")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.bl.read_text(), "- [ ] Uno\n")
        r = self.cli("remove", "5")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no backlog item 5", r.stderr)

    def test_home_defaults_to_repo_of_cwd(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_HOME"}
        sub = self.repo / "sub"
        sub.mkdir()
        r = subprocess.run([sys.executable, CLI, "add", "From cwd"], env=env, cwd=sub,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.bl.read_text(), "- [ ] From cwd\n")
        r = subprocess.run([sys.executable, CLI, "list"], env=env, cwd=self._tmp.name,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not in a git repo", r.stderr)


if __name__ == "__main__":
    unittest.main()
