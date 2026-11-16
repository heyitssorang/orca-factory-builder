from .errors import NotFound


class FakeDirectory:
    """In-memory stand-in for the directory service (accounts, group membership, attributes)."""

    def __init__(self, users=None):
        # users: {name: {"groups": [...], "enabled": bool, "ou": str, "attrs": {...}}}
        self._users = users or {}

    def get(self, name):
        if name not in self._users:
            raise NotFound(f"no such user: {name}")
        return self._users[name]

    def groups(self, name):
        return sorted(self.get(name)["groups"])

    def exists(self, name):
        return name in self._users
