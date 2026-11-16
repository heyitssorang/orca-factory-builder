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


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


class Counting(FakeDirectory):
    calls = 0
    def groups(self, name):
        Counting.calls += 1
        return super().groups(name)


class T(unittest.TestCase):
    def mk(self, ttl=60):
        from opsconsole.groupcache import GroupCache
        Counting.calls = 0
        _, d, _ = env()
        c = Clock()
        return GroupCache(Counting(d._users), ttl=ttl, clock=c), c

    def test_cached_until_ttl(self):
        g, c = self.mk()
        self.assertEqual(g.groups("alice"), ["staff", "vpn"])
        c.t = 59.0; g.groups("alice")
        self.assertEqual(Counting.calls, 1)
        c.t = 60.5; g.groups("alice")
        self.assertEqual(Counting.calls, 2)

    def test_invalidate(self):
        g, c = self.mk()
        g.groups("alice"); g.groups("bob")
        g.invalidate("alice"); g.groups("alice"); g.groups("bob")
        self.assertEqual(Counting.calls, 3)
        g.invalidate(); g.groups("alice"); g.groups("bob")
        self.assertEqual(Counting.calls, 5)

    def test_errors_not_cached(self):
        g, c = self.mk()
        for _ in range(2):
            with self.assertRaises(NotFound):
                g.groups("nobody")
        self.assertEqual(Counting.calls, 2)


if __name__ == "__main__":
    unittest.main()
