import json
import sys

from .audit import AuditLog
from .compare_user import compare_user
from .directory import FakeDirectory
from .errors import OpsError
from .laps_lookup import laps_lookup
from .lifecycle import disable_user, recover_user
from .roles import RoleStore
from .user_report import user_report


def main(argv, store=None, directory=None, audit=None, out=sys.stdout):
    store = store or RoleStore("roles.json")
    directory = directory or FakeDirectory()
    audit = audit or AuditLog("audit.jsonl")
    fmt = "json"
    if "--format" in argv:
        i = argv.index("--format")
        fmt = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    try:
        tool, actor = argv[0], argv[1]
        if tool == "user-report":
            result = user_report(store, directory, audit, actor, argv[2])
        elif tool == "laps-lookup":
            result = laps_lookup(store, directory, audit, actor, argv[2])
        elif tool == "disable-user":
            result = disable_user(store, directory, audit, actor, argv[2])
        elif tool == "recover-user":
            result = recover_user(store, directory, audit, actor, argv[2])
        elif tool == "compare-user":
            result = compare_user(store, directory, audit, actor, argv[2], argv[3])
            if fmt == "table":
                f = lambda k: ", ".join(result[k]) or "-"  # noqa: E731
                print(f"only {result['a']}: {f('only_a')}\nonly {result['b']}: {f('only_b')}\nboth: {f('both')}", file=out)
                return 0
        else:
            raise OpsError(f"unknown tool: {tool}")
    except (OpsError, IndexError) as e:
        print(json.dumps({"error": str(e)}), file=out)
        return 1
    print(json.dumps(result), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
