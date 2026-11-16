import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import install as inst  # noqa: E402


def snapshot(home: Path) -> dict:
    out = {}
    for p in sorted(home.rglob("*")):
        rel = str(p.relative_to(home))
        out[rel] = None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "claude"
        (self.home / "agents").mkdir(parents=True)
        (self.home / "agents" / "review-verifier-p2.md").write_text("v")
        (self.home / "agents" / "review-verifier-p2-security.md").write_text("s")
        (self.home / "skills" / "differential-review").mkdir(parents=True)
        (self.home / "skills" / "differential-review" / "SKILL.md").write_text("d")
        self.before = snapshot(self.home)

    def test_plan_destinations(self):
        pairs = inst.plan(ROOT, self.home)
        dsts = {d.relative_to(self.home).as_posix() for _, d in pairs}
        self.assertIn("agents/builder-p7.md", dsts)
        self.assertIn("skills/factory/SKILL.md", dsts)
        self.assertIn("skills/factory/references/worker-tasks.md", dsts)
        self.assertIn("skills/factory/scripts/factory_dashboard.py", dsts)
        self.assertIn("skills/factory/dashboard/index.html", dsts)
        self.assertNotIn("skills/factory/scripts/lint_roster.py", dsts)
        self.assertFalse([d for d in dsts if "__pycache__" in d or d.endswith(".pyc")])

    def test_install_uninstall_roundtrip(self):
        inst.install(ROOT, self.home)
        self.assertTrue((self.home / inst.MANIFEST_NAME).exists())
        self.assertTrue((self.home / "skills/factory/SKILL.md").exists())
        self.assertEqual(inst.uninstall(self.home), [])
        self.assertEqual(snapshot(self.home), self.before)

    def test_refuses_existing_destination(self):
        (self.home / "agents" / "builder-p7.md").write_text("mine")
        before = snapshot(self.home)
        with self.assertRaises(SystemExit):
            inst.install(ROOT, self.home)
        self.assertEqual(snapshot(self.home), before)

    def test_refuses_p7_name_elsewhere_under_agents(self):
        sub = self.home / "agents" / "nested"
        sub.mkdir()
        (sub / "other-p7.md").write_text("x")
        before = snapshot(self.home)
        with self.assertRaises(SystemExit):
            inst.install(ROOT, self.home)
        self.assertEqual(snapshot(self.home), before)

    def test_refuses_when_manifest_exists(self):
        inst.install(ROOT, self.home)
        with self.assertRaises(SystemExit) as cm:
            inst.install(ROOT, self.home)
        self.assertIn("already installed", str(cm.exception))

    def test_missing_dependency_installed_from_vendor(self):
        import shutil
        shutil.rmtree(self.home / "skills" / "differential-review")
        (self.home / "agents" / "review-verifier-p2.md").unlink()
        inst.install(ROOT, self.home)
        self.assertTrue((self.home / "skills/differential-review/SKILL.md").is_file())
        self.assertTrue((self.home / "skills/differential-review/LICENSE.upstream").is_file())
        self.assertTrue((self.home / "agents/review-verifier-p2.md").is_file())
        inst.uninstall(self.home)
        self.assertFalse((self.home / "skills/differential-review").exists())
        self.assertFalse((self.home / "agents/review-verifier-p2.md").exists())
        self.assertEqual((self.home / "agents/review-verifier-p2-security.md").read_text(), "s")

    def test_existing_dependency_kept_untouched(self):
        inst.install(ROOT, self.home)
        self.assertEqual((self.home / "skills/differential-review/SKILL.md").read_text(), "d")
        inst.uninstall(self.home)
        self.assertEqual((self.home / "skills/differential-review/SKILL.md").read_text(), "d")

    def test_copy_failure_rolls_back(self):
        real = inst.copy_file
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 3:
                raise OSError("disk full")
            return real(src, dst)

        with mock.patch.object(inst, "copy_file", flaky):
            with self.assertRaises(OSError):
                inst.install(ROOT, self.home)
        self.assertEqual(len(calls), 3)
        self.assertEqual(snapshot(self.home), self.before)

    def test_partial_write_rolled_back(self):
        real = inst.copy_file
        calls = []

        def partial(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                Path(dst).write_bytes(b"half")
                raise OSError("interrupted")
            return real(src, dst)

        with mock.patch.object(inst, "copy_file", partial):
            with self.assertRaises(OSError):
                inst.install(ROOT, self.home)
        self.assertEqual(snapshot(self.home), self.before)

    def test_tampered_manifest_deletes_nothing(self):
        import json
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("keep")
        for bad_files, bad_dirs in ([{"path": "../outside.txt", "sha256": "x"}], []), \
                ([], [""]), ([], ["../claude"]), ([{"path": "", "sha256": "x"}], []):
            with self.subTest(bad_files=bad_files, bad_dirs=bad_dirs):
                inst.install(ROOT, self.home)
                m = self.home / inst.MANIFEST_NAME
                data = json.loads(m.read_text())
                data["files"].extend(bad_files)
                data["dirs"].extend(bad_dirs)
                m.write_text(json.dumps(data))
                installed = snapshot(self.home)
                with self.assertRaises(SystemExit):
                    inst.uninstall(self.home)
                self.assertEqual(snapshot(self.home), installed)
                self.assertEqual(outside.read_text(), "keep")
                data["files"] = [r for r in data["files"] if r["sha256"] != "x"]
                data["dirs"] = [d for d in data["dirs"] if d not in ("", "../claude")]
                m.write_text(json.dumps(data))
                inst.uninstall(self.home)
                self.assertEqual(snapshot(self.home), self.before)

    def test_symlinked_parent_outside_home_refused(self):
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        (self.home / "skills" / "factory").symlink_to(outside)
        before = snapshot(self.home)
        with self.assertRaises(SystemExit):
            inst.install(ROOT, self.home)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_edited_file_kept_and_reported(self):
        inst.install(ROOT, self.home)
        edited = self.home / "agents" / "builder-p7.md"
        edited.write_text(edited.read_text() + "\nlocal edit\n")
        kept = inst.uninstall(self.home)
        self.assertEqual(kept, ["agents/builder-p7.md"])
        self.assertTrue(edited.exists())
        self.assertFalse((self.home / "skills/factory/SKILL.md").exists())
        self.assertTrue((self.home / inst.MANIFEST_NAME).exists())

    def test_installed_dashboard_finds_page(self):
        inst.install(ROOT, self.home)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import factory_dashboard as d; "
                "print(d.DEFAULT_PAGE.resolve())")
        out = subprocess.run(
            [sys.executable, "-I", "-c", code, str(self.home / "skills/factory/scripts")],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(Path(out), (self.home / "skills/factory/dashboard/index.html").resolve())
        self.assertTrue(Path(out).is_file())

    def test_installed_agents_match_roster_byte_for_byte(self):
        inst.install(ROOT, self.home)
        for f in (ROOT / "roster").glob("*.md"):
            self.assertEqual((self.home / "agents" / f.name).read_bytes(), f.read_bytes())

    def test_symlink_source_refused(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "r"
            (root / "roster").mkdir(parents=True)
            (root / "roster" / "a-p7.md").symlink_to(ROOT / "roster" / "builder-p7.md")
            with mock.patch.object(inst.lint_roster, "lint", return_value=[]):
                with self.assertRaises(SystemExit):
                    inst.plan(root, self.home)


if __name__ == "__main__":
    unittest.main()
