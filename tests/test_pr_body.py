import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_pr_body as fpb  # noqa: E402
import factory_review as fr  # noqa: E402

CLI = str(ROOT / "scripts" / "factory_pr_body.py")
JOB = {"id": "job-0001", "feature": "Add thing", "round": 1, "work_item": 7, "overlaps": []}


def rd_with(findings, risk="RISK: NONE\n", verifies=None, lens="code"):
    reviews = {l: fr.Review("PASS") for l in fr.LENSES}
    reviews[lens] = fr.Review("CHANGES", findings=findings)
    return fr.RoundData(reviews, verifies or {}, risk, "READY", [(1, True, "done")])


class SanitizeTest(unittest.TestCase):
    def test_sanitize(self):
        s = fpb.sanitize('<img src=x onerror=a>Hi @<5A1B> and @alice see #12 and #7', {7})
        self.assertNotIn("<", s)
        self.assertNotIn("@<", s)
        self.assertNotIn("@alice", s)
        self.assertNotIn("#12", s)
        self.assertIn("#7", s)

    def test_inner_text_kept_and_macros_images_links(self):
        s = fpb.sanitize('<b>bold</b> [[_TOC_]] ![x](http://e/i.png) [t](javascript:alert(1)) [ok](http://a.b)', set())
        self.assertIn("bold", s)
        self.assertNotIn("_TOC_", s)
        self.assertNotIn("![", s)
        self.assertNotIn("javascript", s)
        self.assertIn("[ok](http://a.b)", s)


class SanitizeSafetyTest(unittest.TestCase):
    def test_hash_before_at(self):
        s = fpb.sanitize("@#12alice", {77})
        self.assertNotIn("@alice", s)

    def test_encoded_schemes(self):
        for t in ("javascript&#x3a;alert(1)", "javascript&colon;x", "data:text/html,x",
                  "vbscript:x", "java\nscript:x"):
            s = fpb.sanitize(f"[t]({t})", set())
            self.assertEqual(s, "t", t)

    def test_scheme_probes(self):
        for t in ("[t](<b>javascript:x)", "[t](java<i>script:x)", "[a[b]](javascript:x)",
                  "see javascript:alert(1) now"):
            s = fpb.sanitize(t, set())
            self.assertNotRegex(s.replace("\u200b", "X"), r"(?i)javascript:")
            self.assertNotRegex(s, r"(?i)javascript:")
        s = fpb.sanitize("data: 3 rows", set())
        self.assertIn("3 rows", s)
        self.assertIn("data", s)

    def test_finding_cut_keeps_no_partial_item(self):
        job = dict(JOB, work_item=1234)
        for k in range(594, 604):
            rd = rd_with([fr.Finding(1, "a.py:1", "a" * (k - 1) + " #1234 tail")],
                         verifies={("code", 1): "CONFIRMED"})
            rep = fpb.report_body(job, rd, [])
            self.assertNotRegex(rep, r"#1(?!\d)")
            self.assertNotRegex(rep, r"#12(?!34)")
        rd = rd_with([fr.Finding(1, "a.py:1", "a" * 597 + "#1234")], verifies={("code", 1): "CONFIRMED"})
        self.assertNotIn("#1", fpb.report_body(job, rd, []))




class BodyTest(unittest.TestCase):



    def test_finding_cap_600(self):
        rd = rd_with([fr.Finding(1, "a.py:1", "Z" * 5000)], verifies={("code", 1): "CONFIRMED"})
        rep = fpb.report_body(JOB, rd, ["first rework"])
        self.assertEqual(rep.count("Z"), 600)
        self.assertIn("first rework", rep)

    def test_report_lists_false_positive_and_unverifiable(self):
        rd = rd_with([fr.Finding(1, "a.py:1", "FPTEXT"), fr.Finding(2, "b.py:2", "UNVTEXT")],
                     verifies={("code", 1): "FALSE POSITIVE"})
        rep = fpb.report_body(JOB, rd, [])
        self.assertIn("Dropped as false positives", rep)
        self.assertIn("FPTEXT", rep)
        self.assertIn("UNVERIFIABLE", fpb.report_body(JOB, rd, []).upper())

    def test_risk_heads_body(self):
        rd = rd_with([], risk="RISK: FLAGGED\n- touches <b>auth</b> @bob\n")
        body = fpb.report_body(JOB, rd, [])
        att = body.index("Needs your attention")
        self.assertLess(att, body.index("touches"))
        self.assertLess(body.index("touches"), body.index("## Criteria"))
        self.assertNotIn("@bob", body)
        self.assertNotIn("<b>", body)

    def test_risk_flagged_with_reason_on_first_line(self):
        rd = rd_with([], risk="RISK: FLAGGED — risk worker failed\n")
        body = fpb.report_body(JOB, rd, [])
        self.assertIn("Risk flagged: risk worker failed", body)
        # only an exact RISK: NONE is "nothing flagged"; unknown first lines count as flagged
        self.assertEqual(fpb._risk_lines(JOB, rd_with([], risk="RISK: FLAGGEDX\n")),
                         ["Risk flagged: RISK: FLAGGEDX"])

    def test_risk_none_not_shown(self):
        for risk in ("RISK: NONE\n", "\ufeff  RISK:  NONE \r\n", "\n\nRISK: NONE"):
            body = fpb.report_body(JOB, rd_with([], risk=risk), [])
            self.assertIn("Nothing flagged.", body, repr(risk))

    def test_risk_unknown_or_empty_counts_as_flagged(self):
        for risk in ("RISK: OK\n- nothing\n", "", "RISK: none\n"):
            body = fpb.report_body(JOB, rd_with([], risk=risk), [])
            self.assertNotIn("Nothing flagged.", body, repr(risk))
            self.assertIn("Risk flagged", body)

    def test_risk_first_line_variants(self):
        flags = "\n## Flags\n1. auth/login.py:12 — session handling changed\n"
        for first in ("RISK: FLAGGED (2 items)", "**RISK: FLAGGED**", "RISK: FLAGGED:",
                      "RISK: FLAGGED", "RISK: FLAGGED — see below"):
            lines = fpb._risk_lines(JOB, rd_with([], risk=first + flags))
            body = fpb.report_body(JOB, rd_with([], risk=first + flags), [])
            self.assertNotIn("Nothing flagged.", body, first)
            self.assertTrue(any("session handling changed" in ln for ln in lines), (first, lines))
            self.assertFalse(any("Flags" in ln for ln in lines), (first, lines))
            self.assertFalse(any("RISK" in ln for ln in lines), (first, lines))
        self.assertIn("Risk flagged: (2 items)",
                      fpb._risk_lines(JOB, rd_with([], risk="RISK: FLAGGED (2 items)\n")))

    def test_risk_not_verified_section_is_not_a_flag(self):
        risk = ("RISK: FLAGGED\n\n## Flags\n1. notes.py:24 — returns the removed note\n"
                "- second flag\n\n## Not verified\n- whether CI runs the tests\n"
                "* callers outside this repo\n")
        self.assertEqual(fpb._risk_lines(JOB, rd_with([], risk=risk)), [
            "Risk flagged: notes.py:24 — returns the removed note",
            "Risk flagged: second flag",
            "Risk check could not verify: whether CI runs the tests",
            "Risk check could not verify: callers outside this repo"])
        # only the not-verified items: still flagged (the first line says so)
        lines = fpb._risk_lines(JOB, rd_with([], risk="RISK: FLAGGED\n## Not verified\n- x\n"))
        self.assertEqual(lines, ["Risk check could not verify: x"])
        self.assertIn("Risk flagged.", fpb._risk_lines(JOB, rd_with([], risk="RISK: FLAGGED\n")))


    def test_entity_encoded_scheme_neutralised(self):
        for src in ("[a[b]](javascript&colon;x)", "javascript&colon;alert(1)",
                    "JaVaScRiPt&#58;x", "data&#x3A;text/html", "vbscript&#0058x",
                    "javascript&#x003a;x"):
            out = fpb.sanitize(src, set())
            self.assertNotRegex(out, r"(?i)(javascript|vbscript|data)\s*(:|&colon;|&#0*58;?|&#x0*3a;?)",
                                src)

    def test_overlaps_listed(self):
        job = dict(JOB, overlaps=[{"with": "job-0002", "paths": ["x.py"]}])
        body = fpb.report_body(job, rd_with([]), [], [])
        self.assertIn("job-0002", body)
        self.assertNotIn("Nothing flagged.", body)

    def test_overlap_without_paths_skipped(self):
        for paths in ([], None):
            job = dict(JOB, overlaps=[{"with": "origin/main", "paths": paths}])
            body = fpb.report_body(job, rd_with([]), [], [])
            self.assertNotIn("Overlaps with", body, repr(paths))
            self.assertIn("Nothing flagged.", body, repr(paths))
        job = dict(JOB, overlaps=[{"with": "origin/main", "paths": []},
                                  {"with": "job-0002", "paths": ["x.py"]}])
        body = fpb.report_body(job, rd_with([]), [], [])
        self.assertNotIn("origin/main", body)
        self.assertIn("Overlaps with job-0002: x.py", body)


class CliTest(unittest.TestCase):
    def test_build(self):
        with tempfile.TemporaryDirectory() as d:
            jd = Path(d)
            (jd / "round-1").mkdir()
            (jd / "job.json").write_text(json.dumps(
                dict(JOB, overlaps=[{"with": "origin/main", "paths": ["a.py"]}])))
            (jd / "build.md").write_text("STATUS: READY\n\n## Criteria\n1. met — ok\n")
            (jd / "rework-1.md").write_text("\nFix the thing\nmore\n")
            (jd / "rework-2.md").write_text("## Note\nSecond pass please\n")
            for l in fr.LENSES:
                (jd / "round-1" / f"review-{l}.md").write_text("VERDICT: PASS\n")
            r = subprocess.run([sys.executable, CLI, "build", str(jd), "1"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertTrue(out["draft"])
            self.assertIn("overlaps with default branch: a.py", out["reasons"])
            self.assertFalse((jd / "pr.md").exists())
            rep = (jd / "report.md").read_text()
            self.assertIn("1. Fix the thing", rep)
            self.assertIn("2. Second pass please", rep)
            self.assertNotIn("## Note", rep)
            self.assertTrue(rep.startswith("## Needs your attention"))
            self.assertIn("overlaps with default branch: a.py", rep)
            self.assertNotIn("/home/", rep)
            self.assertNotIn(str(jd), rep)


class AttentionTest(unittest.TestCase):
    def test_reasons_and_risk_head_report(self):
        rd = rd_with([fr.Finding(1, "a.py:1", "bad")], risk="RISK: FLAGGED\n- touches auth\n",
                     verifies={("code", 1): "CONFIRMED"})
        rep = fpb.report_body(JOB, rd, [], ["confirmed finding in code review"])
        self.assertTrue(rep.startswith("## Needs your attention"))
        att, crit = rep.index("confirmed finding in code review"), rep.index("## Criteria")
        self.assertLess(att, crit)
        self.assertLess(rep.index("Risk flagged: touches auth"), crit)
        self.assertNotIn("Nothing flagged.", rep)
        self.assertIn("Nothing flagged.", fpb.report_body(JOB, rd_with([]), []))

    def test_no_length_cap_beyond_finding_cap(self):
        rep = fpb.report_body(JOB, rd_with([]), [], ["r" * 6000])
        self.assertGreater(len(rep), 4000)
        self.assertNotIn("(truncated)", rep)


class ForeignRefsTest(unittest.TestCase):
    def test_foreign_refs_listed_under_attention(self):
        body = fpb.report_body(JOB, rd_with([], risk="RISK: NONE\n"), [], [],
                               ["refs/heads/teammate", "refs/tags/v2"])
        att = body.index("Needs your attention")
        self.assertLess(att, body.index("refs/heads/teammate"))
        self.assertLess(body.index("refs/tags/v2"), body.index("## Criteria"))
        self.assertNotIn("Nothing flagged.", body)

    def test_cli_reads_refs_compare(self):
        with tempfile.TemporaryDirectory() as d:
            jd = Path(d)
            (jd / "round-1").mkdir()
            (jd / "job.json").write_text(json.dumps(JOB))
            (jd / "build.md").write_text("STATUS: READY\n\n## Criteria\n1. met — ok\n")
            for l in fr.LENSES:
                (jd / "round-1" / f"review-{l}.md").write_text("VERDICT: PASS\n")
            (jd / "round-1" / "refs-compare.json").write_text(json.dumps(
                {"suspicious": [], "foreign": ["refs/heads/someone-else"]}))
            r = subprocess.run([sys.executable, CLI, "build", str(jd), "1"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("refs/heads/someone-else", (jd / "report.md").read_text())
            (jd / "round-1" / "refs-compare.json").write_text("[not json")
            r = subprocess.run([sys.executable, CLI, "build", str(jd), "1"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
