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
        grant(s, "disable-user", "bob"); grant(s, "recover-user", "bob")
        from opsconsole import lifecycle
        return s, d, a, lifecycle

    def test_disable_and_recover(self):
        s, d, a, lc = self.mk()
        r = lc.disable_user(s, d, a, "bob", "alice")
        self.assertEqual(r, {"user": "alice", "enabled": False, "ou": "Disabled"})
        u = d.get("alice")
        self.assertEqual((u["enabled"], u["ou"], u["attrs"]["prev_ou"]), (False, "Disabled", "Users"))
        r = lc.recover_user(s, d, a, "bob", "alice")
        self.assertEqual(r, {"user": "alice", "enabled": True, "ou": "Users"})
        self.assertNotIn("prev_ou", d.get("alice")["attrs"])
        self.assertEqual(len(a.query(action="disable-user")), 1)
        self.assertEqual(len(a.query(action="recover-user")), 1)

    def test_dry_run_changes_nothing(self):
        s, d, a, lc = self.mk()
        r = lc.disable_user(s, d, a, "bob", "alice", dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["changes"]["enabled"], [True, False])
        self.assertEqual(r["changes"]["ou"], ["Users", "Disabled"])
        self.assertTrue(d.get("alice")["enabled"])
        self.assertEqual(a.rows(), [])

    def test_protected_and_double(self):
        s, d, a, lc = self.mk()
        from opsconsole.errors import ProtectedAccount
        with self.assertRaises(ProtectedAccount):
            lc.disable_user(s, d, a, "bob", "root")
        self.assertTrue(issubclass(ProtectedAccount, OpsError))
        lc.disable_user(s, d, a, "bob", "alice")
        with self.assertRaises(OpsError):
            lc.disable_user(s, d, a, "bob", "alice")
        with self.assertRaises(OpsError):
            lc.recover_user(s, d, a, "bob", "bob")

    def test_needs_roles(self):
        s, d, a = env()
        from opsconsole import lifecycle as lc
        with self.assertRaises(NotAuthorized):
            lc.disable_user(s, d, a, "bob", "alice")
        with self.assertRaises(NotAuthorized):
            lc.recover_user(s, d, a, "bob", "alice")

    def test_cli(self):
        s, d, a, lc = self.mk()
        out = io.StringIO()
        self.assertEqual(cli.main(["disable-user", "bob", "alice"], store=s, directory=d, audit=a, out=out), 0)
        self.assertEqual(cli.main(["recover-user", "bob", "alice"], store=s, directory=d, audit=a, out=out), 0)
        self.assertTrue(d.get("alice")["enabled"])


if __name__ == "__main__":
    unittest.main()
