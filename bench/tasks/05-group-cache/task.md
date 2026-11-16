Add `src/opsconsole/groupcache.py` with `GroupCache(directory, ttl=60, clock=time.monotonic)`.
`groups(name)` returns the directory's sorted groups for `name` and serves repeats from memory
until `ttl` seconds have passed on `clock`, then reads the directory again. `invalidate(name=None)`
drops one entry, or all when `name` is None. A lookup that raises (for example `NotFound`) is not
cached. `clock` is injectable so tests need no sleeping. Add unit tests.
