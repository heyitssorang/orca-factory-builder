Add `RoleStore.remove_member(role, user)` in `src/opsconsole/roles.py`. It removes `user` from the
role's members and saves. Removing a user who is not a member does nothing and raises no error;
an unknown role raises `NotFound`. Cover it with unit tests.
