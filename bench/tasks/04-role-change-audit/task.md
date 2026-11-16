Role changes must be audited with before and after. `RoleStore(path, audit=None)` takes an
optional `AuditLog`. `create_role`, `add_member` and `remove_member` (add `remove_member(role,
user)` if it does not exist: removes a member, no error if absent, `NotFound` for an unknown role)
accept a keyword `actor="system"`. When an audit log is set, each call that changes the store
records one row: action `role-change`, target the role name, actor the `actor` value, detail
`{"before": <members before>, "after": <members after>}` (member lists; a new role has `[]` before).
A call that changes nothing records no row. Without an audit log behaviour is unchanged. Add unit tests.
