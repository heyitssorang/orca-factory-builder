import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_dashboard as fd  # noqa: E402

PAGE = ROOT / "dashboard" / "index.html"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.jobs = self.home / "jobs"
        self.job = self.jobs / "001-a"
        (self.job / "round-1").mkdir(parents=True)
        (self.job / "round-1" / "review-ui.md").write_text("VERDICT: PASS\n<b>x</b>")
        (self.job / "job.json").write_text(json.dumps(
            {"id": "001-a", "feature": "A", "stage": "spec", "reviews": {}}))
        job2 = self.jobs / "002-b"
        job2.mkdir()
        (job2 / "job.json").write_text(json.dumps({"id": "002-b", "feature": "B", "stage": "build"}))
        (self.jobs / ".n-002").mkdir()
        (self.home / "backlog.md").write_text("secret\n- [ ] Do thing\n")
        self.outside = Path(self._tmp.name) / "outside.txt"
        self.outside.write_text("outside")
        self.workers, self.asked = {}, []

        def workers(runs):
            self.asked.append(set(runs))
            return self.workers
        self.srv = fd.make_server([self.home], PAGE, port=0, workers=workers)
        self.key = fd.lane_key(self.home)
        self.port = self.srv.server_address[1]
        t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def api(self):
        """The one lane's data, as /api/jobs used to return it."""
        r = self.req("GET", "/api/lanes")
        self.assertEqual(r.status, 200)
        return json.loads(r.body)["lanes"][0]

    def req(self, method, path, host=None, raw=False):
        if not raw and path.startswith("/files/") and not path.startswith(f"/files/{self.key}/"):
            path = f"/files/{self.key}/" + path[len("/files/"):]
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", f"127.0.0.1:{self.port}" if host is None else host)
            c.endheaders()
            r = c.getresponse()
            r.body = r.read()
            return r
        finally:
            c.close()

    def test_binds_loopback(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_post_rejected(self):
        self.assertEqual(self.req("POST", "/api/lanes").status, 405)

    def test_traversal_rejected(self):
        self.assertEqual(self.req("GET", "/files/001-a/../../backlog.md").status, 404)
        self.assertEqual(self.req("GET", "/files/001-a/%2e%2e/%2e%2e/backlog.md").status, 404)

    def test_symlink_escape_rejected(self):
        (self.job / "leak.txt").symlink_to(self.outside)
        self.assertEqual(self.req("GET", "/files/001-a/leak.txt").status, 404)
        (self.job / "leakdir").symlink_to(self.outside.parent)
        self.assertEqual(self.req("GET", "/files/001-a/leakdir/outside.txt").status, 404)

    def test_dot_entries_hidden(self):
        (self.jobs / ".n-002" / "x.txt").write_text("x")
        self.assertEqual(self.req("GET", "/files/.n-002/x.txt").status, 404)
        (self.job / ".job.json.tmp").write_text("x")
        self.assertEqual(self.req("GET", "/files/001-a/.job.json.tmp").status, 404)

    def test_text_plain_and_headers(self):
        r = self.req("GET", "/files/001-a/round-1/review-ui.md")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "text/plain; charset=utf-8")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.getheader("X-Factory-Dashboard"), "1")

    def test_html_file_is_text_plain(self):
        (self.job / "x.html").write_text("<script>1</script>")
        r = self.req("GET", "/files/001-a/x.html")
        self.assertTrue(r.getheader("Content-Type").startswith("text/plain"))

    def test_png_only_under_screenshots(self):
        (self.job / "screenshots").mkdir()
        (self.job / "screenshots" / "a.png").write_bytes(b"\x89PNG")
        (self.job / "b.png").write_bytes(b"\x89PNG")
        self.assertEqual(self.req("GET", "/files/001-a/screenshots/a.png").getheader("Content-Type"), "image/png")
        self.assertTrue(self.req("GET", "/files/001-a/b.png").getheader("Content-Type").startswith("text/plain"))

    def test_size_cap(self):
        (self.job / "big.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        (self.job / "ok.txt").write_bytes(b"x" * (2 * 1024 * 1024))
        self.assertEqual(self.req("GET", "/files/001-a/big.txt").status, 404)
        self.assertEqual(self.req("GET", "/files/001-a/ok.txt").status, 200)

    def test_directory_and_missing(self):
        self.assertEqual(self.req("GET", "/files/001-a/round-1").status, 404)
        self.assertEqual(self.req("GET", "/files/001-a/nope").status, 404)
        self.assertEqual(self.req("GET", "/nope").status, 404)

    def test_jobs_api(self):
        r = self.req("GET", "/api/lanes")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("X-Factory-Dashboard"), "1")
        data = self.api()
        self.assertEqual([j["id"] for j in data["jobs"]], ["001-a", "002-b"])
        self.assertEqual(data["jobs"][0]["files"], ["job.json", "round-1/review-ui.md"])
        self.assertEqual(data["jobs"][1]["files"], ["job.json"])
        self.assertEqual(data["backlog"][0]["feature"], "Do thing")

    def test_jobs_api_skips_unreadable_job_and_symlinks(self):
        (self.jobs / "003-bad").mkdir()
        (self.jobs / "003-bad" / "job.json").write_text("{nope")
        (self.jobs / "004-empty").mkdir()
        (self.job / "leak.txt").symlink_to(self.outside)
        data = self.api()
        self.assertEqual([j["id"] for j in data["jobs"]], ["001-a", "002-b"])
        self.assertNotIn("leak.txt", data["jobs"][0]["files"])

    def test_deeply_nested_job_json_skipped(self):
        (self.jobs / "006-deep").mkdir()
        (self.jobs / "006-deep" / "job.json").write_text("[" * 100000 + "]" * 100000)
        self.assertEqual([j["id"] for j in self.api()["jobs"]], ["001-a", "002-b"])
        self.assertEqual([j["id"] for j in fd.collect(self.home)["jobs"]], ["001-a", "002-b"])

    def test_colof_maps_stages_to_columns(self):
        import re
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        page = PAGE.read_text()
        cols = (re.search(r"const ACTIVE = \[.*?\];", page).group(0) + "\n"
                + re.search(r"const COLS = \[.*?\];", page, re.S).group(0))
        col_of = re.search(r"function colOf\(stage\) \{.*?\n\}", page, re.S).group(0)
        script = (cols + "\n" + col_of + "\nconsole.log(JSON.stringify("
                  "['held','merged','shipped','spec','verify','failed','blocked','closed','pr','weird'].map(colOf)));")
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout),
                         ["needs", "done", "done", "flight", "flight", "needs", "needs", "done", "flight",
                          "flight"])

    def test_page_columns_and_no_pr_remnants(self):
        page = PAGE.read_text()
        for gone in ("pr-draft", "pr-ready", "PR pending", "prlink", "job.pr"):
            self.assertNotIn(gone, page)
        for want in ('"held"', '"merged"', '"shipped"', "job.run", "hold.kind", "repofilter",
                     "/api/lanes", "lane.key"):
            self.assertIn(want, page)

    def test_jobs_api_runs(self):
        rd = self.home / "runs"
        rd.mkdir()
        (rd / "20261016-1200-abcd.json").write_text(json.dumps(
            {"id": "20261016-1200-abcd", "branch": "factory-run/20261016-1200-abcd", "shipped": None,
             "merged": ["x"]}))
        (rd / "20261015-0900-0001.json").write_text(json.dumps(
            {"id": "20261015-0900-0001", "branch": "factory-run/20261015-0900-0001",
             "shipped": {"merge_sha": "a" * 40, "pushed": True, "at": "t"}}))
        (rd / "20261016-1200-abcd.refs-before.json").write_text("{}")
        (rd / ".20261016-1200-abcd.flock").write_text("")
        (rd / "20261016-1200-abcd.lock").mkdir()
        (rd / "notarun.json").write_text('{"id": "notarun"}')
        (rd / "20261014-0000-ffff.json").write_text("{nope")
        self.outside.write_text('{"id": "20261013-0000-eeee", "branch": "LEAK"}')
        (rd / "20261013-0000-eeee.json").symlink_to(self.outside)
        data = self.api()
        self.assertEqual(data["runs"], [
            {"id": "20261015-0900-0001", "branch": "factory-run/20261015-0900-0001",
             "shipped": {"merge_sha": "a" * 40, "pushed": True, "at": "t"}},
            {"id": "20261016-1200-abcd", "branch": "factory-run/20261016-1200-abcd", "shipped": None}])
        self.assertNotIn(b"LEAK", self.req("GET", "/api/lanes").body)

    def test_symlinked_job_json_skipped(self):
        d = self.jobs / "005-link"
        d.mkdir()
        (d / "job.json").symlink_to(self.outside)
        self.outside.write_text('{"id": "005-link", "feature": "LEAK"}')
        data = self.api()
        self.assertEqual([j["id"] for j in data["jobs"]], ["001-a", "002-b"])
        self.assertNotIn(b"LEAK", self.req("GET", "/api/lanes").body)

    def test_fifo_job_json_does_not_hang(self):
        d = self.jobs / "006-fifo"
        d.mkdir()
        os.mkfifo(d / "job.json")
        t0 = time.monotonic()
        data = self.api()
        self.assertLess(time.monotonic() - t0, 1)
        self.assertEqual([j["id"] for j in data["jobs"]], ["001-a", "002-b"])
        os.mkfifo(self.job / "pipe.txt")
        t0 = time.monotonic()
        self.assertEqual(self.req("GET", "/files/001-a/pipe.txt").status, 404)
        self.assertLess(time.monotonic() - t0, 1)

    def test_jobs_have_dir(self):
        data = self.api()
        self.assertEqual(data["jobs"][0]["dir"], "001-a")

    def test_png_needs_screenshots_first(self):
        (self.job / "round-1" / "screenshots").mkdir()
        (self.job / "round-1" / "screenshots" / "a.png").write_bytes(b"\x89PNG")
        r = self.req("GET", "/files/001-a/round-1/screenshots/a.png")
        self.assertEqual(r.getheader("Content-Type"), "image/png")
        (self.job / "notes" / "screenshots").mkdir(parents=True)
        (self.job / "notes" / "screenshots" / "a.png").write_bytes(b"\x89PNG")
        r = self.req("GET", "/files/001-a/notes/screenshots/a.png")
        self.assertTrue(r.getheader("Content-Type").startswith("text/plain"))

    def test_error_pages_are_plain_text(self):
        for r in (self.req("GET", "/nope"), self.req("POST", "/"), self.req("BREW", "/")):
            self.assertTrue(r.getheader("Content-Type").startswith("text/plain"), r.status)
            self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_jobs_api_without_jobs_dir(self):
        import shutil
        shutil.rmtree(self.jobs)
        (self.home / "backlog.md").unlink()
        data = self.api()
        self.assertEqual({k: data[k] for k in ("jobs", "backlog", "runs")},
                         {"jobs": [], "backlog": [], "runs": []})
        self.assertEqual((data["key"], data["name"]), (self.key, self.home.parent.name))

    # --- lanes ------------------------------------------------------------------------

    def test_lane_per_home_and_files_stay_in_their_lane(self):
        other = Path(self._tmp.name) / "repo2" / ".factory"
        (other / "jobs" / "001-z").mkdir(parents=True)
        (other / "jobs" / "001-z" / "job.json").write_text(json.dumps({"id": "001-z", "stage": "build"}))
        (other / "jobs" / "001-z" / "build.md").write_text("STATUS: READY\n")
        srv = fd.make_server([self.home, other], PAGE, port=0, workers=lambda runs: {})
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.port, key2 = port, fd.lane_key(other)
        lanes = json.loads(self.req("GET", "/api/lanes").body)["lanes"]
        self.assertEqual([(l["name"], [j["id"] for j in l["jobs"]]) for l in lanes],
                         [(self.home.parent.name, ["001-a", "002-b"]), ("repo2", ["001-z"])])
        self.assertEqual(self.req("GET", f"/files/{key2}/001-z/build.md", raw=True).body, b"STATUS: READY\n")
        self.assertEqual(self.req("GET", f"/files/{self.key}/001-z/build.md", raw=True).status, 404)
        self.assertEqual(self.req("GET", "/files/000000000000/001-z/build.md", raw=True).status, 404)
        self.assertEqual(self.req("GET", "/files/nothex/001-z/build.md", raw=True).status, 404)

    def test_live_workers_marked_from_orca(self):
        (self.jobs / "002-b" / "job.json").write_text(json.dumps(
            {"id": "002-b", "stage": "held", "owner": "run_old"}))
        (self.job / "job.json").write_text(json.dumps(
            {"id": "001-a", "stage": "review", "owner": "run_a", "workers": [
                {"dispatch": "d1", "kind": "review", "lens": "code"},
                {"dispatch": "d2", "kind": "review", "lens": "security"},
                {"dispatch": "d3", "kind": "build"}]}))
        self.workers = {"d1": {"state": "running", "terminal": "active"},
                        "d2": {"state": "succeeded", "terminal": "released"}}
        data = json.loads(self.req("GET", "/api/lanes").body)
        self.assertTrue(data["orca"])
        job = data["lanes"][0]["jobs"][0]
        self.assertEqual(job["live"], [{"kind": "review", "lens": "code", "state": "running"}])
        self.assertEqual(self.asked[-1], {"run_a"})  # only owners of jobs that can be live
        self.workers = None
        data = json.loads(self.req("GET", "/api/lanes").body)
        self.assertFalse(data["orca"])
        self.assertEqual(data["lanes"][0]["jobs"][0]["live"], [])

    def test_run_workers_asks_each_run_explicitly_and_caches(self):
        calls = []
        done = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {"workers": [
            {"dispatchId": "d1", "workerState": "running", "terminalState": "active"}]}}), "")
        with mock.patch.object(fd, "orca_cmd", return_value=["orca-ide"]), \
                mock.patch.object(fd.subprocess, "run",
                                  side_effect=lambda cmd, **kw: calls.append(cmd) or done):
            rw = fd.RunWorkers()
            got = rw({"run_b", "run_a", "-x"})
            rw({"run_a"})
        self.assertEqual(got, {"d1": {"state": "running", "terminal": "active"}})
        self.assertEqual(calls, [
            ["orca-ide", "orchestration", "worker-list", "--run", "run_a", "--limit", "100", "--json"],
            ["orca-ide", "orchestration", "worker-list", "--run", "run_b", "--limit", "100", "--json"]])
        self.assertEqual(fd.RunWorkers(lambda run: None)({"run_a"}), None)
        self.assertEqual(fd.RunWorkers(lambda run: None)(set()), {})

    def test_discover_homes_from_env_and_orca(self):
        r1, r2 = Path(self._tmp.name) / "r1", Path(self._tmp.name) / "r2"
        (r1 / ".factory").mkdir(parents=True)
        r2.mkdir()
        with mock.patch.dict(os.environ, {"FACTORY_REPOS": os.pathsep.join([str(r1), str(r2)])}):
            self.assertEqual(fd.discover_homes(), [r1 / ".factory"])
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_REPOS"}
        listing = {"ok": True, "result": {"repos": [{"path": str(r1)}, {"path": str(r2)}, {"x": 1}]}}
        done = subprocess.CompletedProcess([], 0, json.dumps(listing), "")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fd, "orca_cmd", return_value=["orca-ide"]), \
                mock.patch.object(fd.subprocess, "run", return_value=done) as run:
            self.assertEqual(fd.discover_homes(), [r1 / ".factory"])
        self.assertEqual(run.call_args.args[0], ["orca-ide", "repo", "list", "--json"])
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fd, "orca_cmd", return_value=None):
            self.assertEqual(fd.discover_homes(), [])

    def test_orca_cmd_never_bare_orca(self):
        with mock.patch.dict(os.environ, {"ORCA_CLI_COMMAND": "/opt/orca --x"}):
            self.assertEqual(fd.orca_cmd(), ["/opt/orca", "--x"])
        env = {k: v for k, v in os.environ.items() if k != "ORCA_CLI_COMMAND"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(fd.shutil, "which", return_value=None), \
                mock.patch.object(fd.Path, "home", return_value=Path(self._tmp.name)):
            self.assertIsNone(fd.orca_cmd())

    def test_root_serves_page_with_csp(self):
        r = self.req("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/html"))
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(self.req("HEAD", "/").body, b"")

    def test_foreign_host_rejected(self):
        for h in ("evil.example", f"evil.example:{self.port}", f"127.0.0.1.evil.com:{self.port}",
                  "127.0.0.1:1", ""):
            self.assertEqual(self.req("GET", "/api/lanes", host=h).status, 403, h)
            self.assertEqual(self.req("GET", "/files/001-a/job.json", host=h).status, 403, h)
        self.assertEqual(self.req("GET", "/api/lanes", host=f"localhost:{self.port}").status, 200)

    def test_page_has_no_innerhtml(self):
        self.assertNotIn("innerHTML", PAGE.read_text())


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.port = free_port()

    def test_ensure_starts_once_and_reuses(self):
        self.addCleanup(fd.stop, self.home, self.port)
        url = fd.ensure(self.home, self.port)
        self.assertEqual(url, f"http://127.0.0.1:{self.port}/")
        pid = (self.home / "dashboard.pid").read_text()
        self.assertEqual(fd.ensure(self.home, self.port), url)
        self.assertEqual((self.home / "dashboard.pid").read_text(), pid)
        self.assertTrue(fd.stop(self.home, self.port))
        self.assertEqual(fd.probe(self.port), "free")
        self.assertFalse((self.home / "dashboard.pid").exists())

    def test_ensure_returns_quickly_when_child_dies(self):
        real = fd.subprocess.Popen
        with mock.patch.object(fd.subprocess, "Popen",
                               lambda cmd, **kw: real([sys.executable, "-c", "pass"], **kw)):
            t0 = time.monotonic()
            self.assertIsNone(fd.ensure(self.home, self.port))
        self.assertLess(time.monotonic() - t0, 3)

    def test_ensure_refuses_foreign_port(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port),
                                              http.server.SimpleHTTPRequestHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertEqual(fd.probe(self.port), "other")
        self.assertIsNone(fd.ensure(self.home, self.port))
        (self.home / "dashboard.pid").write_text("999999")
        self.assertFalse(fd.stop(self.home, self.port))
        self.assertEqual(fd.probe(self.port), "other")  # still alive, untouched


if __name__ == "__main__":
    unittest.main()
