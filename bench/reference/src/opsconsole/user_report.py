from .auth import require


def user_report(store, directory, audit, actor, name):
    """Read-only report of one account. Needs the `user-report` tool."""
    require(store, actor, "user-report")
    u = directory.get(name)
    audit.record(actor, "user-report", name)
    return {"user": name, "enabled": u["enabled"], "groups": directory.groups(name)}
