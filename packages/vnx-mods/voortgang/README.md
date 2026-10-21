# voortgang

A VNX mod for Claude Code. It shows the progress of the one VNX project the session is in: the builders at work, what waits on a decision, the tracks with how many of their deliverables are done, and the open PRs with their review seats. It needs VNX: the project stores under `~/.vnx-data` and the `vnx` CLI. It ships with a VNX release and lives in this repository under `packages/vnx-mods/`.

It reads metadata only: ids, counts, statuses, PR numbers, and per builder one title line. It never reads a transcript, a report, a receipt or the body of an open item, so it is safe to load in a session that holds client work.

The labels in the pane are Dutch (`ververs`, `onbeschikbaar`, `geen store`, `verouderd`).

## What it shows

One project per session: the one whose `.vnx-project-id` lies above the session's working directory. A fleet view over all projects is not part of this mod. It is a separate, aggregated product that comes later.

### Which project

The mod walks up from the session's working directory to the first directory that holds `.vnx-project-id`. The first line of that file, with all whitespace removed, is the project id; it must match `^[a-z][a-z0-9-]{1,31}$` (the rule `bin/vnx` follows), else the mod says `geen geldig project-id in <path>`, and the store is `<data_root>/<id>`. The nearest file decides. It never falls back to another project.

Without such a file the pane says:

```
geen VNX-project in deze map (geen .vnx-project-id boven /pad/naar/map)
```

and the status line is empty.

### The pane

`/voortgang` refreshes and opens the pane `Voortgang`. The pane never opens by itself. The sections come in this order:

```
project-a · index 2 min
Nu bezig
  opus · 6 min · <first # line of the instruction> · <track> · PR #2150
Wacht op een besluit
  1 levering te promoveren · 2 open uitkomsten
Tracks
  P1 track-a · bezig
     geen leveringen gepland · PR's: #2141 #2148 dicht, #2150 open · 1 bouwer
  P1 track-b · 6 van 8 klaar ▓▓▓▓▓▓░░
  P2 track-c · 0 van 1 klaar ░░░░░░░░
PR's
  #2150 · ci loopt · geslaagd: geen · open: codex_gate
opruimen: 10 stale rijen
```

- **Header:** the project id and the age of the index. `verouderd` stands once, on the header, when the index or the state is older than 60 minutes or has no timestamp.
- **Nu bezig:** every builder of `live_work` (the live and the starting entries), one row each: `model` of its `dispatch-spec.json`, its minutes (`age_seconds`), the first line of its `instruction.md` that starts with `# `, and the `track_id` and `pr_id` of the spec when it sets them. A long title gives way to the track and the PR behind it. `Nu bezig: niets` when there is none.
- **Wacht op een besluit:** the number of `proposed` deliverables (`te promoveren`) and of `open_outcomes` (`open uitkomsten`). No id lists.
- **Tracks:** every track on the `now` horizon, plus every track with activity: a builder whose spec names it, an open PR that its `pr_ref` lists or that the dispatch spec of the PR names, or a deliverable of it with `last_activity` in the last 7 days. Tracks with activity come first, then by priority (P1, P2, P3, other), then by id.
  - A track with deliverables shows `X van Y klaar` and a bar of 8 cells: `▓` for done, `░` for the rest, rounded down. Y counts every deliverable of the track, X those whose `derived_status` is `done`.
  - A track without deliverables shows `geen leveringen gepland`, dim. That is a signal: the progress of that track is not measurable. When the deliverables themselves could not be read the row says `leveringen onbeschikbaar: <reason>` instead.
  - A second row appears only when there is something to say: the PRs of `pr_ref` (`open` when the PR queue lists them, otherwise `dicht`; more than three closed ones are counted, the open ones are always named) and the number of builders.
- **PR's:** the open PRs of the PR queue, one row each: CI (`groen`, `rood`, `loopt`), the seats that passed (`geslaagd`, from `gates_passed`) and the seats still owed (`open`, from `blocked_on`). An unreadable PR queue shows its reason on one row.
- **opruimen:** one dim row at the bottom with the `stale` and `unmeasured` counts of `live_work`, only when one of them is above 0. They appear nowhere else.

**Room.** The whole pane is for this one project. Sections take rows in the order above. A section that does not fit whole shows the items that fit plus one row `+N niet getoond`, and never a `+N` row without at least one item above it. A section of which not even one item fits is not drawn. Every row is cut to the pane's width in display cells: a wide character takes two cells and a combining mark none; `▓` and `░` take one.

The pane draws with `Box`, `Text` and `Button` only, the elements every surface has, so it draws the same in the terminal, the desktop app, VS Code and the mobile app through Remote Control. The `ververs` button (hotkey `r`) refreshes.

### The status line

After every refresh the mod pins one line under the prompt:

```
project-a · 1 bouwers · 2 PR · 5 te promoveren · 3 uitkomsten · verouderd · onleesbaar
```

- `bouwers` is `live_work.counts.live + live_work.counts.starting`; `stale` and `unmeasured` entries are not builders.
- `verouderd` is added when the index or the state is older than 60 minutes. `onleesbaar` is added when a part could not be read.
- A total that could not be read is `?`, never `0`.
- Without a project the line is empty.

### Headless

Where no surface places the pane, as in `claude --print "/voortgang"`, the command answers with the status line, then one line per track the pane would show:

```
project-a · 1 bouwers · 2 PR · 5 te promoveren · 3 uitkomsten
P1 track-a · geen leveringen gepland
P1 track-b · 6 van 8 klaar
```

### Absence is loud

A read that failed is never shown as zero or as an empty list.

| Situation | What the pane shows |
|---|---|
| No `.vnx-project-id` above the working directory | the message above, an empty status line |
| The file holds no usable project id | `.vnx-project-id in <dir> bevat geen geldige project-id` |
| The store directory is missing | `<id> · geen store` on the header, nothing else |
| The id is not one directory name (letters, digits, `.`, `_` and `-`, starting with a letter or digit) | `ongeldige project-id`, and nothing is read for it |
| A file is missing, unreadable or not JSON | that part: `onbeschikbaar: <reason>` |
| A track or deliverable entry that `vnx` lists is not an object | that part: `onbeschikbaar: vnx gaf geen object op plaats <n>` |
| The store has no `runtime_coordination.db` | tracks and deliverables: `onbeschikbaar: runtime_coordination.db ontbreekt`; `vnx` is not run |
| A refresh rejects | the status line and the first pane row: `verversen mislukt: <reason>`; the last good numbers stay below it |
| A block says `available` is anything but `true` | that part: `onbeschikbaar: <the block's own reason>`, else `available: <value>` |
| `vnx` exits non-zero, times out, cannot start or prints no JSON | tracks and deliverables: `onbeschikbaar: vnx exit 1` (or `vnx time-out`, `vnx startfout: ...`, `vnx gaf ongeldige JSON`) |
| A field of a builder cannot be read | `onbeschikbaar: <reason>` for that field of that builder, and `onleesbaar` in the status line |

The age always shows (`index 3 min`). A stale index can say there are no builders while one runs, which is why it is marked.

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
| `data_root` | `~/.vnx-data` | The directory that holds one store per project. A leading `~` is the `HOME` of the session. |
| `vnx_bin` | `~/.vnx-system/current/bin/vnx` | The `vnx` executable that lists tracks and deliverables. |
| `refresh_seconds` | `120` | How often the status line and the pane refresh. 15 seconds at least. |

The option `exclude` of 0.2 is gone: there is no fleet to leave projects out of. When `HOME` is unknown and a path starts with `~`, the mod reads no store and says `HOME onbekend, ~ niet op te lossen`.

## What it reads

The session's working directory comes from the start of the session. Then:

1. Per directory from the working directory up to `/`, whether `<dir>/.vnx-project-id` exists; the first one found is read, and only its first line is kept.

With the project `p`, the store `S = <data_root>/<p>/state` and the data directory `D = <data_root>/<p>`:

2. Whether `S` exists.
3. `S/t0_index.json`: `timestamp`, `live_work` (`available`, `reason`, `counts`, `live[].dispatch_id`, `live[].age_seconds`, `open_prs[].number`, `open_prs[].dispatch_id`) and `open_outcomes` (`available`, `reason`, `total`).
4. `S/t0_state.json`: `generated_at` and `pr_queue` (`available`, `reason`, and per open PR `number`, `ci_status`, `gates_passed`, `blocked_on`).
5. Whether `S/runtime_coordination.db` exists. `vnx` creates that database when a store has none, so the two commands below are run only for a store that has it.
6. `vnx objective list --json --project-id p --state-dir S`, run in `S` with a 10 second timeout: `track_id`, `priority`, `horizon`, `pr_ref`.
7. `vnx deliverable list --json --project-id p --state-dir S`, run the same way: `track`, `derived_status`, `last_activity`.
8. Per builder, a dispatch `d` in `live_work.live` or `live_work.starting` whose id is one directory name: `D/dispatches/pending/d/dispatch-spec.json`, fields `model`, `track_id` and `pr_id` only.
9. Per builder, `D/dispatches/pending/d/instruction.md`: the first line that starts with `# `, without the `# `, control characters turned into spaces. The engine's read returns a whole file, so the mod drops everything but that line at once and keeps none of it. It looks in the first 4096 characters only: a file whose first `# ` line lies beyond them, or is cut by them, shows `onbeschikbaar: geen titelregel in de eerste 4096 tekens`. A file without such a line shows `onbeschikbaar: geen titelregel`.
10. Per open PR in `live_work.open_prs` whose dispatch id is one directory name: the `track_id` of `D/dispatches/pending/d/dispatch-spec.json`. A dispatch that has left `pending/` has no spec; the PR then belongs to the tracks whose `pr_ref` lists it, and nothing is shown as missing.

The `age_seconds` of each builder comes from its own `live_work` entry. `stale` and `unmeasured` entries are not builders: their spec and instruction are never read.

`--state-dir` is required: without it, run from a directory outside a VNX repository, `objective list` fails with `sqlite3.OperationalError: unable to open database file`.

## What it never does

- It lists no directory. It reads no other file than the ones above: no other project's store, no transcript, report, receipt, event stream, tool name, open-item body, outcome reason, track title or deliverable title, and no other line of an `instruction.md` than its first `# ` line.
- It runs no other command: no `gh`, no `git`, no shell, never `build_t0_state.py` or anything else that writes state.
- It writes nothing: no file and no `$.store`. For a store without `runtime_coordination.db` it does not even run `vnx`, which would create that database. It keeps its model in the session's `$.state` and pins its status line, nothing more.
- It does not open the pane unasked.
- It never shows another project than the one of the session.

## Limits

- A pane with fewer rows than the sections need draws the sections that fit, in order. With very few rows a section that does not fit one item is left out without a marker.
- The numbers are as fresh as the last refresh and as the index itself. The mod never rebuilds an index. The PR queue is as good as the fabric's `gh` call: when that fails, the pane shows the reason.
- Activity of an open PR is read from `pr_ref` and from the spec of the PR's dispatch while it is in `pending/`. A PR whose dispatch has left `pending/` and that no `pr_ref` lists links to no track.
- The tests run on Claude Code with `claude plugin test`. The pane was also rendered through the test kit against a real store; it was not checked on a live screen.
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
- `hooks/view.ts`: the pane sections and rows, cut to the pane's size.
- `types/index.d.ts`: the `PluginState` contract for the four `$.state` values (`model`, `isRefreshing`, `failure`, `cwd`).
- `tests/`: the engine-driven pane, command, timer and boundary tests, and the collector's tests. `tests/no-operator-names.sh` greps the package for the operator's own project names, because the engine's test runner cannot read files from disk. Fixtures and examples use `project-a`.

Type-check it with the `tsconfig.json` from the header of the `claude-code.d.ts` the engine writes, kept outside this folder, with `include` naming that file and this folder's `hooks`, `types` and `tests`. The engine writes `.claude-plugin/types/` when it hot-reloads the folder; that folder carries its own `.gitignore` and is never committed.
