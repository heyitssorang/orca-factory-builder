Add `src/opsconsole/lifecycle.py` with `disable_user(store, directory, audit, actor, name,
dry_run=False)` and `recover_user(store, directory, audit, actor, name, dry_run=False)`.
`disable_user` needs the tool `disable-user`, sets the account's `enabled` to False, moves its `ou`
to `"Disabled"` and remembers the old `ou` in `attrs["prev_ou"]`. It refuses accounts in the group
`protected` by raising a new `ProtectedAccount(OpsError)` (add it to `errors.py`) and refuses an
already disabled account with `OpsError`. `recover_user` needs `recover-user`, sets `enabled` back
to True, restores `ou` from `attrs["prev_ou"]` and removes that attribute; an account that is not
disabled raises `OpsError`. With `dry_run=True` neither changes anything nor writes an audit row;
both return `{"dry_run": True, "changes": {<field>: [old, new], ...}}` for `enabled` and `ou`.
A real run returns `{"user": name, "enabled": <new value>, "ou": <new value>}` and writes one audit
row with action `disable-user` or `recover-user`. Wire both into the CLI as
`disable-user <actor> <user>` and `recover-user <actor> <user>`. Add unit tests.
