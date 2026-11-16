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


def log_with(rows):
    d = Path(tempfile.mkdtemp())
    p = d / "a.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return AuditLog(p)


ROWS = [{"ts": f"2026-01-0{i}T00:00:00+00:00", "actor": "a" if i % 2 else "b", "action": "x",
         "target": str(i), "detail": {}} for i in range(1, 6)]


class T(unittest.TestCase):
    def test_bounds_inclusive(self):
        log = log_with(ROWS)
        got = log.query(since="2026-01-02T00:00:00+00:00", until="2026-01-04T00:00:00+00:00")
        self.assertEqual([r["target"] for r in got], ["2", "3", "4"])

    def test_open_bounds(self):
        log = log_with(ROWS)
        self.assertEqual(len(log.query(since="2026-01-04T00:00:00+00:00")), 2)
        self.assertEqual(len(log.query(until="2026-01-01T00:00:00+00:00")), 1)
        self.assertEqual(len(log.query()), 5)

    def test_combines_with_actor(self):
        log = log_with(ROWS)
        got = log.query(actor="a", since="2026-01-02T00:00:00+00:00")
        self.assertEqual([r["target"] for r in got], ["3", "5"])


if __name__ == "__main__":
    unittest.main()
