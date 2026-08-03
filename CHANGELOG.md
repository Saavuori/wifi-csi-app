# Changelog

Entries below this line are written by the release workflow from the pull request that caused
them. The version is decided by that PR's release type — one `release:` label, or a
conventional-commit title. See `.github/workflows/pr-release-type.yml`.

Versions are `MAJOR.MINOR.PATCH`:

| Release type | When | Example |
|---|---|---|
| `major` | a breaking change — the uplink wire format, an API removal, anything that makes an existing node or recording stop working | 1.4.2 → 2.0.0 |
| `minor` | a new capability that does not break what came before | 1.4.2 → 1.5.0 |
| `patch` | a fix, a performance change, an internal tidy-up | 1.4.2 → 1.4.3 |
| `skip` | docs, CI, tests — nothing a user of a release would notice | unchanged |

The git tag is authoritative. `server/pyproject.toml` and `web/package.json` are updated to
match, and the running server reports the same number on `/api/version` and in the header.
