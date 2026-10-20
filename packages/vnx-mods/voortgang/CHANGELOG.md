# Changelog

All notable changes to this plugin are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the plugin follows [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-20

### Added

- The block "Bouwers" in the pane: every dispatch in `live_work.live` and `live_work.starting`, with the `role` and `model` of its `dispatch-spec.json`, the first `# ` line of its `instruction.md` and its `age_seconds` in whole minutes. A field that cannot be read is `onbeschikbaar: <reason>` for that field of that builder only. `stale` and `unmeasured` entries are left out.
- `N bouwers` in the status line.
- A refresh that rejects is shown as `voortgang · verversen mislukt: <reason>` in the status line and as the first pane row.

### Fixed

- A malformed entry in the list of `vnx objective list` or `vnx deliverable list` was dropped silently, so `[null]` read as success with nothing proposed. That part is now `onbeschikbaar: vnx gaf geen object op plaats <n>`.
- Each list had its own row budget, so a project with PRs of two rows and many tracks drew more rows than the pane has. The builders, the PRs and the tracks now share the rows, and a cut list keeps a row for `+N meer`.
- "It writes nothing" did not hold: for a store with `t0_index.json` or `t0_state.json` but without `runtime_coordination.db`, `vnx objective list` and `vnx deliverable list` create that database. `vnx` is no longer run for such a store; tracks and deliverables show `onbeschikbaar: runtime_coordination.db ontbreekt`.
- A data root that could not be listed showed `Voortgang · 0 projecten`; the header now says `aantal onbekend`.
- The empty state pointed to an option `projects` that does not exist; it names `exclude` and `data_root`.
- Rows were cut on code points; they are now cut on display cells, so wide and combining characters stay within the pane.
- The refresh at session start, on the timer and on the button had no `.catch`; every refresh path handles a rejection and shows it.

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
