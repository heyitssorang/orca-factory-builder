import json
from pathlib import Path

from .errors import NotFound


class RoleStore:
    def __init__(self, path, audit=None):
        self.path = Path(path)
        self.audit = audit
        self._roles = {}
        if self.path.exists():
            self._roles = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self):
        self.path.write_text(json.dumps(self._roles, indent=2, sort_keys=True), encoding="utf-8")

    def _record(self, actor, role, before, after):
        if self.audit is not None:
            self.audit.record(actor, "role-change", role, before=before, after=after)

    def create_role(self, name, tools=(), actor="system"):
        self._roles[name] = {"tools": sorted(tools), "members": []}
        self._save()
        self._record(actor, name, [], [])

    def add_member(self, role, user, actor="system"):
        r = self._get(role)
        if user not in r["members"]:
            before = list(r["members"])
            r["members"].append(user)
            r["members"].sort()
            self._save()
            self._record(actor, role, before, list(r["members"]))

    def remove_member(self, role, user, actor="system"):
        r = self._get(role)
        if user in r["members"]:
            before = list(r["members"])
            r["members"].remove(user)
            self._save()
            self._record(actor, role, before, list(r["members"]))

    def members(self, role):
        return list(self._get(role)["members"])

    def tools_for(self, user):
        out = set()
        for r in self._roles.values():
            if user in r["members"]:
                out.update(r["tools"])
        return out

    def roles(self):
        return sorted(self._roles)

    def _get(self, role):
        if role not in self._roles:
            raise NotFound(f"no such role: {role}")
        return self._roles[role]
