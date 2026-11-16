from .auth import require


def compare_user(store, directory, audit, actor, a, b):
    require(store, actor, "compare-user")
    ga, gb = set(directory.groups(a)), set(directory.groups(b))
    audit.record(actor, "compare-user", f"{a},{b}")
    return {"a": a, "b": b, "only_a": sorted(ga - gb), "only_b": sorted(gb - ga),
            "both": sorted(ga & gb)}
