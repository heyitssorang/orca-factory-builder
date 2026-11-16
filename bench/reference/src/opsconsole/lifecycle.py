from .auth import require
from .errors import OpsError, ProtectedAccount


def disable_user(store, directory, audit, actor, name, dry_run=False):
    require(store, actor, "disable-user")
    u = directory.get(name)
    if "protected" in u["groups"]:
        raise ProtectedAccount(f"{name} is protected")
    if not u["enabled"]:
        raise OpsError(f"{name} is already disabled")
    if dry_run:
        return {"dry_run": True, "changes": {"enabled": [True, False], "ou": [u["ou"], "Disabled"]}}
    u["attrs"]["prev_ou"] = u["ou"]
    u["enabled"], u["ou"] = False, "Disabled"
    audit.record(actor, "disable-user", name)
    return {"user": name, "enabled": False, "ou": "Disabled"}


def recover_user(store, directory, audit, actor, name, dry_run=False):
    require(store, actor, "recover-user")
    u = directory.get(name)
    if u["enabled"]:
        raise OpsError(f"{name} is not disabled")
    prev = u["attrs"].get("prev_ou", "Users")
    if dry_run:
        return {"dry_run": True, "changes": {"enabled": [False, True], "ou": [u["ou"], prev]}}
    u["enabled"], u["ou"] = True, prev
    u["attrs"].pop("prev_ou", None)
    audit.record(actor, "recover-user", name)
    return {"user": name, "enabled": True, "ou": prev}
