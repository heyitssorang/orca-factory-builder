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
    def test_remove(self):
        s, _, _ = env()
        s.create_role("r"); s.add_member("r", "a"); s.add_member("r", "b")
        s.remove_member("r", "a")
        self.assertEqual(s.members("r"), ["b"])
        s.remove_member("r", "zzz")
        self.assertEqual(s.members("r"), ["b"])

    def test_unknown_role(self):
        s, _, _ = env()
        with self.assertRaises(NotFound):
            s.remove_member("nope", "a")

    def test_persisted(self):
        s, _, _ = env()
        s.create_role("r"); s.add_member("r", "a"); s.remove_member("r", "a")
        self.assertEqual(RoleStore(s.path).members("r"), [])


if __name__ == "__main__":
    unittest.main()
