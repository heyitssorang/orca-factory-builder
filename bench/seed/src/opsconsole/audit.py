import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    """Append-only JSON-lines audit log."""

    def __init__(self, path):
        self.path = Path(path)

    def record(self, actor, action, target="", **detail):
        row = {"ts": datetime.now(timezone.utc).isoformat(), "actor": actor,
               "action": action, "target": target, "detail": detail}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def rows(self):
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def query(self, actor=None, action=None):
        return [r for r in self.rows()
                if (actor is None or r["actor"] == actor)
                and (action is None or r["action"] == action)]
