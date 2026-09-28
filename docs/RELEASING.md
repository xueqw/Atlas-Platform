# Release Process

Atlas Agent Platform uses Semantic Versioning: `MAJOR.MINOR.PATCH`.

- Increment `MAJOR` for incompatible API or data-model changes.
- Increment `MINOR` for backward-compatible capabilities.
- Increment `PATCH` for backward-compatible fixes.

## Release checklist

1. Move completed entries from `Unreleased` in `CHANGELOG.md` into a dated version section.
2. Update `VERSION`, then synchronize the root and web package versions.
3. Run `python scripts/check_version.py` from the repository root.
4. Run the API regression suite and the production web build documented in `README.md`.
5. Merge only after the CI checks pass.
6. Create an annotated Git tag named `v<version>` and publish release notes from the matching changelog section.
7. Add a new empty `Unreleased` section for the next development cycle.

The `VERSION` file is the canonical platform version. CI rejects a change when `package.json`, `web/package.json`, `web/package-lock.json`, or the README version marker does not match it.
