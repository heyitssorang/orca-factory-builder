Extend `AuditLog.query` in `src/opsconsole/audit.py` with optional `since` and `until` keyword
arguments (ISO-8601 strings). A row matches when its `ts` is on or after `since` and on or before
`until`; omitted bounds are open. Existing `actor` and `action` filters keep working and combine
with the time filter. Add unit tests.
