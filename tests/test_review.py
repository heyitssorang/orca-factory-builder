import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_review as fr  # noqa: E402

CLI = str(ROOT / "scripts" / "factory_review.py")

CODE_REVIEW_WITH_FAILING_CHECK = """VERDICT: CHANGES

## Findings
1. package.json — tests fail
   Check: `npm test` exited 1
2. src/a.py:3 — off by one
   continues here

## Checks
- `npm test` — fail
- `npm run lint` — pass
"""


class ParseTest(unittest.TestCase):
    def test_bom_crlf_blank_lines(self):
        self.assertEqual(fr.parse_review("﻿\r\n\r\nVERDICT: PASS\r\n").verdict, "PASS")
        self.assertEqual(fr.parse_review("﻿VERDICT: PASS\r\n").verdict, "PASS")
        self.assertEqual(fr.parse_build("﻿\r\n\r\nSTATUS: READY\r\n")[0], "READY")
        self.assertEqual(fr.parse_verify("﻿\r\n\r\nVERIFY: CONFIRMED\r\n"), "CONFIRMED")

    def test_not_run_reason(self):
        for sep in ("—", "–", "-"):
            r = fr.parse_review(f"VERDICT: NOT RUN {sep} no dev command\n")
            self.assertEqual((r.verdict, r.reason), ("NOT RUN", "no dev command"))
        r = fr.parse_review("VERDICT: NOT RUN\n")
        self.assertEqual((r.verdict, r.reason), ("NOT RUN", ""))

    def test_pass_with_reason(self):
        r = fr.parse_review("VERDICT: PASS — no user-facing change\n")
        self.assertEqual((r.verdict, r.reason), ("PASS", "no user-facing change"))

    def test_malformed_and_missing(self):
        self.assertEqual(fr.parse_review("Looks good\n").verdict, "MALFORMED")
        self.assertEqual(fr.parse_review("").verdict, "MALFORMED")
        self.assertEqual(fr.parse_review(None).verdict, "MISSING")
        self.assertEqual(fr.parse_review("VERDICT: PASSED\n").verdict, "MALFORMED")

    def test_findings_and_checks(self):
        r = fr.parse_review(CODE_REVIEW_WITH_FAILING_CHECK)
        self.assertEqual([f.n for f in r.findings], [1, 2])
        self.assertEqual(r.findings[1].location, "src/a.py:3")
        self.assertIn("continues here", r.findings[1].text)
        self.assertEqual(r.checks, {"npm test": "fail", "npm run lint": "pass"})

    def test_none_findings(self):
        r = fr.parse_review("VERDICT: CHANGES\n\n## Findings\nNone.\n")
        self.assertEqual((r.verdict, r.findings), ("CHANGES", []))

    def test_check_finding_routes_rerun(self):
        f = fr.parse_review(CODE_REVIEW_WITH_FAILING_CHECK).findings[0]
        self.assertEqual((f.check_cmd, f.exit_code, fr.route("code", f)), ("npm test", 1, "rerun"))

    def test_route_lenses(self):
        f = fr.Finding(1, "a.py:1", "x", None, None)
        self.assertEqual(fr.route("security", f), "p2-security")
        self.assertEqual(fr.route("code", f), "p2")
        self.assertEqual(fr.route("ux", f), "browser")
        self.assertEqual(fr.route("ui", f), "browser")

    def test_build(self):
        status, crit = fr.parse_build(
            "STATUS: READY\n\n## Criteria\n1. met — tests\n2. not met — missing\n")
        self.assertEqual(status, "READY")
        self.assertEqual(crit, [(1, True, "tests"), (2, False, "missing")])
        self.assertEqual(fr.parse_build(None)[0], "MISSING")
        self.assertEqual(fr.parse_build("hello")[0], "MALFORMED")

    def test_verify_formats(self):
        self.assertEqual(fr.parse_verify("VERIFY: CONFIRMED"), "CONFIRMED")
        self.assertEqual(fr.parse_verify("verdict: FALSE POSITIVE"), "FALSE POSITIVE")
        self.assertEqual(fr.parse_verify("evidence\nverdict: CONFIRMED\n"), "CONFIRMED")
        self.assertEqual(fr.parse_verify("VERIFY: UNVERIFIABLE - no server"), "UNVERIFIABLE")
        self.assertEqual(fr.parse_verify("maybe"), "UNVERIFIABLE")
        self.assertEqual(fr.parse_verify("VERIFY: CONFIRMEDISH"), "UNVERIFIABLE")
        self.assertEqual(fr.parse_verify(None), "UNVERIFIABLE")

    def test_ui_applicable(self):
        g = fr.DEFAULT_UI_GLOBS
        self.assertFalse(fr.ui_applicable(["src/api/x.py"], g))
        self.assertTrue(fr.ui_applicable(["src/components/Nav.tsx"], g))
        self.assertFalse(fr.ui_applicable(["README.md"], g))
        self.assertTrue(fr.ui_applicable(["a/b/page.html"], g))
        self.assertTrue(fr.ui_applicable(["templates/x/y.txt"], g))
        self.assertTrue(fr.ui_applicable(["src/components/x.py"], g))
        self.assertTrue(fr.ui_applicable(["src/a/components/x.py"], g))
        self.assertFalse(fr.ui_applicable([], g))


class FailClosedTest(unittest.TestCase):
    def reasons(self, review=None, lens="code", **kw):
        reviews = {l: fr.Review("PASS") for l in fr.LENSES}
        reviews["code"] = fr.Review("PASS", checks={"make test": "pass"})
        if review is not None:
            reviews[lens] = review
        data = fr.RoundData(reviews=reviews, verifies=kw.pop("verifies", {}), risk="",
                            build_status="READY", criteria=[(1, True, "e")], **kw)
        return fr.draft_reasons(data, [])

    def test_unparsed_check_lines(self):
        for line in ("- `npm test` — not run (no node)", "- `lint` — NOT RUN — x",
                     "* `b` — not run"):
            r = fr.parse_review(f"VERDICT: PASS\n\n## Checks\n{line}\n")
            self.assertEqual(list(r.checks.values()), ["not run"], line)
            self.assertTrue(self.reasons(r), line)
        for text in ("VERDICT: PASS\n\n## Checks run\n- npm test — not run\n",
                     "VERDICT: PASS\n\n## Checks\n- npm test — pass\n"):
            r = fr.parse_review(text)
            self.assertTrue(r.unparsed)
            self.assertTrue(any("unparsed check line" in x for x in self.reasons(r)))

    def test_partial_findings(self):
        r = fr.parse_review("VERDICT: CHANGES\n\n## Findings\n1. a.py — x\n 2. b.py — y\n"
                            "- 3. c.py — z\n**4.** d.py — w\n")
        self.assertEqual([f.n for f in r.findings], [1, 2, 3, 4])
        v = {("code", 1): "FALSE POSITIVE"}
        self.assertTrue(self.reasons(r, verifies=v))

    def test_duplicate_findings(self):
        r = fr.parse_review("VERDICT: CHANGES\n\n## Findings\n1. a — x\n1. b — y\n")
        v = {("code", 1): "FALSE POSITIVE"}
        self.assertIn("code duplicate finding 1", self.reasons(r, verifies=v))

    def test_criteria_fail_closed(self):
        s, c = fr.parse_build("STATUS: READY\n\n## Criteria\n1. Not met — x\n")
        self.assertEqual(c, [(1, False, "x")])
        for text in ("STATUS: READY\n\n## Criteria\n2. partially met — y\n",
                     "STATUS: READY\n", "STATUS: READY\n\n## Criteria\n"):
            with tempfile.TemporaryDirectory() as d:
                job = Path(d)
                (job / "build.md").write_text(text)
                (job / "round-1").mkdir()
                for lens in fr.LENSES:
                    (job / "round-1" / f"review-{lens}.md").write_text("VERDICT: PASS\n")
                data = fr.load_round(job, 1)
                self.assertTrue(fr.draft_reasons(data, []), text)

    def test_diff_paths(self):
        q = 'diff --git "a/x y.html" "b/x y.html"\n'
        self.assertTrue(fr.ui_applicable(fr._diff_paths(q), fr.DEFAULT_UI_GLOBS))
        plain = "--- old/page.css\t2020\n+++ new/page.css\t2020\n"
        self.assertTrue(fr.ui_applicable(fr._diff_paths(plain), fr.DEFAULT_UI_GLOBS))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.patch"
            p.write_text("something odd\n")
            out = subprocess.run([sys.executable, CLI, "ui-applicable", str(p)],
                                 capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "true")
            self.assertTrue(out.stderr)
            p.write_text("diff --git a/x.py b/x.py\n")
            out = subprocess.run([sys.executable, CLI, "ui-applicable", str(p), "--globs", ","],
                                 capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "false")

    def test_pass_with_failing_check(self):
        r = fr.parse_review("VERDICT: PASS\n\n## Checks\n- `npm test` — fail\n")
        self.assertIn("code PASS with failing check npm test", self.reasons(r))

    def test_stray_findings_line(self):
        r = fr.parse_review("VERDICT: CHANGES\n\n## Findings\n- a.py — real bug\n1. b.py — y\n")
        v = {("code", 1): "FALSE POSITIVE"}
        self.assertTrue(any("unparsed findings line" in x for x in self.reasons(r, verifies=v)))
        ok = fr.parse_review("VERDICT: PASS\n\n## Findings\nNone.\n\n## Checks\n- `t` — pass\n")
        self.assertEqual(self.reasons(ok), [])

    def test_unindented_line_after_item_is_stray(self):
        r = fr.parse_review("VERDICT: CHANGES\n\n## Findings\n1. a — x\n- b.py — real bug\n")
        self.assertEqual(r.unparsed_findings, ["- b.py — real bug"])
        v = {("code", 1): "FALSE POSITIVE"}
        self.assertTrue(any("unparsed findings line" in x for x in self.reasons(r, verifies=v)))

    def test_indented_continuation_kept(self):
        r = fr.parse_review("VERDICT: CHANGES\n\n## Findings\n1. a — x\n   a. click Save\n"
                            "   b. see error\n")
        self.assertEqual(len(r.findings), 1)
        self.assertEqual(r.unparsed_findings, [])
        self.assertIn("click Save", r.findings[0].text)

    def test_heading_variants(self):
        r = fr.parse_review("VERDICT: PASS\n\n## Check results\n- `a` — fail\n")
        self.assertEqual(r.checks, {"a": "fail"})
        self.assertTrue(self.reasons(r))
        r = fr.parse_review("VERDICT: CHANGES\n\n## Finding list\n1. a.py — x\n")
        self.assertEqual([f.n for f in r.findings], [1])
        r = fr.parse_review("VERDICT: CHANGES\n\n## Finding 2\n2. a.py — x\n")
        self.assertEqual([f.n for f in r.findings], [2])
        self.assertEqual(fr.parse_build("STATUS: READY\n\n## Criterion\n1. met — x\n")[1],
                         [(1, True, "x")])

    def test_pass_with_findings(self):
        r = fr.parse_review("VERDICT: PASS\n\n## Findings\n1. a.py — x\n")
        self.assertIn("code PASS with findings", self.reasons(r))

    def test_verdict_trailing_text(self):
        for t in ("VERDICT: PASS / CHANGES pending", "VERDICT: CHANGES pending",
                  "VERDICT: NOT RUN soon"):
            self.assertEqual(fr.parse_review(t + "\n").verdict, "MALFORMED", t)


def rd(**kw):
    ok = fr.Review("PASS", "", [], {})
    reviews = {l: ok for l in fr.LENSES}
    reviews["code"] = fr.Review("PASS", "", [], {"make test": "pass"})
    base = dict(reviews=reviews, verifies={}, risk="",
                build_status="READY", criteria=[(1, True, "e")])
    base.update(kw)
    return fr.RoundData(**base)


def changes(*ns, checks=None):
    return fr.Review("CHANGES", "", [fr.Finding(n, "a.py:1", "t", None, None) for n in ns],
                     checks or {"make test": "pass"})


class DraftTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(fr.draft_reasons(rd(), []), [])

    def test_each_alone_drafts(self):
        cases = {
            "confirmed": rd(reviews={**rd().reviews, "code": changes(1)},
                            verifies={("code", 1): "CONFIRMED"}),
            "unverifiable": rd(reviews={**rd().reviews, "security": changes(1)},
                               verifies={("security", 1): "UNVERIFIABLE"}),
            "no verify file": rd(reviews={**rd().reviews, "code": changes(1)}),
            "ux not run": rd(reviews={**rd().reviews, "ux": fr.Review("NOT RUN", "x", [], {})}),
            "ui missing": rd(reviews={**rd().reviews, "ui": fr.Review("MISSING", "", [], {})}),
            "malformed": rd(reviews={**rd().reviews, "code": fr.Review("MALFORMED", "", [], {})}),
            "unmet": rd(criteria=[(1, True, "a"), (2, False, "b")]),
            "check not run": rd(reviews={**rd().reviews,
                                         "code": fr.Review("PASS", "", [], {"npm test": "not run"})}),
            "changes w/o findings": rd(reviews={**rd().reviews, "code": changes()}),
            "blocked": rd(build_status="BLOCKED"),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertTrue(fr.draft_reasons(data, []))
        self.assertTrue(fr.draft_reasons(rd(), ["src/a.py"]))

    def test_ready_cases(self):
        fp = rd(reviews={**rd().reviews, "code": changes(1, 2)},
                verifies={("code", 1): "FALSE POSITIVE", ("code", 2): "FALSE POSITIVE"})
        self.assertEqual(fr.draft_reasons(fp, []), [])
        self.assertEqual(fr.draft_reasons(rd(risk="RISK: FLAGGED\nx"), []), [])


class RoundCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job = Path(self._tmp.name)
        (self.job / "build.md").write_text("STATUS: READY\n\n## Criteria\n1. met — ok\n")
        r = self.job / "round-1"
        r.mkdir()
        for lens in fr.LENSES:
            (r / f"review-{lens}.md").write_text("VERDICT: PASS\n")
        (r / "review-code.md").write_text(CODE_REVIEW_WITH_FAILING_CHECK)
        (r / "risk.md").write_text("RISK: FLAGGED\n")

    def cli(self, *args):
        return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True)

    def test_load_round(self):
        data = fr.load_round(self.job, 1)
        self.assertEqual(data.build_status, "READY")
        self.assertEqual(data.reviews["code"].verdict, "CHANGES")
        self.assertIn("FLAGGED", data.risk)
        (self.job / "round-1" / "verify-code-1.md").write_text("VERIFY: CONFIRMED\n")
        self.assertEqual(fr.load_round(self.job, 1).verifies, {("code", 1): "CONFIRMED"})

    def test_route_cli(self):
        p = self.cli("route", str(self.job), "1")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual([(o["lens"], o["n"], o["route"]) for o in out],
                         [("code", 1, "rerun"), ("code", 2, "p2")])
        self.assertEqual(out[0]["check_cmd"], "npm test")

    def test_decide_cli(self):
        out = json.loads(self.cli("decide", str(self.job), "1").stdout)
        self.assertTrue(out["draft"])
        self.assertTrue(out["reasons"])
        r = self.job / "round-1"
        (r / "review-code.md").write_text("VERDICT: PASS\n")
        out = json.loads(self.cli("decide", str(self.job), "1").stdout)
        self.assertEqual(out, {"draft": True, "reasons": ["code review lists no checks"]})
        (r / "review-code.md").write_text("VERDICT: PASS\n\n## Checks\n- `make test` — pass\n")
        out = json.loads(self.cli("decide", str(self.job), "1").stdout)
        self.assertEqual(out, {"draft": False, "reasons": []})

    def test_ui_applicable_cli(self):
        patch = self.job / "diff.patch"
        patch.write_text("diff --git a/src/api/x.py b/src/api/x.py\n--- a/src/api/x.py\n+++ b/src/api/x.py\n")
        self.assertEqual(self.cli("ui-applicable", str(patch)).stdout.strip(), "false")
        self.assertEqual(self.cli("ui-applicable", str(patch), "--globs", "*.py").stdout.strip(), "true")


def _code_review(cmd: str) -> str:
    return (f"VERDICT: CHANGES\n\n## Findings\n1. a.py:1 — check fails\n"
            f"  Check: `{cmd}` exited 1\n2. b.py:2 — logic\n\n## Checks\n- `{cmd}` — fail\n")


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"FACTORY_TMPDIR": str(Path(self.tmp.name) / "x")})
        env.start()
        self.addCleanup(env.stop)
        self.job, self.wt = Path(self.tmp.name) / "001-x", Path(self.tmp.name) / "wt"
        self.wt.mkdir()

    def checks(self, agents: str | None):
        if agents is not None:
            (self.wt / "AGENTS.md").write_text(agents)
        head = fr.run_checks(self.job, 1, self.wt, timeout=30)
        return head, (self.job / "round-1" / "checks.md").read_text()

    def test_pass_and_fail(self):
        head, text = self.checks("- test: true\n- build: true\n")
        self.assertEqual(head, "CHECKS: PASS")
        self.assertIn("- `true` — pass: exited 0", text)
        head, text = self.checks("- test: false\n- build: true\n")
        self.assertEqual(head, "CHECKS: FAIL")
        self.assertIn("- `false` — fail: exited 1", text)
        self.assertTrue(text.startswith("CHECKS: FAIL\n"))

    def test_no_commands_is_not_run(self):
        head, _ = self.checks("nothing here\n")
        self.assertEqual(head, "CHECKS: NOT RUN — AGENTS.md names no test or build command")
        head, _ = self.checks(None) if not (self.wt / "AGENTS.md").exists() else (head, "")
        self.assertTrue(head.startswith("CHECKS: NOT RUN"))

    def test_refused_command_is_not_run_not_failed(self):
        head, text = self.checks("- test: git status\n")
        self.assertEqual(head, "CHECKS: NOT RUN — no named command could be run")
        self.assertIn("not run: program git not allowed", text)

    def test_cli(self):
        (self.wt / "AGENTS.md").write_text("- test: true\n")
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "factory_review.py"), "checks",
                            str(self.job), "1", "--worktree", str(self.wt)],
                           capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "CHECKS: PASS"), p.stderr)


class RerunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exec_tmp = Path(self.tmp.name) / "exec-tmp"
        env = mock.patch.dict(os.environ, {"FACTORY_TMPDIR": str(self.exec_tmp)})
        env.start()
        self.addCleanup(env.stop)
        self.job = Path(self.tmp.name) / "001-x"
        (self.job / "round-1").mkdir(parents=True)
        self.wt = Path(self.tmp.name) / "wt"
        self.wt.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, cmd, n=1, timeout=30):
        (self.job / "round-1" / "review-code.md").write_text(_code_review(cmd))
        head = fr.rerun(self.job, 1, "code", n, self.wt, timeout)
        text = (self.job / "round-1" / f"verify-code-{n}.md").read_text()
        self.assertTrue(text.startswith(head))
        self.assertIn(fr.parse_verify(text), ("CONFIRMED", "FALSE POSITIVE", "UNVERIFIABLE"))
        return head, text

    def test_nonzero_confirms(self):
        head, text = self.run_cmd("ls definitely-missing-file")
        self.assertEqual(head, "VERIFY: CONFIRMED")
        self.assertIn("exit 2", text)
        self.assertIn("definitely-missing-file", text)

    def test_zero_is_false_positive(self):
        head, text = self.run_cmd("true")
        self.assertEqual(head, "VERIFY: FALSE POSITIVE")
        self.assertIn("exit 0", text)

    def test_runs_in_worktree_without_shell(self):
        (self.wt / "marker.txt").write_text("x")
        head, _ = self.run_cmd("ls marker.txt")
        self.assertEqual(head, "VERIFY: FALSE POSITIVE")

    def test_timeout_unverifiable(self):
        head, _ = self.run_cmd("sleep 5", timeout=0.3)
        self.assertTrue(head.startswith("VERIFY: UNVERIFIABLE — timed out"), head)

    def test_timeout_kills_grandchildren(self):
        pidfile = self.wt / "child.pid"
        (self.wt / "spawn.py").write_text(
            "import subprocess, sys, time\n"
            f"p = subprocess.Popen(['sleep', '30'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(30)\n")
        head, _ = self.run_cmd(f"{sys.executable} spawn.py", timeout=1)
        self.assertTrue(head.startswith("VERIFY: UNVERIFIABLE — timed out"), head)
        pid = int(pidfile.read_text())
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.1)
        else:
            self.fail(f"grandchild {pid} still running after timeout")

    def test_finding_without_command(self):
        head, _ = self.run_cmd("true", n=2)
        self.assertTrue(head.startswith("VERIFY: UNVERIFIABLE — no check command"), head)

    def test_missing_program(self):
        head, _ = self.run_cmd("no-such-program-xyz")
        self.assertTrue(head.startswith("VERIFY: UNVERIFIABLE — could not start"), head)

    def test_refused_commands_never_run(self):
        canary = self.wt / "canary"
        for cmd in (f"touch {canary}; true", f"touch {canary} && true", f"touch {canary} | cat",
                    f"touch {canary} & true", "echo $HOME", f"touch {canary} > x",
                    f"git pu\"\"sh origin", "git status", "az repos list",
                    f"curl http://x", "wget x", "ssh h", "scp a b", f"rm {canary}",
                    "sudo true", f"sh -c 'touch {canary}'", f"bash -c 'touch {canary}'",
                    f"env touch {canary}", f"/usr/bin/git status", "npm pu''sh",
                    f"npx something push", "a 'unbalanced"):
            self.assertIsNotNone(fr.rerun_refusal(cmd), cmd)
        head, text = self.run_cmd(f"sh -c 'touch {canary}'")
        self.assertEqual(head, "VERIFY: UNVERIFIABLE — command refused")
        self.assertFalse(canary.exists())

    def test_wrappers_and_interpreters_refused(self):
        for cmd in ("timeout 5 rm x", "timeout -s KILL 5 rm x", "timeout --signal=KILL 5 git st",
                    "timeout -k 2 5 git status", "nice git status", "nice -n 5 git status",
                    "nice --adjustment=5 rm x", "nohup rm x", "stdbuf -oL git log",
                    "stdbuf -o L git log", "setsid rm x", "command rm x", "exec rm x",
                    "command -p git status", "nohup nice timeout 3 git status",
                    "timeout 5", "nohup",
                    "busybox rm x", "ksh -c x", "mksh -c x", "fish -c x", "csh -c x",
                    "tcsh -c x", "pwsh -c x", "dash -c x", "zsh -c x", "/bin/ksh -c x",
                    "doas true", "su -c x",
                    "python3 -c print(1)", "python -c x", "python3.12 -c x", "python3 -Ic x",
                    "python3 --command=x", "timeout 5 python3 -c x",
                    "node -e x", "node --eval x", "node --eval=x", "node -p x", "nodejs -e x",
                    "perl -e x", "perl -E x", "perl -we x", "ruby -e x", "ruby --eval x"):
            self.assertIsNotNone(fr.rerun_refusal(cmd), cmd)

    def test_allowed_commands(self):
        for cmd in ("npm test -- cart", "npx eslint src/a.ts", "pytest -q tests/test_a.py",
                    "make check", "python3 -m pytest -q", "python3 scripts/check.py",
                    "node scripts/test.js", "timeout 300 npm test", "nice -n 5 make check",
                    "perl -w t/a.t", "python3 -m unittest -- -e"):
            self.assertIsNone(fr.rerun_refusal(cmd), cmd)

    def test_env_scrubbed(self):
        env = fr.scrubbed_env({"PATH": "/bin", "AZURE_DEVOPS_EXT_PAT": "s", "GIT_ASKPASS": "a",
                               "GITHUB_TOKEN": "t", "my_token_x": "t", "ADO_PAT": "p",
                               "PAT": "p", "HOME": "/h", "PATTERN": "keep"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h", "PATTERN": "keep"})

    def test_env_not_inherited_by_command(self):
        import os
        os.environ["FACTORY_TEST_TOKEN"] = "sekrit"
        try:
            head, text = self.run_cmd("printenv FACTORY_TEST_TOKEN")
        finally:
            del os.environ["FACTORY_TEST_TOKEN"]
        self.assertEqual(head, "VERIFY: CONFIRMED")  # printenv exits 1 when unset
        self.assertNotIn("sekrit", text)

    def test_tmpdir_is_fresh_and_removed(self):
        head, text = self.run_cmd("printenv TMPDIR")
        self.assertEqual(head, "VERIFY: FALSE POSITIVE")
        self.assertIn(str(self.exec_tmp / "factory-rerun-"), text)
        self.assertEqual(list(self.exec_tmp.iterdir()), [])

    def test_cli(self):
        (self.job / "round-1" / "review-code.md").write_text(_code_review("true"))
        out = subprocess.run([sys.executable, CLI, "rerun", str(self.job), "1", "code", "1",
                              "--worktree", str(self.wt), "--timeout", "30"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "VERIFY: FALSE POSITIVE")
        self.assertTrue((self.job / "round-1" / "verify-code-1.md").exists())


class ExecTmpdirTest(unittest.TestCase):
    def test_override_default_and_cli(self):
        with tempfile.TemporaryDirectory() as t:
            with mock.patch.dict(os.environ, {"FACTORY_TMPDIR": f"{t}/a", "XDG_CACHE_HOME": t}):
                self.assertEqual(fr.exec_tmpdir(), Path(t, "a"))
                out = subprocess.run([sys.executable, CLI, "tmpdir"], capture_output=True,
                                     text=True, env=os.environ)
                self.assertEqual(out.stdout.strip(), f"{t}/a")
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": t}):
                os.environ.pop("FACTORY_TMPDIR", None)
                p = fr.exec_tmpdir()
            self.assertEqual(p, Path(t, "factory-tmp"))
            self.assertTrue(p.is_dir())
            self.assertEqual(p.stat().st_mode & 0o777, 0o700)


class ReviewGapsTest(unittest.TestCase):
    def test_default_globs_cover_scripts_and_templates(self):
        g = fr.DEFAULT_UI_GLOBS
        for p in ("src/server.js", "lib/a.ts", "x.mjs", "y.cjs", "Views/Home/Index.cshtml",
                  "Pages/Counter.razor", "app/views/a.html.erb", "t/a.hbs", "v/a.ejs",
                  "s/a.less", "s/a.sass", "img/logo.svg"):
            self.assertTrue(fr.ui_applicable([p], g), p)
        for p in ("src/api/x.py", "README.md", "go.mod"):
            self.assertFalse(fr.ui_applicable([p], g), p)

    def test_ui_applicable_cli_server_js(self):
        with tempfile.TemporaryDirectory() as d:
            patch = Path(d) / "diff.patch"
            patch.write_text("diff --git a/src/server.js b/src/server.js\n"
                             "--- a/src/server.js\n+++ b/src/server.js\n")
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "factory_review.py"),
                                "ui-applicable", str(patch)], capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "true", r.stderr)

    def test_parse_spec_criteria(self):
        spec = ("# Spec\n\n## Goal\n1. not a criterion\n\n## Acceptance criteria\n"
                "1. Page shows X.\n   How to verify: `npm test`\n   1. sub-step\n"
                "2) Button works.\n- 3. Third.\n**4.** Fourth.\n\n## Out of scope\n5. no\n")
        self.assertEqual(fr.parse_spec_criteria(spec), [1, 2, 3, 4])
        self.assertEqual(fr.parse_spec_criteria(None), [])
        self.assertEqual(fr.parse_spec_criteria("## Goal\n1. x\n"), [])

    def _job(self, d, spec, build):
        jd = Path(d)
        (jd / "round-1").mkdir()
        (jd / "spec.md").write_text(spec)
        (jd / "build.md").write_text(build)
        for lens in fr.LENSES:
            extra = "\n## Checks\n- `make test` — pass\n" if lens == "code" else ""
            (jd / "round-1" / f"review-{lens}.md").write_text("VERDICT: PASS\n" + extra)
        return jd

    def test_unreported_criterion_drafts(self):
        spec = "## Acceptance criteria\n1. A\n2. B\n3. C\n"
        with tempfile.TemporaryDirectory() as d:
            jd = self._job(d, spec, "STATUS: READY\n\n## Criteria\n1. met — a\n3. met — c\n")
            rd = fr.load_round(jd, 1)
            self.assertEqual(rd.spec_criteria, [1, 2, 3])
            self.assertEqual(fr.draft_reasons(rd, []), ["criterion 2 not reported"])
        with tempfile.TemporaryDirectory() as d:
            jd = self._job(d, spec, "STATUS: READY\n\n## Criteria\n1. met\n2. met\n3. met\n")
            self.assertEqual(fr.draft_reasons(fr.load_round(jd, 1), []), [])

    def test_code_review_without_checks_drafts(self):
        base = {l: fr.Review("PASS") for l in fr.LENSES}
        rd = fr.RoundData(base, {}, "", "READY", [(1, True, "e")])
        self.assertIn("code review lists no checks", fr.draft_reasons(rd, []))
        rd.reviews["code"] = fr.parse_review("VERDICT: PASS\n\n## Checks\n- `pytest` — pass\n")
        self.assertEqual(fr.draft_reasons(rd, []), [])
        rd.reviews["code"] = fr.Review("NOT RUN", "x")
        self.assertNotIn("code review lists no checks", fr.draft_reasons(rd, []))


if __name__ == "__main__":
    unittest.main()
