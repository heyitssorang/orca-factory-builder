import json
import sys

from .audit import AuditLog
from .directory import FakeDirectory
from .errors import OpsError
from .roles import RoleStore
from .user_report import user_report


def main(argv, store=None, directory=None, audit=None, out=sys.stdout):
    """`cli <tool> <actor> <target>`; prints JSON, returns an exit code."""
    store = store or RoleStore("roles.json")
    directory = directory or FakeDirectory()
    audit = audit or AuditLog("audit.jsonl")
    try:
        tool, actor, target = argv[0], argv[1], argv[2]
        if tool == "user-report":
            result = user_report(store, directory, audit, actor, target)
        else:
            raise OpsError(f"unknown tool: {tool}")
    except (OpsError, IndexError) as e:
        print(json.dumps({"error": str(e)}), file=out)
        return 1
    print(json.dumps(result), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
