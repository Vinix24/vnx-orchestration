# Changelog

All notable changes to this plugin are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the plugin follows [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-20

voortgang is a VNX mod. It lives in `packages/vnx-mods/voortgang/` and ships with a VNX release; it is not exported to the public claude-code-toolkit.

### Added

- A pane, `Voortgang`, opened by `/voortgang`: per project the age of the index, the live work with the running dispatch ids, the open outcomes, the open PRs with the seats that passed and the seats still owed, the now-tracks with their deliverables counted by status, and the deliverables that wait on a promote. A `ververs` button (hotkey `r`) refreshes it. It never opens unasked.
- A status line after every refresh: live work, open PRs and open outcomes summed over the readable projects, with the unreadable and the outdated projects counted.
- A refresh every `refresh_seconds` (default 120, at least 15) and once at session start.
- Store discovery: every direct child of the data root that holds `state/t0_index.json` or `state/t0_state.json` is a project, ordered by id. No project list and no project name is configured or built in. A data root that cannot be listed is `onbeschikbaar: <reason>` for the whole model.
- A local marketplace, `packages/vnx-mods/.claude-plugin/marketplace.json` (`vnx-mods`), installed from `~/.vnx-system/current/packages/vnx-mods`.
- Options `exclude`, `data_root`, `vnx_bin` and `refresh_seconds`.
- Loud absence: `geen store` for a missing store, `onbeschikbaar: <reason>` for a part that could not be read, `verouderd` with the age for an index or state older than 60 minutes. A failed read is never shown as zero or as an empty list.
- Where no surface places the pane, `/voortgang` answers with one summary line per project.
- A content boundary: one listing of the data root, per project two files (`t0_index.json`, `t0_state.json`) and two `vnx ... list --json --state-dir` calls, nothing written, and a project id that would leave the data root refused.

### Known limits

- Tested with `claude plugin test` on Claude Code 2.1.304. The manual run against real stores was headless, so the pane was checked through the test kit, not on a live screen.
- The engine's test runner cannot read files, so the check that the package names no operator project is a script, `tests/no-operator-names.sh`.
