import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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
        prices = {"opus": dict(input=10, output=100, cache_read=1, cache_write_5m=0, cache_write_1h=0),
                  "haiku": dict(input=1, output=5, cache_read=0, cache_write_5m=0, cache_write_1h=0)}
        self.assertAlmostEqual(bench.cost(u, prices)["usd"], (5 * 10 + 40 * 100 + 7 + 3 * 5) / 1e6)
        self.assertIsNone(bench.cost(u, {"opus": dict(input=None, output=1, cache_read=1,
                                                      cache_write_5m=1, cache_write_1h=1)}))

    def _transcript(self, rows, name="t.jsonl"):
        p = self.root / name
        p.write_text("\n".join(json.dumps(r) for r in rows))
        return p

    @staticmethod
    def _msg(i, req="r", total=None, cc=None, model="claude-opus-5-5", out=1):
        u = {"input_tokens": 1, "output_tokens": out}
        if total is not None:
            u["cache_creation_input_tokens"] = total
        if cc is not None:
            u["cache_creation"] = {"ephemeral_5m_input_tokens": cc[0], "ephemeral_1h_input_tokens": cc[1]}
        return {"requestId": req, "message": {"id": i, "model": model, "usage": u}}

    PRICES = {"opus": dict(input=4, output=20, cache_read=0.2, cache_write_5m=5, cache_write_1h=8)}

    def test_ttl_split_5m_1h_mixed_and_unknown(self):
        cases = {"5m": (self._msg("a", total=100, cc=(100, 0)), (100, 0, 0)),
                 "1h": (self._msg("a", total=100, cc=(0, 100)), (0, 100, 0)),
                 "mixed": (self._msg("a", total=100, cc=(30, 70)), (30, 70, 0)),
                 "unknown": (self._msg("a", total=100), (0, 0, 100)),
                 "partial": (self._msg("a", total=100, cc=(30, 50)), (30, 50, 20))}
        for name, (row, want) in cases.items():
            b = bench.usage_of(self._transcript([row]))[0]["claude-opus-5-5"]
            self.assertEqual((b["cache_write_5m"], b["cache_write_1h"], b["cache_write_unknown"]),
                             want, name)
            self.assertEqual(b["cache_write_total"], 100, name)
            self.assertEqual(b["inconsistent"], 0, name)

    def test_inconsistent_ttl_sum_is_flagged_not_negative(self):
        b = bench.usage_of(self._transcript([self._msg("a", total=100, cc=(80, 80))]))[0]["claude-opus-5-5"]
        self.assertEqual((b["cache_write_unknown"], b["inconsistent"]), (0, 1))
        self.assertEqual((b["cache_write_5m"], b["cache_write_1h"]), (80, 80))

    def test_streamed_duplicates_dedupe_on_message_and_request_id(self):
        rows = [self._msg("a", "r1", total=10, cc=(0, 10), out=1),
                self._msg("a", "r1", total=10, cc=(0, 10), out=9),   # same request: last wins
                self._msg("a", "r2", total=10, cc=(10, 0), out=2)]   # same id, other request
        b = bench.usage_of(self._transcript(rows))[0]["claude-opus-5-5"]
        self.assertEqual((b["n"], b["out"], b["cache_write_1h"], b["cache_write_5m"]), (2, 11, 10, 10))

    def test_coordinator_model_usage_keeps_the_ttl_split(self):
        self.arm("new", bench.HERE / "reference")
        (self.root / "new" / "coordinator.json").write_text(json.dumps({"modelUsage": {
            "claude-opus-5-5": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3,
                                "cacheCreationInputTokens": 100,
                                "cacheCreationEphemeral5mInputTokens": 30,
                                "cacheCreationEphemeral1hInputTokens": 60, "costUSD": 1.0}}}))
        m = bench.collect_arm(self.root, "new")["coordinator_session"]["models"]["claude-opus-5-5"]
        self.assertEqual((m["cache_write_5m"], m["cache_write_1h"], m["cache_write_unknown"],
                          m["cache_write_total"]), (30, 60, 10, 100))
        self.assertEqual(m["cost_usd"], 1.0)

    def test_cost_is_a_range_for_unknown_ttl_and_a_point_when_assumed(self):
        tok = {"claude-opus-5-5": dict(bench.new_bucket(), inp=1_000_000, cache_write_5m=1_000_000,
                                       cache_write_1h=1_000_000, cache_write_unknown=1_000_000)}
        c = bench.cost(tok, self.PRICES)
        self.assertEqual(c["unknown_ttl_tokens"], 1_000_000)
        self.assertAlmostEqual(c["usd_low"], 4 + 5 + 8 + 5)
        self.assertAlmostEqual(c["usd_high"], 4 + 5 + 8 + 8)
        self.assertIsNone(c["usd"])
        self.assertAlmostEqual(bench.cost(tok, self.PRICES, "1h")["usd"], 25.0)
        self.assertAlmostEqual(bench.cost(tok, self.PRICES, "5m")["usd"], 22.0)
        known = {"claude-opus-5-5": dict(bench.new_bucket(), cache_write_1h=1_000_000)}
        self.assertEqual(bench.cost(known, self.PRICES)["usd"], 8.0)
        with self.assertRaises(ValueError):
            bench.cost(tok, self.PRICES, "2h")

    def test_legacy_cache_write_bucket_counts_as_unknown_ttl(self):
        c = bench.cost({"claude-opus-5-5": dict(inp=0, out=0, cache_read=0, cache_write=1_000_000)},
                       self.PRICES)
        self.assertEqual((c["usd_low"], c["usd_high"], c["unknown_ttl_tokens"]), (5.0, 8.0, 1_000_000))

    def test_legacy_prices_file_gets_a_migration_error(self):
        p = self.root / "prices.json"
        p.write_text(json.dumps({"opus": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 2}}))
        with self.assertRaises(SystemExit) as e:
            bench.load_prices(p)
        self.assertIn("cache_write_5m", str(e.exception))
        p.write_text(json.dumps({"_note": "x", "opus": self.PRICES["opus"]}))
        self.assertIn("opus", bench.load_prices(p))

    def test_shipped_prices_json_uses_the_new_schema(self):
        prices = bench.load_prices()
        for fam in ("opus", "sonnet", "haiku"):
            self.assertAlmostEqual(prices[fam]["cache_write_5m"], prices[fam]["input"] * 1.25, places=3)
            self.assertAlmostEqual(prices[fam]["cache_write_1h"], prices[fam]["input"] * 2, places=3)

    def test_fit_prices_recovers_known_rates_and_both_multipliers(self):
        pin, pout, pread, p5, p1 = 10.0, 50.0, 0.5, 12.5, 20.0
        pts = []
        for inp, out, read, w5, w1 in [(2, 4, 0, 0, 51000), (3, 300, 51000, 0, 900),
                                       (2, 1100, 52000, 0, 40), (9, 200, 300000, 0, 2000),
                                       (2, 4, 0, 51000, 0), (3, 300, 51000, 900, 0),
                                       (2, 1100, 52000, 40, 0), (12, 400, 700000, 3000, 0)]:
            cost = (pin * inp + pout * out + pread * read + p5 * w5 + p1 * w1) / 1e6
            pts.append(dict(inp=inp, out=out, read=read, w5=w5, w1=w1, cost=cost))
        fit = bench.fit_prices(pts)
        self.assertAlmostEqual(fit["input"], 10.0, places=2)
        self.assertAlmostEqual(fit["output"], 50.0, places=2)
        self.assertAlmostEqual(fit["cache_read"], 0.5, places=2)
        self.assertAlmostEqual(fit["cache_write_5m"], 12.5, places=2)
        self.assertAlmostEqual(fit["cache_write_1h"], 20.0, places=2)
        self.assertAlmostEqual(fit["multiplier_5m"], 1.25, places=3)
        self.assertAlmostEqual(fit["multiplier_1h"], 2.0, places=3)
        self.assertLess(fit["residual"], 1e-6)
        with self.assertRaises(ValueError):
            bench.fit_prices(pts[:4])
        with self.assertRaises(ValueError):          # 1h writes only: the 5m price is unidentified
            bench.fit_prices([dict(p, w5=0) for p in pts])

    STUB = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv
prompt = a[a.index("-p") + 1]
model = a[a.index("--model") + 1]
forced = os.environ.get("FORCE_PROMPT_CACHING_5M") == "1"
turns = 10 if "`echo a`" in prompt else (5 if "`echo 1`" in prompt else 1)
out = 4 if "one word" in prompt else (300 if "120" in prompt else (1100 if "400" in prompt else 60 * turns))
read = 0 if "one word" in prompt else 51000 * turns
w = 51000 if "one word" in prompt else 900 + 100 * turns
w5, w1 = (w, 0) if forced else (0, w)
cost = (3 * 3 + 15 * out + 0.15 * read + 3 * __M5__ * w5 + 3 * 2 * w1) / 1e6
u = {"inputTokens": 3, "outputTokens": out, "cacheReadInputTokens": read,
     "cacheCreationInputTokens": w, "cacheCreationEphemeral1hInputTokens": w1,
     "cacheCreationEphemeral5mInputTokens": w5, "costUSD": cost}
__DROP__
print(json.dumps({"modelUsage": {"claude-" + model + "-5-5": u}}))
"""

    def _stub(self, m5=1.25, drop=""):
        stub = ROOT / "tests" / f"claude-stub-{id(self)}"
        stub.write_text(self.STUB.replace("__M5__", repr(m5)).replace("__DROP__", drop))
        stub.chmod(0o755)
        self.addCleanup(stub.unlink)
        return str(stub)

    def test_calibrate_with_a_stub_claude_fits_both_multipliers(self):
        out = self.root / "prices.json"
        out.write_text(json.dumps({"opus": self.PRICES["opus"]}))      # another family is kept
        t = bench.calibrate(["sonnet"], claude=self._stub(), out=out)
        self.assertAlmostEqual(t["sonnet"]["input"], 3.0, places=2)
        self.assertAlmostEqual(t["sonnet"]["output"], 15.0, places=2)
        self.assertAlmostEqual(t["sonnet"]["cache_read"], 0.15, places=3)
        saved = json.loads(out.read_text())
        self.assertEqual((saved["sonnet"]["cache_write_1h"], saved["sonnet"]["cache_write_5m"]), (6.0, 3.75))
        self.assertAlmostEqual(saved["sonnet"]["_fit"]["multiplier_5m"], 1.25, places=3)
        self.assertNotIn("cache_write", saved["sonnet"])
        self.assertEqual(saved["opus"], self.PRICES["opus"])

    def test_calibrate_refuses_unknown_ttl_data_points(self):
        out = self.root / "prices.json"
        drop = 'u.pop("cacheCreationEphemeral5mInputTokens"); u.pop("cacheCreationEphemeral1hInputTokens")'
        t = bench.calibrate(["sonnet"], claude=self._stub(drop=drop), out=out)
        self.assertNotIn("sonnet", t)
        self.assertFalse(out.exists())
        with self.assertRaises(ValueError):
            bench.point_from_model_usage({"cacheCreationInputTokens": 5, "costUSD": 1})
        with self.assertRaises(ValueError):          # 5m + 1h disagree with the total
            bench.point_from_model_usage({"cacheCreationInputTokens": 10,
                                          "cacheCreationEphemeral5mInputTokens": 3,
                                          "cacheCreationEphemeral1hInputTokens": 3, "costUSD": 1})

    def test_calibrate_does_not_write_when_the_5m_multiplier_is_off(self):
        out = self.root / "prices.json"
        t = bench.calibrate(["sonnet"], claude=self._stub(m5=1.0), out=out)
        self.assertNotIn("sonnet", t)
        self.assertFalse(out.exists())

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

    def _spec_stage_fixture(self):
        """Workers on the repo's main workspace (spec stage). Layout as Claude writes it: a main
        session `<id>.jsonl` whose brief never names the job, with its subagents under
        `<id>/subagents/`. Job and group ids need not match, only the `.factory/jobs/<id>` paths
        in tool calls do. Job 002-y exists beside 001-x."""
        repo = self.arm("new", bench.HERE / "reference")
        jd = repo / ".factory" / "jobs"
        j2 = json.loads((jd / "001-x" / "job.json").read_text())
        j2.update(id="002-y", branch="factory/002-y")
        (jd / "002-y").mkdir()
        (jd / "002-y" / "job.json").write_text(json.dumps(j2))
        pd = bench.project_dir(repo, self.root / "projects")
        def sess(path, out, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"timestamp": "2026-11-15T10:05:00Z", "message": {
                "id": path.stem, "model": "claude-sonnet-5-5", "usage": {"output_tokens": out},
                "content": text}}))
        brief = "Please carry out this task from my Orca coordinator. Your task ID is task_1."
        sess(pd / "mainA.jsonl", 10, brief + " Read .factory/jobs/001-x/job.json " * 3
             + " and once .factory/jobs/002-y/job.json")
        sess(pd / "mainA" / "subagents" / "agent-a1.jsonl", 100, "Write .factory/jobs/001-x/spec.md")
        sess(pd / "mainB.jsonl", 20, brief + " Read .factory/jobs/002-y/job.json")
        sess(pd / "mainB" / "subagents" / "agent-b1.jsonl", 200, "no job path here at all")
        sess(pd / "stray.jsonl", 1000, "an unrelated session")
        orig, bench.CLAUDE_PROJECTS = bench.CLAUDE_PROJECTS, self.root / "projects"
        self.addCleanup(setattr, bench, "CLAUDE_PROJECTS", orig)
        return bench.collect_arm(self.root, "new")

    def test_spec_stage_workers_are_attributed_by_job_path_across_main_and_subagents(self):
        m = self._spec_stage_fixture()
        out = lambda t: t["claude-sonnet-5-5"]["out"]
        # mainA names 001-x three times and 002-y once: 001-x wins and takes its subagent too
        self.assertEqual(out(m["jobs"]["001-x"]["tokens"]), 10 + 100)
        # mainB names only 002-y; its subagent names nothing but belongs to mainB
        self.assertEqual(out(m["jobs"]["002-y"]["tokens"]), 20 + 200)

    def test_spec_workers_in_the_lean_spec_worktree_are_found_by_run_id(self):
        repo = self.arm("new", bench.HERE / "reference")
        runs = repo / ".factory" / "runs"
        runs.mkdir()
        (runs / "20261115-1000-abcd.json").write_text(json.dumps({"id": "20261115-1000-abcd"}))
        d = self.root / "projects" / "-home-x-orca-workspaces-bench-new-factory-spec-20261115-1000-abcd"
        d.mkdir(parents=True)
        (d / "w.jsonl").write_text(json.dumps({"timestamp": "2026-11-15T10:05:00Z", "message": {
            "id": "w", "model": "claude-sonnet-5-5", "usage": {"output_tokens": 9},
            "content": "Write .factory/jobs/001-x/spec.md"}}))
        other = self.root / "projects" / "-home-x-orca-workspaces-bench-new-factory-spec-20990101-0000-ffff"
        other.mkdir()
        (other / "w.jsonl").write_text(json.dumps({"timestamp": "2026-11-15T10:05:00Z", "message": {
            "id": "z", "model": "claude-sonnet-5-5", "usage": {"output_tokens": 500}}}))
        orig, bench.CLAUDE_PROJECTS = bench.CLAUDE_PROJECTS, self.root / "projects"
        self.addCleanup(setattr, bench, "CLAUDE_PROJECTS", orig)
        m = bench.collect_arm(self.root, "new")
        self.assertEqual(m["jobs"]["001-x"]["spec_tokens"]["claude-sonnet-5-5"]["out"], 9)
        self.assertNotIn("claude-sonnet-5-5", m["unattributed_tokens"])   # other run's worker is not ours

    def test_attributed_sessions_are_not_also_counted_as_unattributed(self):
        m = self._spec_stage_fixture()
        self.assertEqual(m["unattributed_tokens"]["claude-sonnet-5-5"]["out"], 1000)

    def _worker_fixture(self):
        """A job whose worktree has: a main session (entrypoint sdk-cli, tool use), one subagent
        under <session>/subagents/, and a one-reply title helper (also sdk-cli)."""
        repo = self.arm("new", bench.HERE / "reference")
        jd = repo / ".factory" / "jobs" / "001-x"
        j = json.loads((jd / "job.json").read_text())
        wt = self.root / "wt"
        j["worktree"] = str(wt)
        (jd / "job.json").write_text(json.dumps(j))
        pd = bench.project_dir(wt, self.root / "projects")
        (pd / "main1" / "subagents").mkdir(parents=True)

        def row(i, ep, side=False, tool=False, cc=(5, 0), model="claude-sonnet-5-5"):
            msg = {"id": i, "model": model, "usage": {"output_tokens": 1, "cache_creation_input_tokens": sum(cc),
                   "cache_creation": {"ephemeral_5m_input_tokens": cc[0], "ephemeral_1h_input_tokens": cc[1]}}}
            if tool:
                msg["content"] = [{"type": "tool_use", "name": "Bash"}]
            return json.dumps({"timestamp": "2026-11-15T10:05:00Z", "entrypoint": ep, "version": "2.1.326",
                               "isSidechain": side, "requestId": i, "message": msg})
        (pd / "main1.jsonl").write_text("\n".join([row("a", "sdk-cli", tool=True, cc=(0, 7)),
                                                   row("b", "sdk-cli"), row("c", "sdk-cli")]))
        (pd / "main1" / "subagents" / "agent-1.jsonl").write_text(row("s", "cli", side=True, cc=(11, 0)))
        (pd / "title.jsonl").write_text(row("t", "sdk-cli", model="claude-haiku-5-5", cc=(0, 0)))
        orig, bench.CLAUDE_PROJECTS = bench.CLAUDE_PROJECTS, self.root / "projects"
        self.addCleanup(setattr, bench, "CLAUDE_PROJECTS", orig)
        return bench.collect_arm(self.root, "new")["jobs"]["001-x"]

    def test_session_kinds_and_entrypoint_diagnosis_name_only_the_main_session(self):
        job = self._worker_fixture()
        kinds = {m["session"]: m["kind"] for m in job["sessions"]}
        self.assertEqual(kinds, {"main1": "main", "agent-1": "subagent", "title": "helper"})
        dg = job["diagnosis"]
        self.assertEqual(dg["main_entrypoints"], {"sdk-cli": 1})
        self.assertEqual(dg["sdk_main_sessions"], ["main1"])        # not the helper, not the subagent
        self.assertEqual(dg["subagents"], 1)
        self.assertEqual(dg["helpers"], [{"session": "title", "entrypoints": ["sdk-cli"]}])
        sub = next(m for m in job["sessions"] if m["kind"] == "subagent")
        self.assertEqual((sub["entrypoints"], sub["versions"]), (["cli"], ["2.1.326"]))

    def test_tokens_split_by_session_kind_keep_the_ttl(self):
        job = self._worker_fixture()
        k = job["tokens_by_kind"]
        self.assertEqual((k["main"]["claude-sonnet-5-5"]["cache_write_1h"],
                          k["main"]["claude-sonnet-5-5"]["cache_write_5m"]), (7, 10))
        self.assertEqual(k["subagent"]["claude-sonnet-5-5"]["cache_write_5m"], 11)
        self.assertIn("claude-haiku-5-5", k["helper"])
        self.assertEqual(job["tokens"]["claude-sonnet-5-5"]["cache_write_total"], 7 + 10 + 11)

    def test_report_has_ttl_and_entrypoint_sections(self):
        self._worker_fixture()
        self.arm("old", None)
        metrics = {arm: bench.collect_arm(self.root, arm) for arm in bench.ARMS}
        (self.root / "metrics.json").write_text(json.dumps(metrics))
        (self.root / "grades.json").write_text(json.dumps(
            {arm: bench.grade_arm(self.root, arm) for arm in bench.ARMS}))
        bench.report(self.root)
        text = (self.root / "report.md").read_text()
        self.assertIn("## Cache-write TTL by session kind", text)
        self.assertIn("| new | worker subagent | 1 | 11 | 0 | 0 |", text)
        self.assertIn("| new | worker main | 1 | 10 | 7 | 0 |", text)
        self.assertIn("main1", text.split("## Worker sessions and entrypoints")[1])
        self.assertIn("| 001-x | new | 1 | sdk-cli x1 | main1 | 1 | sdk-cli |", text)

    def _run_branch(self, arm="new", files_from=None, merged=("001-x",)):
        """A run branch `factory-run/R1` off main, with the reference implementation (or nothing)."""
        repo = bench.repo_of(self.root, arm)
        git(repo, "switch", "-qc", "factory-run/R1", "main")
        if files_from:
            shutil.copytree(files_from, repo, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "merged")
        git(repo, "switch", "-q", "main")
        (repo / ".factory" / "runs").mkdir(parents=True, exist_ok=True)
        (repo / ".factory" / "runs" / "R1.json").write_text(json.dumps({
            "id": "R1", "branch": "factory-run/R1",
            "merged": [{"job": j, "commit": "x"} for j in merged]}))
        return repo

    def test_grade_runs_grades_the_integrated_tip_for_every_task(self):
        self.arm("new", bench.HERE / "reference")
        self._run_branch(files_from=bench.HERE / "reference")
        rg = bench.grade_runs(self.root, "new")["R1"]
        self.assertEqual(rg["merged"], ["001-x"])
        self.assertTrue(rg["suite_ok"] and rg["build_ok"])
        self.assertEqual(set(rg["accept"]), set(bench.TASKS))
        self.assertTrue(rg["accept"]["03-laps-lookup"]["ok"])
        grades = {"001-x": {"task": "03-laps-lookup"}}
        self.assertEqual(bench.delivered_jobs({"jobs": {"001-x": {}}}, grades, {"R1": rg}), ["001-x"])

    def test_a_job_that_passes_alone_but_not_on_the_run_tip_is_not_delivered(self):
        self.arm("new", bench.HERE / "reference")
        self._run_branch(files_from=None)                       # the run tip lacks the job's code
        rg = bench.grade_runs(self.root, "new")["R1"]
        self.assertFalse(rg["accept"]["03-laps-lookup"]["ok"])
        grades = bench.grade_arm(self.root, "new")
        self.assertTrue(grades["001-x"]["accept"]["ok"])         # its own branch is fine
        self.assertEqual(bench.delivered_jobs({"jobs": {"001-x": {}}}, grades, {"R1": rg}), [])
        # not merged into any run branch: not delivered either
        rg2 = dict(rg, merged=[], suite_ok=True, accept={"03-laps-lookup": {"ok": True}})
        self.assertEqual(bench.delivered_jobs({"jobs": {"001-x": {}}}, grades, {"R1": rg2}), [])
        # a red suite on the tip delivers nothing
        rg3 = dict(rg2, merged=["001-x"], suite_ok=False)
        self.assertEqual(bench.delivered_jobs({"jobs": {"001-x": {}}}, grades, {"R1": rg3}), [])

    def test_cost_per_delivered_job_counts_every_dollar_of_the_arm(self):
        def toks(out):
            return {"claude-opus-5-5": dict(bench.new_bucket(), out=out)}     # $20 per M output
        m = {"coordinator_session": {"cost_usd": 1.0},
             "jobs": {"a": {"tokens": toks(100_000)},      # delivered      $2
                      "b": {"tokens": toks(150_000)},      # failed         $3
                      "c": {"tokens": toks(50_000)}},      # held           $1
             "unattributed_tokens": toks(25_000)}          # unmatched      $0.5
        total = bench.arm_cost(m, self.PRICES)
        self.assertAlmostEqual(total["usd_low"], 1 + 2 + 3 + 1 + 0.5)
        self.assertAlmostEqual(total["usd_high"], total["usd_low"])
        per = bench.cost_per_delivered(total, 2)
        self.assertAlmostEqual(per["usd_low"], 3.75)
        self.assertIsNone(bench.cost_per_delivered(total, 0))        # nothing delivered
        self.assertIsNone(bench.cost_per_delivered(None, 2))         # cost unknown (no price)
        self.assertIsNone(bench.arm_cost({"jobs": {"a": {"tokens": {"mystery-model": bench.new_bucket()}}}},
                                         self.PRICES))

    def test_cost_per_delivered_keeps_the_unknown_ttl_range(self):
        unk = {"claude-opus-5-5": dict(bench.new_bucket(), cache_write_unknown=1_000_000)}
        m = {"coordinator_session": {"cost_usd": 0.0}, "jobs": {"a": {"tokens": unk}}}
        total = bench.arm_cost(m, self.PRICES)
        self.assertEqual((total["usd_low"], total["usd_high"]), (5.0, 8.0))
        per = bench.cost_per_delivered(total, 2)
        self.assertEqual((per["usd_low"], per["usd_high"]), (2.5, 4.0))
        pinned = bench.arm_cost(m, self.PRICES, "1h")
        self.assertEqual((pinned["usd_low"], pinned["usd_high"]), (8.0, 8.0))

    def test_register_with_orca_adds_the_repo_and_fails_loudly(self):
        rt = os.environ.get("XDG_RUNTIME_DIR")   # /tmp is often noexec; stubs must execute
        if not (rt and os.path.isdir(rt) and os.access(rt, os.W_OK | os.X_OK)):
            self.skipTest("no executable temp directory")
        tmp = tempfile.TemporaryDirectory(dir=rt)
        self.addCleanup(tmp.cleanup)
        bindir = Path(tmp.name) / "bin"
        bindir.mkdir()
        log = Path(tmp.name) / "orca.log"
        stub = bindir / "orca"
        stub.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit ${{ORCA_STUB_RC:-0}}\n')
        stub.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(bindir)}):
            bench.register_with_orca(Path("/r/bench-x"))
            self.assertEqual(log.read_text().strip(), "repo add --path /r/bench-x --json")
            with mock.patch.dict(os.environ, {"ORCA_STUB_RC": "1"}), self.assertRaises(SystemExit):
                bench.register_with_orca(Path("/r/bench-x"))
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "empty")}), self.assertRaises(SystemExit):
            bench.register_with_orca(Path("/r/bench-x"))

    def test_spec_only_prompt_stops_before_the_build_and_keeps_the_scout_rule(self):
        full = bench.build_prompt(Path("/s"))
        spec = bench.build_prompt(Path("/s"), spec_only=True)
        self.assertNotIn("spec-only", full)
        self.assertTrue(spec.startswith(full))
        self.assertIn("Do not start step 3", spec)
        self.assertIn("The scout and the planner are mandatory", spec)

    def test_spec_stage_is_split_out_with_quality_and_prefix_size(self):
        repo = self.arm("new", bench.HERE / "reference")
        jd = repo / ".factory" / "jobs"
        (jd / "001-x" / "spec.md").write_text("# Spec\n## Acceptance criteria\n1. a\n2. b\n3. c\n## Notes\n1. no\n")
        (jd / "001-x" / "context.md").write_text("ctx")
        pd = bench.project_dir(repo, self.root / "projects")
        row = {"timestamp": "2026-11-15T10:05:00Z", "message": {
            "id": "m", "model": "claude-sonnet-5-5", "usage": {"output_tokens": 1,
            "cache_creation_input_tokens": 66000, "cache_creation": {
                "ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 66000}},
            "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": ".factory/jobs/001-x/job.json"}}]}}
        (pd).mkdir(parents=True)
        (pd / "w.jsonl").write_text(json.dumps(row))
        orig, bench.CLAUDE_PROJECTS = bench.CLAUDE_PROJECTS, self.root / "projects"
        self.addCleanup(setattr, bench, "CLAUDE_PROJECTS", orig)
        ma = bench.collect_arm(self.root, "new")
        q = ma["jobs"]["001-x"]["spec_quality"]
        self.assertEqual((q["starts_with_heading"], q["criteria_section"], q["criteria"], q["context"], q["plan"]),
                         (True, True, 3, True, False))
        ss = bench.spec_stage_summary(ma, bench.load_prices())
        self.assertEqual((ss["jobs"], ss["usable"], ss["criteria_mean"]), (1, 1, 3.0))
        self.assertEqual((ss["main_sessions"], ss["main_cache_write_per_session"]), (1, 66000))
        self.assertAlmostEqual(ss["cost"]["usd_low"], 66000 * 4e-6 + 1e-5 * 0 + 1 * 10e-6, places=4)

    def test_report_has_a_delivery_section(self):
        self.arm("new", bench.HERE / "reference")
        self.arm("old", None)
        self._run_branch(files_from=None)
        metrics = {arm: bench.collect_arm(self.root, arm) for arm in bench.ARMS}
        (self.root / "metrics.json").write_text(json.dumps(metrics))
        (self.root / "grades.json").write_text(json.dumps(
            {arm: bench.grade_arm(self.root, arm) for arm in bench.ARMS}))
        (self.root / "run_grades.json").write_text(json.dumps(
            {arm: bench.grade_runs(self.root, arm) for arm in bench.ARMS}))
        bench.report(self.root)
        text = (self.root / "report.md").read_text()
        sec = text.split("## Delivery on the run branch")[1].split("## Cache-write TTL")[0]
        self.assertIn("| new | 1 | 1 | 0 |", sec)
        self.assertIn("n/a (nothing delivered)", sec)
        self.assertIn("new/001-x (03-laps-lookup)", sec)          # passes alone, fails on the tip

    def test_coordinator_json_is_read_and_reported(self):
        self.arm("new", bench.HERE / "reference")
        (self.root / "new" / "coordinator.json").write_text(json.dumps({
            "total_cost_usd": 0.0104, "duration_ms": 2378, "num_turns": 1,
            "modelUsage": {"claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 4,
                           "cacheReadInputTokens": 11595, "cacheCreationInputTokens": 51459,
                           "costUSD": 0.0104}}}))
        m = bench.collect_arm(self.root, "new")["coordinator_session"]
        self.assertEqual((m["cost_usd"], m["duration_s"], m["turns"]), (0.0104, 2.378, 1))
        h = m["models"]["claude-haiku-5-5"]
        self.assertEqual((h["cache_write_total"], h["cache_write_unknown"]), (51459, 51459))

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
