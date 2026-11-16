# Bundled dependencies

The factory's security reviewer and its finding verifiers need these. `scripts/install.py`
installs each one into `~/.claude` only if it is not already there, and `uninstall` removes only
what it installed.

| path | what | origin |
|---|---|---|
| `agents/review-verifier-p2.md`, `agents/review-verifier-p2-security.md` | finding verifier agents | written for this project |
| `skills/differential-review/` | security-focused diff review skill, unmodified | [trailofbits/skills](https://github.com/trailofbits/skills), `plugins/differential-review/skills/differential-review` @ `1f68e03`, licensed CC BY-SA 4.0 (`skills/differential-review/LICENSE.upstream`) |

The CC BY-SA 4.0 licence covers `skills/differential-review/` only. Keep its `LICENSE.upstream`
and this attribution if you redistribute it.
