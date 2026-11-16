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
        d = Path(tempfile.mkdtemp())
        a = AuditLog(d / "a.jsonl")
        return RoleStore(d / "r.json", audit=a), a

    def test_rows_with_before_after(self):
        s, a = self.mk()
        s.create_role("r", tools=["t"], actor="admin")
        s.add_member("r", "u1", actor="admin")
        s.add_member("r", "u2", actor="admin")
        s.remove_member("r", "u1", actor="carol")
        rows = a.query(action="role-change")
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["detail"], {"before": [], "after": []})
        self.assertEqual(rows[2]["detail"], {"before": ["u1"], "after": ["u1", "u2"]})
        self.assertEqual((rows[3]["actor"], rows[3]["target"]), ("carol", "r"))
        self.assertEqual(rows[3]["detail"], {"before": ["u1", "u2"], "after": ["u2"]})

    def test_noop_records_nothing(self):
        s, a = self.mk()
        s.create_role("r"); s.add_member("r", "u")
        n = len(a.rows())
        s.add_member("r", "u"); s.remove_member("r", "ghost")
        self.assertEqual(len(a.rows()), n)

    def test_default_actor_and_no_audit(self):
        s, a = self.mk()
        s.create_role("r")
        self.assertEqual(a.rows()[0]["actor"], "system")
        plain = RoleStore(Path(tempfile.mkdtemp()) / "x.json")
        plain.create_role("r"); plain.add_member("r", "u")
        self.assertEqual(plain.members("r"), ["u"])

    def test_unknown_role(self):
        s, _ = self.mk()
        with self.assertRaises(NotFound):
            s.remove_member("nope", "u")


if __name__ == "__main__":
    unittest.main()
