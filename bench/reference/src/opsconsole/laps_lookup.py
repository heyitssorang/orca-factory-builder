from .errors import NotAuthorized, NotFound
from .auth import is_authorized


def laps_lookup(store, directory, audit, actor, name):
    if not is_authorized(store, actor, "laps-lookup"):
        audit.record(actor, "laps-denied", name)
        raise NotAuthorized(f"{actor} may not use laps-lookup")
    pw = directory.get(name)["attrs"].get("laps_password")
    if pw is None:
        raise NotFound(f"no LAPS password for {name}")
    audit.record(actor, "laps-reveal", name)
    return {"computer": name, "password": pw}
