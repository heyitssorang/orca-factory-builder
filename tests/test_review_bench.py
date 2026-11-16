import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
import review_bench as rb  # noqa: E402


class CaseTests(unittest.TestCase):
    def repo(self, case):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        repo = Path(d.name) / "r"
        shutil.copytree(rb.HERE / "seed", repo, ignore=shutil.ignore_patterns("__pycache__"))
        rb.apply_case(case, repo)
        return repo

    def test_defects_apply_and_change_the_file(self):
        for c in rb.CASES:
            repo = self.repo(c)
            for f in c["files"]:
                ref = (rb.HERE / "reference" / f).read_text()
                got = (repo / f).read_text()
                if c["defect"] and f == c["defect"][0]:
                    self.assertNotEqual(ref, got, c["id"])
                else:
                    self.assertEqual(ref, got, c["id"])

    def test_every_case_still_builds_and_suite_passes(self):
        for c in rb.CASES:
            repo = self.repo(c)
            for cmd in (["python3", "-m", "compileall", "-q", "src"],
                        ["python3", "-m", "unittest", "discover", "-s", "tests"]):
                p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                                   env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
                self.assertEqual(p.returncode, 0, f"{c['id']}: {p.stderr}")

    def test_spec_drops_cli_and_tests_sentences(self):
        for c in rb.CASES:
            s = rb.case_spec(c)
            self.assertNotIn("Add unit tests", s)
            self.assertNotIn("into the CLI", s)
            self.assertIn("## Acceptance criteria", s)

    def test_controls_are_clean_and_ids_unique(self):
        ids = [c["id"] for c in rb.CASES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(sum(1 for c in rb.CASES if not c["defect"]), 2)

    def test_verdict_and_findings_parsing(self):
        self.assertEqual(rb.verdict("VERDICT: CHANGES\n"), "CHANGES")
        self.assertEqual(rb.verdict("RISK: FLAGGED"), "FLAGGED")
        self.assertEqual(rb.verdict(None), "MISSING")
        self.assertEqual(rb.verdict("hello"), "MALFORMED")
        self.assertEqual(rb.n_findings("## Findings\n1. a.py:1 — x\n  detail\n2. b — y\n"), 2)


if __name__ == "__main__":
    unittest.main()
