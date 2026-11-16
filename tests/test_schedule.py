import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_schedule as fs  # noqa: E402

CLI = str(ROOT / "scripts" / "factory_schedule.py")


def S(id, job, kind, requested, workers, server=False):
    return fs.Step(id, job, kind, requested, workers, server)


class ScheduleTest(unittest.TestCase):
    def test_port(self):
        self.assertEqual(fs.choose_port(7, lambda p: True), 4170)
        self.assertEqual(fs.choose_port(107, lambda p: p != 4170), 4171)
        self.assertIsNone(fs.choose_port(7, lambda p: False))

    def test_port_is_free_bool(self):
        self.assertIsInstance(fs.port_is_free(4199), bool)

    def test_caps_clamped(self):
        self.assertEqual(fs.Caps(jobs=9).jobs, 5)
        self.assertEqual(fs.Caps(jobs=0).jobs, 1)
        c = fs.Caps()
        self.assertEqual((c.jobs, c.workers, c.servers), (3, 8, 4))

    def test_can_claim(self):
        self.assertTrue(fs.can_claim(2, fs.Caps()))
        self.assertFalse(fs.can_claim(3, fs.Caps()))

    def test_worker_cap(self):
        running = [S("r", "x", "build", 0, 6)]
        pending = [S("a", "A", "review", 1, 4), S("b", "B", "spec", 2, 1)]
        # 2 workers left: review (4) refused, spec (1) would fit but must not jump
        self.assertEqual(fs.admit(pending, running, fs.Caps()), [])

    def test_fifo_no_starvation(self):
        running = [S("r", "x", "build", 0, 5)]
        pending = [S("a", "A", "review", 1, 4), S("b", "B", "spec", 2, 1)]
        self.assertEqual(fs.admit(pending, running, fs.Caps()), [])

    def test_admits_in_request_order_regardless_of_list_order(self):
        pending = [S("b", "B", "spec", 2, 1), S("a", "A", "spec", 1, 1)]
        self.assertEqual([s.id for s in fs.admit(pending, [], fs.Caps())], ["a", "b"])

    def test_budget_consumed_within_call(self):
        pending = [S("a", "A", "x", 1, 5), S("b", "B", "x", 2, 4)]
        self.assertEqual([s.id for s in fs.admit(pending, [], fs.Caps())], ["a"])

    def test_server_cap_independent(self):
        running = [S(f"r{i}", "x", "dev", 0, 0, True) for i in range(4)]
        pending = [S("a", "A", "review", 1, 1, True), S("b", "B", "build", 2, 2)]
        self.assertEqual([s.id for s in fs.admit(pending, running, fs.Caps())], ["b"])

    def test_server_refusal_blocks_later_server_steps(self):
        running = [S(f"r{i}", "x", "dev", 0, 0, True) for i in range(4)]
        pending = [S("a", "A", "r", 1, 1, True), S("b", "B", "r", 2, 1, True)]
        self.assertEqual(fs.admit(pending, running, fs.Caps()), [])

    def test_zero_workers_needs_no_worker_budget(self):
        running = [S("r", "x", "build", 0, 8)]
        pending = [S("a", "A", "w", 1, 1), S("b", "B", "srv", 2, 0, True)]
        self.assertEqual([s.id for s in fs.admit(pending, running, fs.Caps())], ["b"])

    def test_oversize_step_does_not_starve(self):
        pending = [S("big", "A", "x", 1, 9), S("srv", "A", "y", 2, 1, True),
                   S("ok", "B", "z", 3, 1)]
        self.assertEqual([s.id for s in fs.admit(pending, [], fs.Caps(servers=0))], ["ok"])
        self.assertEqual([s.id for s in fs.admit(pending, [], fs.Caps())], ["srv", "ok"])

    def test_two_resource_step_refused_for_one(self):
        # servers full: a (workers+server) refused for server only; worker-only c still goes,
        # later server step d stays blocked
        running = [S(f"r{i}", "x", "dev", 0, 0, True) for i in range(4)]
        pending = [S("a", "A", "r", 1, 2, True), S("c", "B", "b", 2, 2),
                   S("d", "C", "r", 3, 1, True)]
        self.assertEqual([s.id for s in fs.admit(pending, running, fs.Caps())], ["c"])
        # workers short: a refused for workers; later server-only step (0 workers) still goes
        running = [S("w", "x", "b", 0, 7)]
        pending = [S("a", "A", "r", 1, 2, True), S("s", "B", "r", 2, 0, True),
                   S("w1", "C", "b", 3, 1)]
        self.assertEqual([s.id for s in fs.admit(pending, running, fs.Caps())], ["s"])


class ScheduleCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_file = str(Path(self._tmp.name) / "sub" / "runs" / "r1.json")

    def cli(self, *args):
        return subprocess.run([sys.executable, CLI, *args],
                              capture_output=True, text=True)

    def req(self, job, kind, workers, requested, server=False):
        a = ["request", "--run", self.run_file, "--job", job, "--kind", kind,
             "--workers", str(workers), "--requested", str(requested)]
        if server:
            a.append("--server")
        r = self.cli(*a)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def data(self):
        return json.loads(Path(self.run_file).read_text())

    def test_request_admit_done(self):
        a = self.req("001-a", "review", 4, 1)
        b = self.req("002-b", "review", 4, 2)
        c = self.req("002-b", "review", 1, 3)
        self.assertEqual(len({a, b, c}), 3)
        self.assertTrue(a.startswith("001-a:review:"))
        r = self.cli("admit", "--run", self.run_file)
        self.assertEqual(r.returncode, 0, r.stderr)
        admitted = [json.loads(l)["id"] for l in r.stdout.splitlines()]
        self.assertEqual(admitted, [a, b])  # 4+4 fills the 8-worker budget
        d = self.data()
        self.assertEqual([s["id"] for s in d["running"]], [a, b])
        self.assertEqual([s["id"] for s in d["pending"]], [c])
        self.assertEqual(self.cli("done", "--run", self.run_file, "--step", a).returncode, 0)
        r = self.cli("admit", "--run", self.run_file)
        self.assertEqual([json.loads(l)["id"] for l in r.stdout.splitlines()], [c])
        self.assertEqual(self.cli("done", "--run", self.run_file, "--step", "nope").returncode, 1)

    def test_init_clamps_and_refuses_existing(self):
        r = self.cli("init", "--run", self.run_file, "--jobs", "9")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.data()["caps"], {"jobs": 5, "workers": 8, "servers": 4})
        self.assertEqual(self.cli("init", "--run", self.run_file).returncode, 1)

    def test_request_rejects_oversize(self):
        self.cli("init", "--run", self.run_file, "--workers", "4", "--servers", "0")
        base = ["request", "--run", self.run_file, "--job", "j", "--kind", "k"]
        self.assertEqual(self.cli(*base, "--workers", "5").returncode, 2)
        self.assertEqual(self.cli(*base, "--workers", "1", "--server").returncode, 2)
        self.assertEqual(self.data()["pending"], [])
        self.assertEqual(self.cli(*base, "--workers", "4").returncode, 0)

    def test_can_claim_cli(self):
        self.cli("init", "--run", self.run_file, "--jobs", "3")
        out = [self.cli("can-claim", "--run", self.run_file, "--active", str(n)).stdout.strip()
               for n in (0, 1, 3, 4)]
        self.assertEqual(out, ["3", "2", "0", "0"])
        self.assertEqual(self.cli("can-claim", "--run", self.run_file, "--active", "-1")
                         .returncode, 2)

    def test_claimable(self):
        self.assertEqual(fs.claimable(1, fs.Caps(jobs=5)), 4)
        self.assertEqual(fs.claimable(9, fs.Caps(jobs=5)), 0)

    def test_port_cli(self):
        r = self.cli("port", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(4170 <= int(r.stdout) <= 4179)


if __name__ == "__main__":
    unittest.main()
