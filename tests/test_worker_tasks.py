import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = (ROOT / "skills/factory/references/worker-tasks.md").read_text(encoding="utf-8")
SKILL = (ROOT / "skills/factory/SKILL.md").read_text(encoding="utf-8")


def task(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", TASKS, re.S | re.M)
    assert m, title
    return m.group(1)


class GroupedReviewTasks(unittest.TestCase):
    def test_security_and_code_share_one_worker_in_parallel(self):
        t = task("reviewer (security, code)")
        self.assertIn("reviewer-security-p7", t)
        self.assertIn("reviewer-code-p7", t)
        self.assertIn("ONE message", t)
        self.assertIn("Never pass one reviewer's output to the other", t)
        self.assertIn("risk-flagger-p7", t)           # folded in only when there is no ux/ui group
        self.assertIn("<R>/risk.md", t)

    def test_ux_and_ui_share_a_worker_but_never_run_in_parallel(self):
        t = task("reviewer (ux, ui)")
        self.assertIn("reviewer-ux-p7", t)
        self.assertIn("reviewer-ui-p7", t)
        self.assertIn("never in parallel", t)
        self.assertIn("review-ux.md", t)
        self.assertIn("review-ui.md", t)

    def test_skill_step_5_names_the_groups_and_the_risk_fold(self):
        step5 = SKILL[SKILL.index("**Step 5 (stage `review`)"):SKILL.index("**Step 6")]
        self.assertIn("`security+code`", step5)
        self.assertIn("`ux+ui`", step5)
        self.assertIn("`R/risk.md`", step5)
        self.assertIn("R/risk.md` exists", SKILL[SKILL.index("**Step 6"):])


if __name__ == "__main__":
    unittest.main()
