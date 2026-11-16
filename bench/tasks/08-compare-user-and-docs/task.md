Add `src/opsconsole/compare_user.py` with `compare_user(store, directory, audit, actor, a, b)`. It
needs the tool `compare-user` and returns `{"a": a, "b": b, "only_a": [...], "only_b": [...],
"both": [...]}` with sorted group names, and writes one audit row with action `compare-user` and
target `"<a>,<b>"`. Wire it into the CLI as `compare-user <actor> <a> <b> [--format json|table]`
(default `json`). The `table` format prints one line each: `only <a>: g1, g2`, `only <b>: ...`,
`both: ...` (use `-` when a list is empty). Add a `README.md` at the repo root that documents every
CLI tool (`user-report`, `compare-user`) and the roles/tools they need. Add unit tests.
