import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
import bench  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, check=True).stdout.strip()


class BenchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def arm(self, name, files_from, feature_task="03-laps-lookup"):
        repo = bench.repo_of(self.root, name)
        shutil.copytree(bench.HERE / "seed", repo, ignore=shutil.ignore_patterns("__pycache__"))
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "seed")
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "switch", "-qc", "factory/001-x")
        if files_from:
            shutil.copytree(files_from, repo, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "impl")
        git(repo, "switch", "-q", "main")
        jd = repo / ".factory" / "jobs" / "001-x"
        jd.mkdir(parents=True)
        feature = (bench.HERE / "tasks" / feature_task / "task.md").read_text()
        (jd / "job.json").write_text(json.dumps({
            "id": "001-x", "feature": feature, "branch": "factory/001-x", "base": base,
            "stage": "merged", "round": 1, "risk": "NONE", "reviews": {"code": "PASS"},
            "verified": {"confirmed": 0, "false_positive": 0, "unverifiable": 0},
            "created": "2026-11-15T10:00:00+00:00", "updated": "2026-11-15T10:20:00+00:00",
            "workers": [], "worktree": str(self.root / "nowhere")}))
        return repo

    def test_grade_reference_passes_and_seed_fails(self):
        self.arm("new", bench.HERE / "reference")
        self.arm("old", None)
        new = bench.grade_arm(self.root, "new")["001-x"]
        old = bench.grade_arm(self.root, "old")["001-x"]
        self.assertEqual(new["task"], "03-laps-lookup")
        self.assertTrue(new["accept"]["ok"], new["accept"])
        self.assertTrue(new["suite_ok"] and new["build_ok"])
        self.assertGreater(new["lines_added"], 20)
        self.assertFalse(old["accept"]["ok"])

    def test_usage_dedupes_streamed_messages_and_cost(self):
        p = self.root / "s.jsonl"
        rows = [{"timestamp": "2026-11-15T10:00:00Z", "message": {"id": "m1", "model": "claude-opus-5-5",
                 "usage": {"input_tokens": 5, "output_tokens": 1}}},
                {"timestamp": "2026-11-15T10:01:00Z", "message": {"id": "m1", "model": "claude-opus-5-5",
                 "usage": {"input_tokens": 5, "output_tokens": 40, "cache_read_input_tokens": 7}}},
                {"message": {"id": "m2", "model": "claude-haiku-5-5", "usage": {"output_tokens": 3}}},
                "not json"]
        p.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows))
        u, t0, t1 = bench.usage_of(p)
        self.assertEqual(u["claude-opus-5-5"]["out"], 40)
        self.assertEqual(u["claude-opus-5-5"]["cache_read"], 7)
        self.assertEqual((t0, t1), ("2026-11-15T10:00:00Z", "2026-11-15T10:01:00Z"))
        prices = {"opus": dict(input=10, output=100, cache_read=1, cache_write=0),
                  "haiku": dict(input=1, output=5, cache_read=0, cache_write=0)}
        self.assertAlmostEqual(bench.cost(u, prices), (5 * 10 + 40 * 100 + 7 + 3 * 5) / 1e6)
        self.assertIsNone(bench.cost(u, {"opus": dict(input=None, output=1, cache_read=1,
                                                      cache_write=1)}))

    def test_fit_prices_recovers_known_rates(self):
        pin, pout, pread = 10.0, 50.0, 0.5
        pts = []
        for inp, out, read, w1 in [(2, 4, 0, 51000), (3, 300, 51000, 900), (2, 1100, 52000, 40),
                                   (9, 200, 300000, 2000), (12, 400, 700000, 3000)]:
            cost = (pin * (inp + 2 * w1) + pout * out + pread * read) / 1e6
            pts.append(dict(inp=inp, out=out, read=read, w5=0, w1=w1, cost=cost))
        fit = bench.fit_prices(pts)
        self.assertAlmostEqual(fit["input"], 10.0, places=2)
        self.assertAlmostEqual(fit["output"], 50.0, places=2)
        self.assertAlmostEqual(fit["cache_read"], 0.5, places=2)
        self.assertAlmostEqual(fit["cache_write"], 20.0, places=2)
        self.assertLess(fit["residual"], 1e-6)
        with self.assertRaises(ValueError):
            bench.fit_prices(pts[:3])

    def test_calibrate_with_a_stub_claude(self):
        stub = ROOT / "tests" / f"claude-stub-{id(self)}"
        stub.write_text("""#!/usr/bin/env python3
import json, sys
a = sys.argv
prompt = a[a.index("-p") + 1]
model = a[a.index("--model") + 1]
turns = 10 if "`echo a`" in prompt else (5 if "`echo 1`" in prompt else 1)
out = 4 if "one word" in prompt else (300 if "120" in prompt else (1100 if "400" in prompt else 60 * turns))
read = 0 if "one word" in prompt else 51000 * turns
w1 = 51000 if "one word" in prompt else 900 + 100 * turns
cost = (3 * (3 + 2 * w1) + 15 * out + 0.15 * read) / 1e6
print(json.dumps({"modelUsage": {"claude-" + model + "-5-5": {
    "inputTokens": 3, "outputTokens": out, "cacheReadInputTokens": read,
    "cacheCreationInputTokens": w1, "cacheCreationEphemeral1hInputTokens": w1,
    "cacheCreationEphemeral5mInputTokens": 0, "costUSD": cost}}}))
""")
        stub.chmod(0o755)
        self.addCleanup(stub.unlink)
        out = self.root / "prices.json"
        t = bench.calibrate(["sonnet"], claude=str(stub), out=out)
        self.assertAlmostEqual(t["sonnet"]["input"], 3.0, places=2)
        self.assertAlmostEqual(t["sonnet"]["output"], 15.0, places=2)
        self.assertAlmostEqual(t["sonnet"]["cache_read"], 0.15, places=3)
        self.assertEqual(json.loads(out.read_text())["sonnet"]["cache_write"], 6.0)

    def test_sessions_from_before_the_job_are_ignored(self):
        repo = self.arm("new", bench.HERE / "reference")
        jd = repo / ".factory" / "jobs" / "001-x"
        j = json.loads((jd / "job.json").read_text())
        wt = self.root / "wt"
        j["worktree"] = str(wt)
        (jd / "job.json").write_text(json.dumps(j))
        pd = bench.project_dir(wt, self.root / "projects")
        pd.mkdir(parents=True)
        def sess(name, ts, out):
            (pd / name).write_text(json.dumps({"timestamp": ts, "message": {
                "id": name, "model": "claude-sonnet-5-5", "usage": {"output_tokens": out}}}))
        sess("old.jsonl", "2026-11-15T09:00:00Z", 1000)     # an earlier run, same path
        sess("mine.jsonl", "2026-11-15T10:05:00Z", 7)
        orig, bench.CLAUDE_PROJECTS = bench.CLAUDE_PROJECTS, self.root / "projects"
        self.addCleanup(setattr, bench, "CLAUDE_PROJECTS", orig)
        m = bench.collect_arm(self.root, "new")["jobs"]["001-x"]["tokens"]
        self.assertEqual(m["claude-sonnet-5-5"]["out"], 7)

    def test_coordinator_json_is_read_and_reported(self):
        self.arm("new", bench.HERE / "reference")
        (self.root / "new" / "coordinator.json").write_text(json.dumps({
            "total_cost_usd": 0.0104, "duration_ms": 2378, "num_turns": 1,
            "modelUsage": {"claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 4,
                           "cacheReadInputTokens": 11595, "cacheCreationInputTokens": 51459,
                           "costUSD": 0.0104}}}))
        m = bench.collect_arm(self.root, "new")["coordinator_session"]
        self.assertEqual((m["cost_usd"], m["duration_s"], m["turns"]), (0.0104, 2.378, 1))
        self.assertEqual(m["models"]["claude-haiku-5-5"]["cache_write"], 51459)

    def test_collect_pack_report_end_to_end(self):
        self.arm("new", bench.HERE / "reference")
        self.arm("old", None)
        metrics = {arm: bench.collect_arm(self.root, arm) for arm in bench.ARMS}
        self.assertEqual(metrics["new"]["jobs"]["001-x"]["wall_s"], 1200)
        (self.root / "metrics.json").write_text(json.dumps(metrics))
        (self.root / "grades.json").write_text(json.dumps(
            {arm: bench.grade_arm(self.root, arm) for arm in bench.ARMS}))
        bench.pack(self.root)
        key = json.loads((self.root / "judge" / "KEY.json").read_text())
        self.assertEqual(set(key["03-laps-lookup"].values()), {"old", "new"})
        packet = (self.root / "judge" / "03-laps-lookup.md").read_text()
        self.assertNotIn("old", packet.split("## Task")[1].lower().split("implementation a")[0])
        bench.report(self.root)
        text = (self.root / "report.md").read_text()
        self.assertIn("03-laps-lookup | new", text)
        self.assertIn("PASS", text)


if __name__ == "__main__":
    unittest.main()
