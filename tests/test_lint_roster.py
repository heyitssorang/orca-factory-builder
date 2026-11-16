import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_review as fr  # noqa: E402
import lint_roster as lr  # noqa: E402

BAD = ROOT / "tests" / "fixtures" / "roster-bad"
GOOD_SKILL = ROOT / "tests" / "fixtures" / "skill-good"
CLI = str(ROOT / "scripts" / "lint_roster.py")


class Overlay:
    """Copy the real roster and lock to a temp root, then lay fixture files over it."""

    def __init__(self, *overlays: Path):
        self.overlays = overlays

    def __enter__(self) -> Path:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        shutil.copytree(ROOT / "roster", root / "roster")
        shutil.copy(ROOT / "agents.lock", root / "agents.lock")
        for ov in self.overlays:
            shutil.copytree(ov, root, dirs_exist_ok=True)
        return root

    def __exit__(self, *exc):
        self.tmp.cleanup()


def lint_case(case: str) -> list[str]:
    with Overlay(BAD / case) as root:
        return lr.lint(root)


class BadFixtureTest(unittest.TestCase):
    def assertMsg(self, case, msg):
        errs = lint_case(case)
        self.assertIn(msg, errs, f"{case}: got {errs}")

    def test_name_mismatch(self):
        self.assertMsg("name-mismatch", "builder-p7: frontmatter name is 'builder-p8'")

    def test_model_differs(self):
        self.assertMsg("model-differs", "spec-writer-p7: model differs from lock")

    def test_tools_differ(self):
        self.assertMsg("tools-differ", "reviewer-code-p7: tools differ from lock")

    def test_mode_forbidden(self):
        self.assertMsg("mode-forbidden", "reviewer-security-p7: read-only role has ['Bash']")

    def test_always_forbidden(self):
        self.assertMsg("always-forbidden", "builder-p7: writer role has ['Agent']")

    def test_screenshot_not_allowed(self):
        self.assertMsg("screenshot-not-allowed", "reviewer-ux-p7: take_screenshot not allowed")

    def test_fill_forbidden(self):
        self.assertMsg("fill-forbidden", "reviewer-ui-p7: forbidden tool fill")

    def test_mcp_outside_allowlist(self):
        self.assertMsg("mcp-outside-allowlist",
                       "browser-verifier-p7: mcp tool outside allow-list lighthouse_audit")

    def test_mcp_wrong_mode(self):
        self.assertMsg("mcp-wrong-mode",
                       "risk-flagger-p7: mcp tools only allowed in mcp-read-only mode")

    def test_over_90_lines(self):
        errs = lint_case("over-90-lines")
        self.assertTrue(any(re.fullmatch(r"reviewer-code-p7: \d+ lines \(max 90\)", e)
                            for e in errs), errs)

    def test_missing_footer_phrase(self):
        self.assertMsg("missing-footer", "risk-flagger-p7: constraints footer missing 'Never push'")

    def test_missing_format_token(self):
        self.assertMsg("missing-format-token",
                       "browser-verifier-p7: missing format token 'VERIFY: FALSE POSITIVE'")

    def test_skill_not_allowed(self):
        self.assertMsg("skill-not-allowed", "builder-p7: skill 'differential-review' not allowed")

    def test_hooks(self):
        self.assertMsg("hooks", "spec-writer-p7: frontmatter hooks not allowed")

    def test_unlisted_file(self):
        self.assertMsg("unlisted-file", "roster files not in agents.lock: ['extra-p7']")

    def test_lock_mode_model_against_global_constraints(self):
        errs = lint_case("lock-mode-model")
        self.assertIn("spec-writer-p7: lock model is 'claude-opus-5-5', want 'claude-sonnet-5-5'",
                      errs)
        self.assertIn("risk-flagger-p7: lock mode is 'runner', want 'read-only'", errs)

    def test_no_frontmatter(self):
        self.assertMsg("no-frontmatter", "risk-flagger-p7: no frontmatter")

    def test_every_bad_fixture_has_a_test(self):
        tested = {m.group(1) for m in re.finditer(r'"([a-z0-9-]+)"',
                                                  Path(__file__).read_text())}
        for d in BAD.iterdir():
            self.assertIn(d.name, tested, f"fixture {d.name} has no test")


class McpPrefixTest(unittest.TestCase):
    def test_plugin_server_tools_refused(self):
        errs = []
        tool = "mcp__plugin_chrome-devtools-mcp_chrome-devtools__navigate_page"
        lr._check_tools("reviewer-ux-p7", "mcp-read-only", {"Read", tool}, errs.append)
        self.assertEqual(errs, [f"reviewer-ux-p7: mcp tool outside allow-list {tool}"])

    def test_project_server_tools_allowed(self):
        errs = []
        lr._check_tools("reviewer-ux-p7", "mcp-read-only",
                        {"Read", "mcp__chrome-devtools__navigate_page"}, errs.append)
        self.assertEqual(errs, [])

    def test_personas_use_project_server_only(self):
        for name in ("reviewer-ux-p7", "reviewer-ui-p7", "browser-verifier-p7"):
            text = (ROOT / "roster" / f"{name}.md").read_text()
            self.assertIn("mcp__chrome-devtools__navigate_page", text, name)
            self.assertNotIn("mcp__plugin_", text, name)


class LockTest(unittest.TestCase):
    def lint_with_lock(self, mutate) -> list[str]:
        with Overlay() as root:
            lock = json.loads((root / "agents.lock").read_text())
            mutate(lock)
            (root / "agents.lock").write_text(json.dumps(lock))
            return lr.lint(root)

    def test_wrong_pack_and_source(self):
        def m(lock):
            lock["rows"][0]["pack"] = "p6"
            lock["rows"][0]["source"] = "upstream"
        errs = self.lint_with_lock(m)
        self.assertIn("spec-writer-p7: pack is 'p6', want 'p7'", errs)
        self.assertIn("spec-writer-p7: source is 'upstream', want 'in-house'", errs)

    def test_duplicate_and_missing_row(self):
        def m(lock):
            lock["rows"] = [r for r in lock["rows"] if r["install_name"] != "risk-flagger-p7"]
            lock["rows"].append(dict(lock["rows"][0]))
        errs = self.lint_with_lock(m)
        self.assertIn("duplicate install_name spec-writer-p7", errs)
        self.assertIn("risk-flagger-p7: missing from agents.lock", errs)

    def test_unknown_mode(self):
        errs = self.lint_with_lock(lambda lock: lock["rows"][1].update(mode="god"))
        self.assertIn("builder-p7: unknown mode 'god'", errs)

    def test_install_name_suffix(self):
        errs = self.lint_with_lock(lambda lock: lock["rows"][1].update(install_name="builder"))
        self.assertIn("builder: install_name must end with -p7", errs)

    def test_unreadable_lock(self):
        with Overlay() as root:
            (root / "agents.lock").write_text("{not json")
            errs = lr.lint(root)
        self.assertTrue(errs and errs[0].startswith("agents.lock: "), errs)


class SkillTest(unittest.TestCase):
    def test_no_skill_is_fine(self):
        with Overlay() as root:
            self.assertFalse((root / "skills").exists())
            self.assertEqual(lr.lint(root), [])

    def test_good_skill_clean(self):
        with Overlay(GOOD_SKILL) as root:
            self.assertEqual(lr.lint(root), [])

    def test_skill_unknown_agent(self):
        self.assertIn("SKILL.md: names unknown agent code-reviewer-p2",
                      lint_case("skill-unknown-agent"))

    def test_skill_missing_agent(self):
        self.assertIn("SKILL.md: does not name risk-flagger-p7",
                      lint_case("skill-missing-agent"))

    def test_skill_too_long(self):
        with Overlay(GOOD_SKILL) as root:
            p = root / "skills" / "factory" / "SKILL.md"
            p.write_text(p.read_text() + "x\n" * 500)
            self.assertIn("SKILL.md: over 500 lines", lr.lint(root))


class InstalledTest(unittest.TestCase):
    def test_collision_and_duplicate(self):
        with Overlay() as root, tempfile.TemporaryDirectory() as h:
            agents = Path(h) / "agents"
            agents.mkdir()
            (agents / "builder-p7.md").write_text("---\nname: builder-p7\n---\nsomeone else's\n")
            shutil.copy(root / "roster" / "risk-flagger-p7.md", agents / "risk-flagger-p7.md")
            (agents / "a.md").write_text("---\nname: other-x\n---\n")
            (agents / "b.md").write_text("---\nname: other-x\n---\n")
            errs = lr.lint(root, installed=True, home=Path(h))
        self.assertIn("builder-p7: installed agent differs from roster/builder-p7.md", errs)
        self.assertFalse(any(e.startswith("risk-flagger-p7:") for e in errs), errs)
        self.assertTrue(any(e.startswith("agent name 'other-x' defined twice") for e in errs))

    def test_installed_clean_when_absent(self):
        with Overlay() as root, tempfile.TemporaryDirectory() as h:
            self.assertEqual(lr.lint(root, installed=True, home=Path(h)), [])


class RealRosterTest(unittest.TestCase):
    def test_real_roster_clean(self):
        self.assertEqual(lr.lint(ROOT), [])

    def test_skill_clean(self):
        skill = ROOT / "skills" / "factory" / "SKILL.md"
        self.assertTrue(skill.exists(), "skills/factory/SKILL.md missing")
        self.assertEqual([e for e in lr.lint(ROOT) if e.startswith("SKILL.md")], [])
        text = skill.read_text(encoding="utf-8")
        for cmd in ("/factory <feature> [--repo <path>]", "/factory next", "/factory run",
                    "/factory init", "/factory backlog", "/factory migrate",
                    "/factory accept", "/factory ship", "/factory rework", "/factory resume",
                    "/factory status", "/factory dashboard", "/factory close"):
            self.assertIn(cmd, text)
        for s in ("factory_integrate.py finish-job", "factory_integrate.py ship-gate",
                  "factory_push.py ship", "factory_review.py decide", "factory_pr_body.py build",
                  "factory_dashboard.py ensure", "xvfb-run", "takeover",
                  "factory_push.py config-check", "factory_review.py rerun", "can-claim",
                  "enabledMcpjsonServers", "chrome-devtools-mcp@1.10.1",
                  "core.hooksPath=/dev/null", "--force-move", "--pushed no",
                  "hold kind `refused`"):
            self.assertIn(s, text)
        for s in ("az repos", "pr.json", "--pr-comments", "factory_push.py push "):
            self.assertNotIn(s, text)
        tasks = ROOT / "skills" / "factory" / "references" / "worker-tasks.md"
        self.assertTrue(tasks.exists(), "references/worker-tasks.md missing")

    def test_cli_exit_codes(self):
        ok = subprocess.run([sys.executable, CLI], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        with Overlay(BAD / "fill-forbidden") as root:
            bad = subprocess.run([sys.executable, CLI, "--root", str(root)],
                                 capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("forbidden tool fill", bad.stdout)


def example(name: str, k: int = 0) -> str:
    """The k-th fenced block in a persona: its documented output examples."""
    text = (ROOT / "roster" / f"{name}.md").read_text()
    blocks = re.findall(r"^```[a-z]*\n(.*?)^```", text, re.S | re.M)
    assert len(blocks) > k, f"{name}: no example block {k}"
    return blocks[k]


class PersonaParserAgreementTest(unittest.TestCase):
    """Each persona's example output parses to the intended verdict."""

    def assertCleanChanges(self, name):
        r = fr.parse_review(example(name))
        self.assertEqual(r.verdict, "CHANGES", name)
        self.assertTrue(r.findings, name)
        self.assertEqual((r.unparsed, r.unparsed_findings), ([], []), name)
        reviews = {"security": r, "code": r, "ux": r, "ui": r}
        if not r.checks:  # a code review without checks is its own draft reason
            reviews["code"] = fr.Review("PASS", checks={"make test": "pass"})
        self.assertEqual(fr.draft_reasons(
            fr.RoundData(reviews,
                         {(lens, f.n): "FALSE POSITIVE" for lens in fr.LENSES
                          for f in r.findings}, "", "READY", [(1, True, "x")]), []), [],
            name)
        return r

    def test_reviewer_security(self):
        r = self.assertCleanChanges("reviewer-security-p7")
        self.assertRegex(r.findings[0].location, r"^\S+:\d+$")
        self.assertEqual(fr.route("security", r.findings[0]), "p2-security")

    def test_round_two_unfixed_prior_is_relisted(self):
        text = example("reviewer-security-p7", 1)
        r = fr.parse_review(text)
        self.assertEqual(r.verdict, "CHANGES")
        self.assertEqual(len(r.findings), 1)
        self.assertEqual((r.unparsed, r.unparsed_findings), ([], []))
        self.assertRegex(text, r"(?m)^## Prior findings\n- 1\.1 not fixed — ")
        self.assertIn("prior 1.1", r.findings[0].text)

    def test_reviewer_code(self):
        r = self.assertCleanChanges("reviewer-code-p7")
        routes = {fr.route("code", f) for f in r.findings}
        self.assertEqual(routes, {"rerun", "p2"})
        failing = [f for f in r.findings if f.check_cmd]
        self.assertEqual(r.checks.get(failing[0].check_cmd), "fail")
        self.assertIsInstance(failing[0].exit_code, int)

    def test_reviewer_ux_and_ui(self):
        for name in ("reviewer-ux-p7", "reviewer-ui-p7"):
            r = self.assertCleanChanges(name)
            self.assertEqual(len(r.findings), 1, name)
            f = r.findings[0]
            self.assertRegex(f.location, r"^https?://\S+ @ (1280|390)$", name)
            self.assertRegex(f.text, r"(?m)^a\. .*\n^b\. ", name)

    def test_builder(self):
        status, crit = fr.parse_build(example("builder-p7"))
        _, _, unparsed, has = fr.parse_build_full(example("builder-p7"))
        self.assertEqual(status, "READY")
        self.assertTrue(crit and all(met for _, met, _ in crit))
        self.assertEqual((unparsed, has), ([], True))

    def test_browser_verifier(self):
        self.assertEqual(fr.parse_verify(example("browser-verifier-p7")), "CONFIRMED")

    def test_risk_flagger(self):
        self.assertRegex(example("risk-flagger-p7").splitlines()[0], r"^RISK: (NONE|FLAGGED)$")


if __name__ == "__main__":
    unittest.main()
