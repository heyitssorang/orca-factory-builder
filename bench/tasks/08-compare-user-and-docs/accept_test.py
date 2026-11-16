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


class T(unittest.TestCase):
    def mk(self):
        s, d, a = env()
        grant(s, "compare-user", "bob")
        return s, d, a

    def test_compare(self):
        s, d, a = self.mk()
        from opsconsole.compare_user import compare_user
        r = compare_user(s, d, a, "bob", "alice", "bob")
        self.assertEqual(r, {"a": "alice", "b": "bob", "only_a": ["vpn"], "only_b": ["ops"], "both": ["staff"]})
        rows = a.query(action="compare-user")
        self.assertEqual((len(rows), rows[0]["target"]), (1, "alice,bob"))

    def test_needs_role(self):
        s, d, a = env()
        from opsconsole.compare_user import compare_user
        with self.assertRaises(NotAuthorized):
            compare_user(s, d, a, "bob", "alice", "bob")

    def test_cli_formats(self):
        s, d, a = self.mk()
        out = io.StringIO()
        self.assertEqual(cli.main(["compare-user", "bob", "alice", "bob"], store=s, directory=d, audit=a, out=out), 0)
        self.assertEqual(json.loads(out.getvalue())["both"], ["staff"])
        out = io.StringIO()
        self.assertEqual(cli.main(["compare-user", "bob", "alice", "bob", "--format", "table"],
                                  store=s, directory=d, audit=a, out=out), 0)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(lines, ["only alice: vpn", "only bob: ops", "both: staff"])

    def test_table_dash_for_empty(self):
        s, d, a = self.mk()
        out = io.StringIO()
        cli.main(["compare-user", "bob", "alice", "alice", "--format", "table"], store=s, directory=d, audit=a, out=out)
        self.assertEqual(out.getvalue().strip().splitlines(), ["only alice: -", "only alice: -", "both: staff, vpn"])

    def test_readme_documents_tools(self):
        text = (REPO / "README.md").read_text()
        for needle in ("user-report", "compare-user"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
