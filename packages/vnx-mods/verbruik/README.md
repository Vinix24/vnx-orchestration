# verbruik

A VNX mod for Claude Code. It puts the usage of every subscription and the balance of the API providers in one status line:

```
verbruik: claude 5u 34% · week 61% · codex 58% · kimi terug over 1d 19u (uiterlijk 24-10 13:05) !! · deepseek $12.92 · openrouter $4.91 !
```

The `verbruik: ` at the start is the engine's: it puts the plugin name in front of every status entry. The labels are Dutch (`5u`, `week`, `vol tot reset`, `(oud)`, `nog geen meting`). It needs VNX 1.7.4 or newer for the providers other than Claude. It ships with a VNX release and lives in this repository under `packages/vnx-mods/`.

## What it shows

| Segment | Source | Example |
|---|---|---|
| Claude, the 5-hour window | `$.session.usage().rateLimits`, kind `five_hour` | `claude 5u 34%` |
| Claude, the weekly window | the same, kind `seven_day` | `week 61%` |
| codex | `used_percent` of its row | `codex 58%` |
| kimi | `status` of its row as text; the percentage when a later schema carries one | `kimi vol tot reset`, or with `resets_at_latest` (an upper bound, local time) `kimi terug over 1d 19u (uiterlijk 25-10 14:05)`, and after that bound `kimi laatst vol, grens 25-10 14:05 voorbij` |
| deepseek | `balances[].total`, in its currency | `deepseek $12.92` |
| openrouter | `remaining` | `openrouter $4.91` |

The `claude` row of `vnx providers usage` is ignored: it says it can only be measured inside a session, and the session is the source.

A figure is never written as 0 when it is missing:

- No reading from the session: `claude: nog geen meting`. The session holds none at `session.start` and fills it at the first `session.measure`.
- A row with `available: false` shows the provider and a short form of its `reason` (`codex geen rate_limits-regel`), cut at ` in ` or a bracket, without any path or file name.
- A run that fails (a non-zero exit, no JSON, JSON that is no report, another `schema` than `vnx.providers.usage/1`) keeps the last good figures and marks each `(oud)`. The cause is named once, at the end: `vnx-meting mislukt: vnx gaf exit 1`.

### Colours

The status line is plain text. Part 0 of the build measured that `$.ui.status` takes no style (an escape sequence is drawn as a stray character) and that the engine draws the whole entry in one colour of its own. So the tone of a segment is carried by the text: a warning ends in ` !` and an error in ` !!`. Below the warn threshold nothing is added.

The tone of a segment is computed per segment (`success`, `warning`, `error`, `dim` for what could not be measured) and tested on its boundaries.

| Figure | `success` | `warning` | `error` |
|---|---|---|---|
| a used percentage | below `warnPercent` | from `warnPercent` up to `errorPercent` | from `errorPercent` up |
| a balance | above `warnBalance` | at or below `warnBalance`, above `errorBalance` | at or below `errorBalance` |
| kimi status | `bereikbaar` | any other status, and `laatst vol, grens ... voorbij` | `vol tot reset` and `terug over ...` |

With the defaults 69 is `success`, 70 and 89 are `warning`, 90 is `error`; a balance of 5.01 is `success`, 5 and 1.01 are `warning`, 1 is `error`. A deepseek row with several currencies takes the worst tone of its balances.

### The kimi countdown

The bound is the first weekly-limit failure of the current streak plus 7 days: an upper bound, never the exact reset. Before it the text counts down to it: `1d 19u` above 24 hours, `5u 07m` from 1 to 24 hours, `42m` under an hour, rounded up on the smallest unit shown. At or after the bound the `vol` status is older than the bound and no new measurement, so the text names only the stored bound and the tone drops to a warning. The countdown is not a live ticker: it is computed again on every render (`session.start`, every `session.measure`, every refresh, a width change and `/verbruik`) from the engine's clock.

### Width

The status line is given no width. A hook on the prompt hint, which draws nothing, receives the terminal width at every resize, and the mod fits the line to it, reserving 14 cells for the indent, the glyph and the `verbruik: ` prefix. A narrow terminal drops segments in this order: the cause or the old-vnx notice, openrouter, deepseek, kimi, codex, the weekly Claude figure. The Claude 5-hour figure stays. Until the width is known nothing is dropped.

### `/verbruik`

Refreshes the measurement and answers with the full figures as text lines: the Claude windows with their reset time, every other provider with its numbers or its reason, `gemeten: <measured_at>`, and the cause of a failed run.

## An older vnx

An installed fabric older than the release that ships `vnx providers usage` (1.7.3 and before) refuses the verb: argparse exits 2 with `invalid choice: 'usage'`. The mod recognises that failure itself, not a version string, and shows only the Claude part with a notice:

```
verbruik: claude 5u 34% · week 61% · vnx te oud voor providers usage
```

It asks again at every refresh, so the other providers appear after the fabric is updated.

## Install

The mod is part of a VNX release: a release copies `packages/` into `~/.vnx-system/versions/<version>/`, and `~/.vnx-system/current` points at the installed one. Install it from there on each Claude account you use:

```
claude plugin marketplace add ~/.vnx-system/current/packages/vnx-mods
claude plugin install verbruik@vnx-mods
```

For development from a checkout of this repository:

```
claude --plugin-dir packages/vnx-mods/verbruik
```

## Options

Set them in `/config`, where each one is a row, or in settings under `pluginConfigs.verbruik.options`.

| Option | Default | What it does |
|---|---|---|
| `vnxBin` | `~/.vnx-system/current/bin/vnx` | The `vnx` executable that runs `providers usage`. A leading `~` is the `HOME` of the session. |
| `refreshMinutes` | `10` | How often the other providers are measured again. 2 minutes at least. |
| `warnPercent` | `70` | A used percentage from here up is a warning. |
| `errorPercent` | `90` | A used percentage from here up is an error. |
| `warnBalance` | `5` | A balance at or below this, in its own currency, is a warning. |
| `errorBalance` | `1` | A balance at or below this, in its own currency, is an error. |

## What it reads, and what it never does

- The Claude figures come from the session. The other figures come from one command, `vnx providers usage --json`, run with a 25 second timeout and one run at a time. A run asked while another is going joins it.
- The mod never reads an API key, never calls a provider itself and never runs anything but that command. It shows what the command returns: the `source`, `store` and `plan_type` fields are never read, and a word that looks like a path or a file name is cut from every reason. It does not print the path of `vnxBin`.
- The command makes no model call. It reads balance endpoints and local state.

## Claude Code versions

Measured on Claude Code 2.1.306 (a Max subscription, a throwaway tmux session, `--plugin-dir` twice):

- **Two plugins that call `$.ui.status`** each keep their own entry. The engine draws one row per plugin and puts the plugin name in front (`probea: ...`, `voortgang: ...`); the last call does not replace the other plugin's. So the mod uses `$.ui.status` and draws nothing in `AbovePrompt`.
- **`$.session.usage().rateLimits`** is empty at `session.start` (`[]`). After the first `session.measure`, both on the event and from `$.session.usage()`, it holds:

```
[{"kind":"five_hour","percentUsed":18,"resetsAt":"2026-10-21T23:00:00.000Z"},
 {"kind":"seven_day","percentUsed":31,"resetsAt":"2026-10-27T16:00:00.000Z"}]
```

  `percentUsed` is a percentage from 0 to 100 (18, not 0.18) and `resetsAt` an ISO timestamp. The measure that carried it named `context`, `rateLimits` and `cost` as changed.
- **Style in a status entry:** none. `\u001b[32m` is drawn as a stray character and the entry has one colour, so the mod marks warnings with `!` and `!!`.
- **Width:** only a `ui.render` hook is told the terminal width (`e.viewport.columns`). A hook on `PromptHint` is called at the start and at every resize, and the mod reads the width there.

## Development

```
claude plugin validate packages/vnx-mods
claude plugin validate packages/vnx-mods/verbruik
claude plugin test packages/vnx-mods/verbruik
bash packages/vnx-mods/verbruik/tests/no-operator-names.sh
```

- `hooks/register.ts`: the hooks module (`session.start`, `session.measure`, the prompt-hint hook for the width, `command.run`, the timer).
- `hooks/usage.ts`: the pure part. Parsing the report, the state between runs, the tones, the segments, the fit and the text of `/verbruik`.
- `types/index.d.ts`: the shapes the two share.
- `tests/`: `usage.test.ts` for the pure part and `hooks.test.ts` for the mod in a session over an engine in memory. The fixtures are the rows `vnx providers usage --json` writes, with `source` and `store` left out. `tests/no-operator-names.sh` greps the package for the operator's own project names.

Type-check it with a `tsconfig.json` kept outside this folder: `strict` and `noUncheckedIndexedAccess` on, `noEmit`, `skipLibCheck`, `types: []`, `module: esnext`, `moduleResolution: bundler`, `lib` and `target` `es2023`, `jsx: react` with `jsxFactory: h` and `jsxFragmentFactory: Fragment`, and `include` naming the `claude-code.d.ts` the engine writes plus this folder's `hooks`, `types` and `tests`. Run `npx --yes -p typescript@5.6.3 tsc -p tsconfig.json`; exit 0 is the bar.
