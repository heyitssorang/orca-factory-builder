Add a LAPS lookup tool as `src/opsconsole/laps_lookup.py` with
`laps_lookup(store, directory, audit, actor, name)`. It needs the tool `laps-lookup` (own role,
not part of `user-report`). It returns `{"computer": name, "password": <directory attrs "laps_password">}`.
A computer without that attribute raises `NotFound`. Every successful reveal writes one audit row
with action `laps-reveal` and target `name`; a refused attempt raises `NotAuthorized` and writes
one row with action `laps-denied`. Wire it into the CLI in `src/opsconsole/cli.py` as
`laps-lookup <actor> <computer>`. Add unit tests.
