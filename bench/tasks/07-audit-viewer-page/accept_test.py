import io, json, os, sys, tempfile, unittest
from pathlib import Path

REPO = Path(os.environ["BENCH_REPO"])
sys.path.insert(0, str(REPO / "src"))
from opsconsole import cli  # noqa: E402
from opsconsole.audit import AuditLog  # noqa: E402
from opsconsole.directory import FakeDirectory  # noqa: E402
from opsconsole.errors import NotAuthorized, NotFound, OpsError  # noqa: E402
from opsconsole.roles import RoleStore  # noqa: E402


def env():
    d = Path(tempfile.mkdtemp())
    users = {
        "alice": {"groups": ["staff", "vpn"], "enabled": True, "ou": "Users", "attrs": {}},
        "bob": {"groups": ["staff", "ops"], "enabled": True, "ou": "Users", "attrs": {}},
        "root": {"groups": ["protected"], "enabled": True, "ou": "Admins", "attrs": {}},
        "PC1": {"groups": [], "enabled": True, "ou": "Computers", "attrs": {"laps_password": "s3cret"}},
        "PC2": {"groups": [], "enabled": True, "ou": "Computers", "attrs": {}},
    }
    return RoleStore(d / "roles.json"), FakeDirectory(users), AuditLog(d / "audit.jsonl")


def grant(store, tool, user):
    store.create_role(tool + "-role", tools=[tool])
    store.add_member(tool + "-role", user)


import re


class T(unittest.TestCase):
    def setUp(self):
        self.html = (REPO / "web" / "audit.html").read_text()
        self.low = self.html.lower()

    def test_structure(self):
        self.assertRegex(self.low, r"<html[^>]*\blang=")
        self.assertIn('id="filter"', self.html)
        self.assertRegex(self.low, r"<table")
        for col in ("ts|time", "actor", "action", "target"):  # a human label for `ts` is fine
            self.assertRegex(self.low, rf"<th[^>]*>[^<]*({col})", col)
        self.assertIn("audit.json", self.html)
        self.assertIn("No matching rows", self.html)

    def test_filter_is_labelled(self):
        m = re.search(r'<input[^>]*id="filter"[^>]*>', self.html)
        self.assertTrue(m)
        has_label = re.search(r'<label[^>]*for="filter"', self.html) or "aria-label" in m.group(0)
        self.assertTrue(has_label, "filter input has no label")

    def test_linked_and_responsive(self):
        self.assertIn("audit.html", (REPO / "web" / "index.html").read_text())
        self.assertRegex(self.low, r'name="viewport"')


if __name__ == "__main__":
    unittest.main()
