# voortgang

A VNX mod for Claude Code. It shows the progress of the one VNX project the session is in: the builders at work, what waits on a decision, the tracks with how many of their deliverables are done, what is being worked on and what comes next, and the open PRs with the review seats still owed. It needs VNX: the project stores under `~/.vnx-data` and the `vnx` CLI. It ships with a VNX release and lives in this repository under `packages/vnx-mods/`.

It reads metadata only: ids, counts, statuses, PR numbers, the titles of deliverables, and per builder one title line. It never reads a transcript, a report, a receipt or the body of an open item, so it is safe to load in a session that holds client work.

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

`/voortgang` refreshes and opens the pane `Voortgang`. The pane never opens by itself. The unit of progress is the deliverable: a track row counts deliverables, and names the ones in progress and the ones that come next. PRs are not a unit of a track; an open PR is shown where the work is. The sections of the now view come in this order:

```
project-a · index 2 min
Nu bezig
  opus · 6 min · <first # line of the instruction> · <track> · PR #2150
    PR #2150 · ci loopt · stoel open: codex_gate
  <title of a deliverable>
    PR #2151 · klaar voor merge
Wacht op een besluit
  1 te promoveren (now) · 2 open uitkomsten
  backlog: 12 voorgesteld op next/later
Tracks
  P1 track-a  ▓▓▓░░░░░  3/12
     bezig:  K2a-3b
     volgt:  K2a-3c · K2b · K3  (+6)
  P1 track-b  ▓▓▓▓▓▓▓▓  8/8
  P2 track-c  geen leveringen gepland
opruimen: 10 stale rijen
```

- **Header:** the project id and the age of the index. `verouderd` stands once, on the header, when the index or the state is older than 60 minutes or has no timestamp. In the plan view `· plan` follows.
- **Nu bezig:** every builder of `live_work` (the live and the starting entries), one row each: `model` of its `dispatch-spec.json`, its minutes (`age_seconds`), the first line of its `instruction.md` that starts with `# `, and the `track_id` and `pr_id` of the spec when it sets them. A spec without `model` shows `model ?`; its track and PR still link (the spec's `model` is optional). A long title gives way to the track and the PR behind it. `Nu bezig: niets` when there is none and no PR is open.
  - **The open PRs sit here, and nowhere else.** A PR is on the builder whose spec names its number as `pr_id`, or whose dispatch opened it (`live_work.open_prs`). A PR no builder owns keeps a row of its own only when its dispatch id is a deliverable that `bezig:` names on any track the pane shows, whatever its horizon, under the title of that deliverable. Every other open PR is folded into one row at the end of the section: `N open PR's zonder bouwer · M klaar voor merge · K wacht op stoel · J ci rood`, each count left out when it is 0 (`klaar voor merge` in ok, `wacht op stoel` in warn, `ci rood` in error, the rest dim). The plan view does not show it. A PR row says `PR #N · ci loopt|rood|groen · stoel open: <seats>` (the seats of `blocked_on`), or `PR #N · klaar voor merge` when nothing is owed and CI is green. A PR the index lists and the readable PR queue misses counts in `N` only; with an unreadable PR queue the folded row ends in `onbeschikbaar: <reason>`, and when no PR is known at all (the normal shape when `gh` fails) `Nu bezig` carries one row `onbeschikbaar: <reason>` of its own, never both. A builder list that cannot be read is one `onbeschikbaar: <reason>` row; the PRs of a readable queue are still drawn after it, all folded because no builder is known.
- **Wacht op een besluit:** `N te promoveren (now)` counts the `proposed` deliverables on tracks with horizon `now`; with `open uitkomsten` after it. Below it one dim row `backlog: N voorgesteld op next/later`, only when N is above 0. A proposed deliverable on a track whose phase is `done` (an orphan), on a track without horizon, or on a track the track list does not know, is not counted and not shown. When the tracks or the deliverables cannot be read the row says `te promoveren onbeschikbaar: <reason>`: without the tracks a deliverable cannot be placed on a horizon.
- **Tracks:** every track on the `now` horizon, plus every track with activity: a builder whose spec names it, an open PR that its `pr_ref` lists or that the dispatch spec of the PR names, or a deliverable of it with `last_activity` in the last 7 days. They are ordered by what is going on, in four tiers: a builder whose spec names the track (the track with the most builders first), then an open PR linked to the track, then a deliverable with `last_activity` in the last 7 days, then no activity (a `now` track shown only for its horizon). Within a tier by priority (P1, P2, P3, other), then by id.
  - The first row of a track with deliverables is its priority, its id, a bar of 8 cells (`▓` for done, `░` for the rest, rounded down) and `done/total`: the total counts every deliverable of the track, done those whose `derived_status` is `done`.
  - `bezig:` names the deliverables that are `ready` or `in_progress`, or that a running builder carries (the builder's dispatch id is the deliverable's id, the part of its `deliverable_ref` after the colon), and the builders on the track whose dispatch is no deliverable of it (by the builder's title line). A deliverable is named by its title up to the first `:` or the first 40 cells, whichever is shorter.
  - `volgt:` names the `proposed` deliverables in the order `vnx deliverable list` gives them, as many as fit the width, then `(+N)` for the rest. The count is never cut: the first title is shortened (with `…`) to make room for it, and where not even one title fits only `(+N)` stays. The row is left out when there are none, and so is `bezig:`.
  - A track without deliverables shows `geen leveringen gepland`, dim, on its first row. That is a signal: the progress of that track is not measurable. When the deliverables themselves could not be read the second row says `leveringen onbeschikbaar: <reason>`.
  - When `vnx deliverable list` fails, a track whose only activity was a recent deliverable cannot be recognised. The section then starts with one row `leveringen onleesbaar: <reason>, tracks met alleen recente leveringen ontbreken`.
- **opruimen:** one dim row at the bottom with the `stale` and `unmeasured` counts of `live_work`, only when one of them is above 0. They appear nowhere else.

**Colour.** A row is a list of segments and each segment has a tone. `ok` (the theme's success colour) is the filled part of a bar, a track whose deliverables are all done, `ci groen` and `klaar voor merge`. `warn` is what waits on the operator: the `te promoveren (now)` count, a `proposed` deliverable in `volgt:`, a seat still owed. `error` is `ci rood` and every `onbeschikbaar`. `dim` is the empty part of a bar, the backlog row, `(+N)`, the count of tracks without horizon. Every row reads correctly without colour; the text carries the meaning and the tone adds to it. Width is measured on the text.

### The plan view

`/voortgang plan` opens the same pane as a plan: the tracks with horizon `next`, then those with `later` (each under its own word), each with its bar and its `volgt:` row, ordered by priority and id. A track whose phase is `done` is left out. The tracks without a horizon are one dim count, `N tracks zonder horizon`. `/voortgang` without an argument opens the now view again; any other argument is answered with the two forms and opens nothing.

**Room.** The whole pane is for this one project. Sections take rows in the order above. A section that does not fit whole shows the items that fit plus one row `+N niet getoond`, and never a `+N` row without at least one item above it. A section of which not even one item fits is not drawn. Every row is cut to the pane's width in display cells: a wide character takes two cells and a combining mark none; `▓` and `░` take one.

The pane draws with `Box`, `Text` and `Button` only, the elements every surface has, so it draws the same in the terminal, the desktop app, VS Code and the mobile app through Remote Control. The `ververs` button (hotkey `r`) refreshes.

### The status line

After every refresh the mod pins one line under the prompt. It answers the question of the pane at a glance: how far is this project, and does something run.

```
project-a · track-a ▓▓▓▓▓░░░ 5/8 · track-b ▓▓░░░░░░ 2/8 · 1 bouwer
project-a · track-a: geen plan · track-b ▓▓▓▓▓▓░░ 6/8 · 2 bouwers
project-a · geen tracks in beweging
```

- The tracks are the first two of the pane's Tracks order: builders first, then open PRs, then recent deliverables, then priority, then id. A track with deliverables shows the same 8-cell bar as the pane and `X/Y` (done of all). A track without deliverables shows `: geen plan`, one whose deliverables could not be read shows `: ?`.
- A track id longer than 28 display cells is cut with `…`.
- `1 bouwer` or `N bouwers` stands only when at least one builder runs (`live_work.counts.live + live_work.counts.starting`; `stale` and `unmeasured` entries are not builders). It is left out at 0 and when the live work could not be read.
- `verouderd` is added when the index or the state is older than 60 minutes. `onleesbaar` is added when a part could not be read.
- When no track is shown the line says `geen tracks in beweging`. When the tracks could not be read it says `tracks onbekend`, never that nothing moves.
- The PR count, `te promoveren` and `uitkomsten` are not on the line; they stay in the pane.
- Without a project the line is empty.

### Headless

Where no surface places the pane, as in `claude --print "/voortgang"`, the command answers with the status line, then one line per track the pane would show:

```
project-a · track-a: geen plan · track-b ▓▓▓▓▓▓░░ 6/8 · 1 bouwer
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
6. `vnx objective list --json --project-id p --state-dir S`, run in `S` with a 10 second timeout: `track_id`, `priority`, `horizon`, `phase`, `pr_ref`.
7. `vnx deliverable list --json --project-id p --state-dir S`, run the same way: `track`, `derived_status`, `last_activity`, and per deliverable its `deliverable_ref` and its `title`.
8. Per builder, a dispatch `d` in `live_work.live` or `live_work.starting` whose id is one directory name: `D/dispatches/pending/d/dispatch-spec.json`, fields `model`, `track_id` and `pr_id` only.
9. Per builder, `D/dispatches/pending/d/instruction.md`: the first line that starts with `# `, without the `# `, control characters turned into spaces. The engine's read returns a whole file, so the mod drops everything but that line at once and keeps none of it. It looks in the first 4096 characters only: a file whose first `# ` line lies beyond them, or is cut by them, shows `onbeschikbaar: geen titelregel in de eerste 4096 tekens`. A file without such a line shows `onbeschikbaar: geen titelregel`.
10. Per open PR in `live_work.open_prs` whose dispatch id is one directory name: the `track_id` of `D/dispatches/pending/d/dispatch-spec.json`. A dispatch that has left `pending/` has no spec; the PR then belongs to the tracks whose `pr_ref` lists it, and nothing is shown as missing.

The `age_seconds` of each builder comes from its own `live_work` entry. `stale` and `unmeasured` entries are not builders: their spec and instruction are never read.

`--state-dir` is required: without it, run from a directory outside a VNX repository, `objective list` fails with `sqlite3.OperationalError: unable to open database file`.

## What it never does

- It lists no directory. It reads no other file than the ones above: no other project's store, no transcript, report, receipt, event stream, tool name, open-item body, outcome reason or track title, and no other line of an `instruction.md` than its first `# ` line.
- It runs no other command: no `gh`, no `git`, no shell, never `build_t0_state.py` or anything else that writes state.
- It writes nothing: no file and no `$.store`. For a store without `runtime_coordination.db` it does not even run `vnx`, which would create that database. It keeps its model in the session's `$.state` and pins its status line, nothing more.
- It does not open the pane unasked.
- It never shows another project than the one of the session.

## Limits

- A pane with fewer rows than the sections need draws the sections that fit, in order. With very few rows a section that does not fit one item is left out without a marker.
- The numbers are as fresh as the last refresh and as the index itself. The mod never rebuilds an index. The PR queue is as good as the fabric's `gh` call: when that fails, the pane shows the reason.
- An open PR shows in Nu bezig only. Activity of an open PR is read from `pr_ref` and from the spec of the PR's dispatch while it is in `pending/`. A PR whose dispatch has left `pending/` and that no `pr_ref` lists links to no track.
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
- `types/index.d.ts`: the `PluginState` contract for the five `$.state` values (`model`, `isRefreshing`, `failure`, `cwd`, `view`).
- `tests/`: the engine-driven pane, command, timer and boundary tests, and the collector's tests. `tests/no-operator-names.sh` greps the package for the operator's own project names, because the engine's test runner cannot read files from disk. Fixtures and examples use `project-a`.

Type-check it with the `tsconfig.json` from the header of the `claude-code.d.ts` the engine writes, kept outside this folder, with `include` naming that file and this folder's `hooks`, `types` and `tests`. Without a loaded mod, load the `plugin-authoring` skill (it names the `types/claude-code.d.ts` of the running build) and write a `tsconfig.json` outside this folder: `strict` and `noUncheckedIndexedAccess` on, `noEmit`, `skipLibCheck`, `types: []`, `module: esnext`, `moduleResolution: bundler`, `lib` and `target` `es2023`, `jsx: react` with `jsxFactory: h` and `jsxFragmentFactory: Fragment`, and `include` naming that file plus this folder's `hooks`, `types` and `tests`. Run `npx --yes -p typescript@5.6.3 tsc -p tsconfig.json` from the folder holding that file; exit 0 is the bar. The engine writes `.claude-plugin/types/` when it hot-reloads the folder; that folder carries its own `.gitignore` and is never committed.
