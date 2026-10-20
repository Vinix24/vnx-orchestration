# voortgang

A Claude Code mod that shows the progress of several VNX projects in one pane and one status line. Per project it shows the live work, the outcomes that wait on a decision, the open PRs with their review seats, the tracks on the `now` horizon with their deliverables, and how many deliverables wait on a promote.

It reads metadata only: ids, counts, statuses and PR titles. It never reads a transcript, a report, a receipt or the body of an open item, so it is safe to load in a session that holds client work.

The labels in the pane are Dutch (`ververs`, `onbeschikbaar`, `geen store`, `verouderd`).

## What it shows

### The status line

After every refresh the mod pins one line under the prompt:

```
voortgang · 1 live · 2 PR · 20 open · 1 onleesbaar · 1 verouderd
```

- `live`, `PR` and `open` are totals over the projects whose part could be read. When no project could be read the total is `?`, never `0`.
- `onleesbaar` counts the projects where at least one part could not be read, a missing store included.
- `verouderd` counts the projects whose `t0_index.json` or `t0_state.json` is older than 60 minutes.
- Both counts are left out while they are zero.

### The pane

`/voortgang` refreshes and opens the pane `Voortgang`. The pane never opens by itself. One block per project:

```
vnx-dev · index 3 min
  werk: 1 live · 0 start · 10 stale · 0 ongemeten
  live: 20261020-vg-1-voortgang-mod
  uitkomsten: 20 open (investigate 20)
  besluit nodig: 20261019-a, 20261019-b +18 meer
  PR's: 2 open · state 3 min
  #2063 · ci pass · blocked · <PR title>
    geslaagd: geen · open: codex_gate
  tracks: 3 now · 20 next · 109 later
  P1 ci-profile-a-xdist · queued → in_progress · 2 proposed · 6 done
  P2 voortgang-mod · queued → in_progress · geen leveringen
  te promoveren: 5 now · 105 totaal
```

- `werk` and `live` come from `live_work`; `uitkomsten` and `besluit nodig` from `open_outcomes`.
- A PR shows its seats twice: `geslaagd` are the gates that passed (`gates_passed`), `open` the gates still owed (`blocked_on`).
- A now-track shows `phase`, then `→ derived_status` when the two differ, then its deliverables counted by `derived_status`.
- `te promoveren` counts the `proposed` deliverables: on the now-tracks, and in the whole project.
- The `ververs` button (hotkey `r`) refreshes.

Every row is cut to the pane's width. The PR list and the track list are cut to the rows the pane has per project, with a `+N meer` row for the rest.

The pane draws with `Box`, `Text` and `Button` only, the elements every surface has, so it draws the same in the terminal, the desktop app, VS Code and the mobile app through Remote Control.

Where no surface places the pane, as in a headless `claude --print "/voortgang"` run, the command answers with one summary line per project instead:

```
vnx-dev: 1 live · 2 PR · 20 open · now ci-profile-a-xdist, provider-schakelaar, voortgang-mod · next 20 · later 109 · 105 te promoveren
```

### Absence is loud

A read that failed is never shown as zero or as an empty list.

| Situation | What the project shows |
|---|---|
| The store directory is missing | `geen store` |
| The project id is not one directory name (letters, digits, `.`, `_` and `-`, starting with a letter or digit) | `ongeldige project-id`, and nothing is read for it |
| A file is missing, unreadable or not JSON | that part: `onbeschikbaar: <reason>` |
| A block says `available` is anything but `true` | that part: `onbeschikbaar: <the block's own reason>`, else `available: <value>` |
| `vnx` exits non-zero, times out, cannot start or prints no JSON | tracks and deliverables: `onbeschikbaar: vnx exit 1` (or `vnx time-out`, `vnx startfout: ...`, `vnx gaf ongeldige JSON`); the file parts still show |
| `t0_index.json` or `t0_state.json` is older than 60 minutes, or has no timestamp | its age and `verouderd` next to the numbers it carries |

The age always shows (`index 3 min`). A stale index can say `0 live` while a worker runs, which is why it is marked.

## Install

From the toolkit marketplace, inside Claude Code:

```
/plugin marketplace add Vinix24/claude-code-toolkit
/plugin install voortgang@claude-code-toolkit
```

The same from a terminal: `claude plugin marketplace add Vinix24/claude-code-toolkit`, then `claude plugin install voortgang@claude-code-toolkit`.

Until the mod is exported to that marketplace, load it from a checkout of this repository:

```
claude --plugin-dir packages/claude-code-toolkit/voortgang
```

It needs the `vnx` binary (VNX 1.7.2 or newer) and the project stores under the data root.

## Options

Set them in `/config`, where each one is a row, or in settings under `pluginConfigs.voortgang.options`.

| Option | Default | What it does |
|---|---|---|
| `projects` | `vnx-dev,mission-control,seocrawler-v2,sales-copilot` | Comma-separated project ids. Each one is read from `<data_root>/<id>/state`. |
| `data_root` | `~/.vnx-data` | The directory that holds one store per project. A leading `~` is the `HOME` of the session. |
| `vnx_bin` | `~/.vnx-system/current/bin/vnx` | The `vnx` executable that lists tracks and deliverables. |
| `refresh_seconds` | `120` | How often the status line and the pane refresh. 15 seconds at least. |

When `HOME` is unknown and a path starts with `~`, the mod reads nothing and shows `onbeschikbaar: HOME onbekend, ~ niet op te lossen`.

## What it reads

Per project `p`, with the store `S = <data_root>/<p>/state`:

1. Whether `S` exists.
2. `S/t0_index.json`: `timestamp`, `live_work` (`available`, `reason`, `counts`, `live[].dispatch_id`) and `open_outcomes` (`available`, `reason`, `total`, `by_outcome`, `items[].dispatch_id`).
3. `S/t0_state.json`: `generated_at` and `pr_queue` (`available`, `reason`, and per open PR `number`, `title`, `ci_status`, `merge_state`, `gates_passed`, `blocked_on`).
4. `vnx objective list --json --project-id p --state-dir S`, run in `S` with a 10 second timeout: `track_id`, `priority`, `phase`, `derived_status`, `horizon`.
5. `vnx deliverable list --json --project-id p --state-dir S`, run the same way: `track`, `derived_status`.

`--state-dir` is required: without it, run from a directory outside a VNX repository, `objective list` fails with `sqlite3.OperationalError: unable to open database file`.

The projects are read in parallel, the reads and runs of one project one after the other. The PR title is the only free text the mod shows.

## What it never does

- It reads no other file: no other file under the data root, no transcript, report, receipt, open-item body, outcome reason, track title or deliverable title.
- It runs no other command: no `gh`, no `git`, no shell, never `build_t0_state.py` or anything else that writes state.
- It writes nothing: no file and no `$.store`. It keeps its model in the session's `$.state` and pins its status line, nothing more.
- It does not open the pane unasked.

## Limits

- The numbers are as fresh as the last refresh and as the index itself. The mod never rebuilds an index.
- The tests ran on Claude Code 2.1.304 with `claude plugin test`. The manual run against real stores was headless, so the pane was checked through the test kit on the terminal and the mobile table, not on a live screen.
- The mod names VNX by design. The toolkit export script (`scripts/maintenance/export_toolkit_plugin.sh`) handles context-rotation only, and its leak scan refuses the product name, so publishing this mod to the marketplace needs its own export path.

## Development

```
claude plugin validate packages/claude-code-toolkit/voortgang
claude plugin test packages/claude-code-toolkit/voortgang
```

- `hooks/register.tsx`: the hooks module (`session.start`, `command.run`, `ui.render`, the timer).
- `hooks/collect.ts`: the collector. Pure functions over parsed JSON, and one function that reads a project through a narrow `Io` that `$` provides in a session and an object in memory provides in a test.
- `hooks/view.ts`: the pane rows, cut to the pane's size.
- `types/index.d.ts`: the `PluginState` contract for the two `$.state` values.
- `tests/`: the engine-driven pane, command, timer and boundary tests, and the collector's tests.

Type-check it with the `tsconfig.json` from the header of the `claude-code.d.ts` the engine writes, kept outside this folder, with `include` naming that file and this folder's `hooks`, `types` and `tests`. The engine writes `.claude-plugin/types/` when it hot-reloads the folder; that folder carries its own `.gitignore` and is never committed.
