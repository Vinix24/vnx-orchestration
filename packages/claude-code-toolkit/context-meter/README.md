# context-meter

One line above the prompt that shows how full the context is, in the colours of the bands the `context-rotation` guard uses: green below warn, yellow from warn, red from force.

```
context ▓▓▓▓▓░░░ 412K · warn: wrap up · force at 500K
```

The guard warns and rotates when a band is hit. Until then you see nothing. The meter shows where you are before that, and what the guard will do next.

It is a Claude Code mod: a hooks module that draws the band above the prompt. It reads the guard's settings and its per-session state. It writes nothing and runs nothing outside the session.

## Install

```
/plugin install context-meter --marketplace Vinix24/claude-code-toolkit
```

It works without `context-rotation`, on the default bands. Install both to have the line and the guard agree by construction: the meter reads the guard's settings file.

## The context meter

The line has up to five parts, left to right: the fill with an 8-cell bar, the band word, what the guard does there, the next threshold, and a note when something does not add up.

| State | Line | Colour |
|---|---|---|
| Below warn | `context ▓▓▓▓░░░░ 78K · ok · warn at 110K` | green |
| Warn to force | `context ▓▓▓▓▓▓░░ 120K · warn: wrap up · force at 130K` | yellow |
| Force to hard | `context ▓▓▓▓▓▓▓░ 140K · force: write the handover · hard at 150K` | red |
| From hard | `context ▓▓▓▓▓▓▓▓ 160K · hard: only reads are allowed` | red |
| No reading yet | `context: no reading yet` | dim |
| `enabled: false` | `context ▓▓▓▓▓▓░░ 120K · rotation off` | dim |

The text says the same without the colour. The bar is full at the hard threshold.

A narrow terminal drops the trailing parts first: the notes, then the next threshold, then what the guard does. The band word stays. When even that does not fit, the bar goes too.

**No reading yet.** A fresh session, and a session that was just compacted, have no token count until the first response. The meter says so and never shows 0.

**The same measure as the guard.** The fill is `context.tokens` of `$.session.usage()`, the figure the status line reads: the input side of the last response. The guard reads the same count from the transcript: `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` of the last assistant message. They are the same measure, so the band you see is the band the guard acts on.

**After an injected handover.** A session that continues from an injected handover is held below force and hard until it has grown `min_work_tokens` past its start size. The meter reads that start size from the guard's state for this session (`.claude/rotation/state/<session>.enforcement.json`). While the hold applies the band stays yellow at most and says when force starts: `context ▓▓▓▓▓▓░░ 2.1K · warn: wrap up · force from 2.3K`.

## Settings

The meter reads the guard's file: `.claude/context-rotation.json` in the project, else `~/.claude/context-rotation.json` in the home directory (not `CLAUDE_CONFIG_DIR`), else the defaults. A bad key falls back to its own default. The percentage triple and the token triple are validated as one, and absolute tokens win over percentages. This is the same logic as `context-rotation`, ported without drift. The test suites of both plugins read one fixture file with 43 settings cases and 14 min-work cases to keep it so.

| Key | Default | What the meter does with it |
|---|---|---|
| `show_meter` | `true` | `false` hides the line. A value that is not a boolean counts as `true`. |
| `enabled` | `true` | `false` shows the fill dim with `rotation off`, no band colour. |
| `window_tokens` | `200000` | The window the percentages refer to. |
| `warn_pct`, `force_pct`, `hard_pct` | `55`, `65`, `75` | The bands as a share of `window_tokens`. |
| `warn_tokens`, `force_tokens`, `hard_tokens` | none | Absolute bands. All three set to ascending positive integers, they win over the percentages. |
| `min_work_tokens` | `20000` | How far an injected session must grow before force and hard apply. |

## Check your window first

The percentages refer to `window_tokens`, which is 200K unless you set it. A session on a larger window reaches the same percentage much later than you meant. When the bands come from percentages and the session's window differs from `window_tokens`, the line ends in a yellow note:

```
context ▓▓▓▓▓▓░░ 120K · warn: wrap up · force at 130K · window 1M, bands assume 200K
```

Set `window_tokens` to your window, or give absolute `warn_tokens`, `force_tokens` and `hard_tokens`. When `hard_tokens` is above the session's window the guard can never reach hard, and the line says so: `hard 600K is above the 200K window`.

## Claude Code versions

The line needs `$.session.usage()`, the `session.measure` event and the band above the prompt (`AbovePrompt`). Measured on Claude Code 2.1.303 and 2.1.306: the module loads and reads the usage on both. Versions 2.1.300 to 2.1.302 were not measured.

It is a plugin of its own, not part of `context-rotation`, because of that measurement. A plugin that carries both command hooks and a hooks module validates on 2.1.306, and its command hooks keep running. 2.1.303 does not skip such a module either: it loads it. Whether 2.1.300 to 2.1.302 do was not measured, so the guard stays a plugin with command hooks only, and nobody who installs it gets a module they did not ask for.

A live session on 2.1.306 with `--plugin-dir` drew the line in all three colours, with the settings file changed between prompts.

## Limits

- The line is drawn on the terminal and desktop surfaces only. Other surfaces have no band above the prompt.
- A survey in the band takes its place: the meter yields to it.
- The meter shows the guard's state. It does not change it.

## Tests

- `claude plugin test packages/claude-code-toolkit/context-meter`: each band and its tone, no reading, the window notes, the min-work hold, `enabled: false`, `show_meter: false`, a narrow width, and the parity cases.
- `claude plugin validate packages/claude-code-toolkit/context-meter`.
- `python3 -m pytest -q -p no:cacheprovider` in `context-meter`: the isolation checks.
- The parity cases are read by `tests/test_meter_parity.py` in `context-rotation` as well, against the guard's own functions.
