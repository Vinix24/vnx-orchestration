# changelog-fragmenten: Changelog entries without a shared insertion point

Contributors add each changelog entry as a separate fragment. Release preparation folds the fragments into a versioned section in first-parent addition order and removes the consumed files; CI checks fragment validity and direct changelog edits.

## How

Follow the [fragment and release instructions](../../changelog.d/README.md). From the repository root, preview a release with `python3 scripts/release/assemble_changelog.py --version X.Y.Z --check`, substituting the release version; see `python3 scripts/release/assemble_changelog.py --help` for flags.

## Modules

- [Related release cutover](../modules/central-install-cutover.md)

## Pull requests

Track closed: 2026-10-17 (UTC).

[#2085](https://github.com/Vinix24/vnx-orchestration/pull/2085).
