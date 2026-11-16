# ops-console

Small ops console (benchmark seed repo, synthetic). Python 3 stdlib only.

- test: python3 -m unittest discover -s tests
- build: python3 -m compileall -q src
- dev: python3 -m http.server {port} --directory web
- ui_globs: web/*

Conventions: one module per concern under `src/opsconsole/`; every public function has a
unit test in `tests/`; no new dependencies; errors are raised as `OpsError` subclasses.
