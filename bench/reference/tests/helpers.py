import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opsconsole.audit import AuditLog  # noqa: E402
from opsconsole.directory import FakeDirectory  # noqa: E402
from opsconsole.roles import RoleStore  # noqa: E402


def make_env():
    d = Path(tempfile.mkdtemp())
    store, audit = RoleStore(d / "roles.json"), AuditLog(d / "audit.jsonl")
    directory = FakeDirectory({
        "alice": {"groups": ["staff", "vpn"], "enabled": True, "ou": "Users", "attrs": {}},
        "bob": {"groups": ["staff"], "enabled": True, "ou": "Users", "attrs": {}},
    })
    return d, store, directory, audit
