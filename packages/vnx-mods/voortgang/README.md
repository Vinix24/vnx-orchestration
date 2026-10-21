# voortgang

A VNX mod for Claude Code. It shows the progress of every VNX project on the machine in one pane and one status line, and it needs VNX: the project stores under `~/.vnx-data` and the `vnx` CLI. It ships with a VNX release and lives in this repository under `packages/vnx-mods/`. Per project it shows the live work, the builders that run it, the outcomes that wait on a decision, the open PRs with their review seats, the tracks on the `now` horizon with their deliverables, and how many deliverables wait on a promote.

It reads metadata only: ids, counts, statuses, PR titles, and per builder one title line. It never reads a transcript, a report, a receipt or the body of an open item, so it is safe to load in a session that holds client work.

The labels in the pane are Dutch (`ververs`, `onbeschikbaar`, `geen store`, `verouderd`).

## What it shows

### The status line

After every refresh the mod pins one line under the prompt:

```
voortgang · 1 live · 2 PR · 20 open · 1 bouwers · 1 onleesbaar · 1 verouderd
```

- `live`, `PR`, `open` and `bouwers` are totals over the projects whose part could be read. `bouwers` counts the dispatches in `live_work.live` and `live_work.starting`; `stale` and `unmeasured` entries are not builders and are not counted. When no project could be read the total is `?`, never `0`.
- `onleesbaar` counts the projects where at least one part could not be read, a missing store included.
- `verouderd` counts the projects whose `t0_index.json` or `t0_state.json` is older than 60 minutes.
- Both counts are left out while they are zero.

### The pane

`/voortgang` refreshes and opens the pane `Voortgang`. The pane never opens by itself. One block per project, in id order:

```
project-a · index 3 min
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
  bouwers: 1
  bouwer: backend-developer · sonnet · 12 min bezig
    taak: <first # line of the instruction>
  te promoveren: 5 now · 105 totaal
```

- `werk` and `live` come from `live_work`; `uitkomsten` and `besluit nodig` from `open_outcomes`.
- A PR shows its seats twice: `geslaagd` are the gates that passed (`gates_passed`), `open` the gates still owed (`blocked_on`).
- A now-track shows `phase`, then `→ derived_status` when the two differ, then its deliverables counted by `derived_status`.
- `bouwers` is the block "Bouwers": every dispatch in `live_work.live` and `live_work.starting`, in index order. Per builder exactly three things: `role` and `model` of its spec (`bouwer`), the first line of its instruction that starts with `# ` (`taak`), and its `age_seconds` in whole minutes (`min bezig`). The index lists at most eight builders, in `live_work.live`, with the live and the starting entries together and no `starting` list; the mod reads that list (and a `starting` list when one is there) and takes the number of builders from `live_work.counts.live + live_work.counts.starting`. When the list holds fewer entries than the counts, the block shows the ones it has plus one line `+N niet getoond (index toont er max. 8)`. The fields it reads are `live_work.counts.live`, `.starting`, `.stale`, `.unmeasured` and, per entry of `live_work.live`, `dispatch_id` and `age_seconds`. A field that cannot be read shows `onbeschikbaar: <reason>` for that field of that builder only, and the project counts as `onleesbaar` in the status line. Without a builder the block says `bouwers: geen`; when the live work itself cannot be read it says `bouwers: onbeschikbaar: <reason>`.
- `te promoveren` counts the `proposed` deliverables: on the now-tracks, and in the whole project.
- The `ververs` button (hotkey `r`) refreshes.

Every row is cut to the pane's width in display cells: a wide character takes two cells and a combining mark none. The PR list, the track list and the builder list share the rows the pane has per project: they get items by turns, and a list that is cut keeps one row for its `+N meer`.

The pane draws with `Box`, `Text` and `Button` only, the elements every surface has, so it draws the same in the terminal, the desktop app, VS Code and the mobile app through Remote Control.

Where no surface places the pane, as in a headless `claude --print "/voortgang"` run, the command answers with one summary line per project instead:

```
project-a: 1 live · 2 PR · 20 open · now ci-profile-a-xdist, provider-schakelaar, voortgang-mod · next 20 · later 109 · 105 te promoveren
```

### Absence is loud

A read that failed is never shown as zero or as an empty list.

| Situation | What the project shows |
|---|---|
| The data root cannot be listed | the whole pane and the status line: `onbeschikbaar: <reason>`, never an empty list; the header says `aantal onbekend` |
| The store directory vanished between the listing and the read | `geen store` |
| The project id is not one directory name (letters, digits, `.`, `_` and `-`, starting with a letter or digit) | `ongeldige project-id`, and nothing is read for it |
| A file is missing, unreadable or not JSON | that part: `onbeschikbaar: <reason>` |
| A track or deliverable entry that `vnx` lists is not an object | that part: `onbeschikbaar: vnx gaf geen object op plaats <n>`; the entry is never dropped silently |
| The store has no `runtime_coordination.db` | tracks and deliverables: `onbeschikbaar: runtime_coordination.db ontbreekt`; `vnx` is not run for it |
| A refresh rejects | the status line and the first pane row: `verversen mislukt: <reason>`; the last good numbers stay below it |
| A block says `available` is anything but `true` | that part: `onbeschikbaar: <the block's own reason>`, else `available: <value>` |
| `vnx` exits non-zero, times out, cannot start or prints no JSON | tracks and deliverables: `onbeschikbaar: vnx exit 1` (or `vnx time-out`, `vnx startfout: ...`, `vnx gaf ongeldige JSON`); the file parts still show |
| `t0_index.json` or `t0_state.json` is older than 60 minutes, or has no timestamp | its age and `verouderd` next to the numbers it carries |

The age always shows (`index 3 min`). A stale index can say `0 live` while a worker runs, which is why it is marked.

## Install

The mod is part of a VNX release: a release copies `packages/` into `~/.vnx-system/versions/<version>/`, and `~/.vnx-system/current` points at the installed one. Install it from there on each Claude account you use:

```
claude plugin marketplace add ~/.vnx-system/current/packages/vnx-mods
claude plugin install voortgang@vnx-mods
```

For a second account, the same with its config directory in front:

```
CLAUDE_CONFIG_DIR=~/.claude-salesminds claude plugin marketplace add ~/.vnx-system/current/packages/vnx-mods
CLAUDE_CONFIG_DIR=~/.claude-salesminds claude plugin install voortgang@vnx-mods
```

The marketplace is a folder, so an update of VNX that rewrites `current` reaches the mod after `/reload-plugins`.

For development from a checkout of this repository:

```
claude --plugin-dir packages/vnx-mods/voortgang
```

It needs the `vnx` binary (VNX 1.7.2 or newer) and the project stores under the data root.

## Options

Set them in `/config`, where each one is a row, or in settings under `pluginConfigs.voortgang.options`.

| Option | Default | What it does |
|---|---|---|
| `exclude` | empty | Comma-separated project ids to leave out. |
| `data_root` | `~/.vnx-data` | The directory that holds one store per project. A leading `~` is the `HOME` of the session. |
| `vnx_bin` | `~/.vnx-system/current/bin/vnx` | The `vnx` executable that lists tracks and deliverables. |
| `refresh_seconds` | `120` | How often the status line and the pane refresh. 15 seconds at least. |

When `HOME` is unknown and a path starts with `~`, the mod reads nothing and shows `onbeschikbaar: HOME onbekend, ~ niet op te lossen`.

## What it reads

The mod finds the projects itself. It lists `<data_root>` once and takes every direct child `<id>` that holds `state/t0_index.json` or `state/t0_state.json`, minus the ids in `exclude`, ordered by id. A name that is not one directory name is skipped. Nothing deeper than `<id>/state/` is checked for existence.

Per project `p`, with the store `S = <data_root>/<p>/state`:

1. Whether `S` exists.
2. `S/t0_index.json`: `timestamp`, `live_work` (`available`, `reason`, `counts`, `live[].dispatch_id`) and `open_outcomes` (`available`, `reason`, `total`, `by_outcome`, `items[].dispatch_id`).
3. `S/t0_state.json`: `generated_at` and `pr_queue` (`available`, `reason`, and per open PR `number`, `title`, `ci_status`, `merge_state`, `gates_passed`, `blocked_on`).
4. Whether `S/runtime_coordination.db` exists. `vnx` creates that database when a store has none, so the two commands below are run only for a store that has it.
5. `vnx objective list --json --project-id p --state-dir S`, run in `S` with a 10 second timeout: `track_id`, `priority`, `phase`, `derived_status`, `horizon`.
6. `vnx deliverable list --json --project-id p --state-dir S`, run the same way: `track`, `derived_status`.
7. Per builder, a dispatch `d` in `live_work.live` or `live_work.starting` whose id is one directory name, with the data directory `D = <data_root>/<p>`: `D/dispatches/pending/d/dispatch-spec.json`, fields `role` and `model` only.
8. Per builder, `D/dispatches/pending/d/instruction.md`: the first line that starts with `# `, without the `# `, control characters turned into spaces. The engine's read returns a whole file, so the mod drops everything but that line at once and keeps none of it. It looks for the line in the first 4096 characters only and does not look past them: a file whose first `# ` line lies beyond them, or is cut by them, shows `onbeschikbaar: geen titelregel in de eerste 4096 tekens`. A file without such a line shows `onbeschikbaar: geen titelregel`.

The `age_seconds` of each builder comes from its own `live_work` entry in `t0_index.json`, read in step 2. `stale` and `unmeasured` entries are not builders: their spec and instruction are never read.

`--state-dir` is required: without it, run from a directory outside a VNX repository, `objective list` fails with `sqlite3.OperationalError: unable to open database file`.

The projects are read in parallel, the reads and runs of one project one after the other. The PR title is the only free text the mod shows.

## What it never does

- It reads no other file and lists no other directory than the data root: no other file under the data root, no transcript, report, receipt, event stream, tool name, open-item body, outcome reason, track title or deliverable title, and no other line of an `instruction.md` than its first `# ` line.
- It runs no other command: no `gh`, no `git`, no shell, never `build_t0_state.py` or anything else that writes state.
- It writes nothing: no file and no `$.store`. For a store without `runtime_coordination.db` it does not even run `vnx`, which would create that database. It keeps its model in the session's `$.state` and pins its status line, nothing more.
- It does not open the pane unasked.

## Limits

- A pane with fewer rows than a project's fixed rows plus one `+N meer` row per list still draws those rows. The lists then show their `+N meer` rows only.
- The numbers are as fresh as the last refresh and as the index itself. The mod never rebuilds an index.
- The tests ran on Claude Code 2.1.304 with `claude plugin test`. The manual run against real stores was headless, so the pane was checked through the test kit on the terminal and the mobile table, not on a live screen.
- The mod is not a stand-alone tool: it reads VNX data only, so it is not published to a public marketplace.

## Development

```
claude plugin validate packages/vnx-mods
claude plugin validate packages/vnx-mods/voortgang
claude plugin test packages/vnx-mods/voortgang
bash packages/vnx-mods/voortgang/tests/no-operator-names.sh
```

- `hooks/register.tsx`: the hooks module (`session.start`, `command.run`, `ui.render`, the timer).
- `hooks/collect.ts`: the collector. Pure functions over parsed JSON, and one function that reads a project through a narrow `Io` that `$` provides in a session and an object in memory provides in a test.
- `hooks/view.ts`: the pane rows, cut to the pane's size.
- `types/index.d.ts`: the `PluginState` contract for the three `$.state` values (`model`, `isRefreshing`, `failure`).
- `tests/`: the engine-driven pane, command, timer and boundary tests, and the collector's tests. `tests/no-operator-names.sh` greps the package for the operator's own project names, because the engine's test runner cannot read files from disk. Fixtures and examples use `project-a` and `project-b`.

Type-check it with the `tsconfig.json` from the header of the `claude-code.d.ts` the engine writes, kept outside this folder, with `include` naming that file and this folder's `hooks`, `types` and `tests`. The engine writes `.claude-plugin/types/` when it hot-reloads the folder; that folder carries its own `.gitignore` and is never committed.
