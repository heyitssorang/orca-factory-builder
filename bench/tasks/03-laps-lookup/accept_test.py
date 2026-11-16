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
    def test_reveal_and_audit(self):
        s, d, a = env()
        grant(s, "laps-lookup", "bob")
        from opsconsole.laps_lookup import laps_lookup
        r = laps_lookup(s, d, a, "bob", "PC1")
        self.assertEqual(r, {"computer": "PC1", "password": "s3cret"})
        rows = a.query(action="laps-reveal")
        self.assertEqual((len(rows), rows[0]["target"], rows[0]["actor"]), (1, "PC1", "bob"))

    def test_denied_audited(self):
        s, d, a = env()
        from opsconsole.laps_lookup import laps_lookup
        with self.assertRaises(NotAuthorized):
            laps_lookup(s, d, a, "bob", "PC1")
        self.assertEqual(len(a.query(action="laps-denied")), 1)
        self.assertEqual(len(a.query(action="laps-reveal")), 0)

    def test_user_report_role_does_not_grant(self):
        s, d, a = env()
        grant(s, "user-report", "bob")
        from opsconsole.laps_lookup import laps_lookup
        with self.assertRaises(NotAuthorized):
            laps_lookup(s, d, a, "bob", "PC1")

    def test_missing_attribute(self):
        s, d, a = env()
        grant(s, "laps-lookup", "bob")
        from opsconsole.laps_lookup import laps_lookup
        with self.assertRaises(NotFound):
            laps_lookup(s, d, a, "bob", "PC2")
        self.assertEqual(len(a.query(action="laps-reveal")), 0)

    def test_cli(self):
        s, d, a = env()
        grant(s, "laps-lookup", "bob")
        out = io.StringIO()
        self.assertEqual(cli.main(["laps-lookup", "bob", "PC1"], store=s, directory=d, audit=a, out=out), 0)
        self.assertEqual(json.loads(out.getvalue())["password"], "s3cret")


if __name__ == "__main__":
    unittest.main()
