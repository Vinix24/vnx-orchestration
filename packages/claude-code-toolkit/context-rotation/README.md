# context-rotation

A Claude Code plugin that rotates a long session before it degrades. It measures how full the context is, asks Claude for a handover note at set points, checks that note, and gives it to the fresh session after `/clear`.

**Check your window first.** The bands are a share of `window_tokens`, which defaults to 200,000. Claude Code does not tell a hook the real window size, so it is a setting. On a 1M window with the defaults the plugin rotates early, never late: the warn band starts at 110,000 tokens, about 11% of the real window. Set `"window_tokens": 1000000` in `.claude/context-rotation.json` to move the bands up.

## The problem

Long Claude Code sessions get worse well before the context is full, and auto-compaction fires late and keeps an unpredictable part of the work. A manual `/clear` throws the thread away. This plugin keeps the thread: it has Claude write down where things stand, then continues from that note in a fresh context.

## Install

From the public marketplace, inside Claude Code:

```
/plugin marketplace add Vinix24/claude-code-toolkit
/plugin install context-rotation@claude-code-toolkit
```

The same from a terminal: `claude plugin marketplace add Vinix24/claude-code-toolkit`, then `claude plugin install context-rotation@claude-code-toolkit`.

To install it for one project only, run the second command with project scope, from inside that project:

```
claude plugin install context-rotation@claude-code-toolkit --scope project
```

The plugin is on from the moment it is installed. It needs `python3` (3.9 or newer, standard library only) and `sh`. The automatic clear also needs tmux.

## How it works

1. **Measure.** Before each prompt and tool call, and at Stop, `lib/context_budget.py` reads the transcript backwards and takes the input, cache-read and cache-creation tokens of the last assistant message. After a compaction it uses the size Claude Code records for it. A size it cannot read is unknown, and an unknown size never warns or blocks.
2. **Warn.** At `warn_pct` of the window Claude is told to wrap up and prepare a handover. At `force_pct` it is told to write it now, and the turn is held back once so it can. At `hard_pct` every tool except reads, questions, the todo list and the handover write itself is denied. Typing `/context-rotation:rotate release` ends the enforcement for that session.
3. **Hand over.** Claude writes `.claude/rotation/handover-<session id>.md` with the `rotate` skill (`skills/rotate/SKILL.md`): goal, current state, what still runs, numbered next steps, files touched, open risks. At most `handover_max_chars` characters.
4. **Check.** `hooks/handover_detector.py` validates the file after each Write or Edit. A valid one gets a ready-marker. Any later prompt or work that changes the picture makes the marker outdated, and an outdated handover is never injected.
5. **Clear.** At Stop with a current marker, inside tmux and with `auto_clear` on, `scripts/tmux_clear.sh` types `/clear` into the session's own pane once it is idle. Anywhere else the plugin tells you to type `/clear` yourself.
6. **Continue.** `hooks/session_start.py` runs in the fresh session. It copies the handover to `.claude/rotation/archive/`, logs it in `injections.jsonl` and puts it in the session's context. The job then types one fixed line, `Continue from the handover in your context, step 1.`

The hook entries are `hooks/context_guard.py` (prompt, tool call, Stop), `hooks/handover_detector.py` (Write and Edit), `hooks/session_start.py` (SessionStart) and the shim `hooks/run.sh` that starts them. The logic sits in `lib/`. `/context-rotation:rotate resume` lists earlier handovers and injects the one you pick.

## Settings

Settings are read from `.claude/context-rotation.json` in the project, else `~/.claude/context-rotation.json`, else the defaults. A bad value falls back to its own default with a message on stderr. The environment variable `CLAUDE_CONTEXT_ROTATION=off` switches the hooks off for a shell.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | `false` switches the plugin off for the project (or for every project in the user file). |
| `window_tokens` | `200000` | The context window the percentages refer to. |
| `warn_pct` | `55` | Share of the window where Claude is told to prepare a handover. |
| `force_pct` | `65` | Share where Claude is told to write it now and the turn is held back once. |
| `hard_pct` | `75` | Share where tools other than the allowed few are denied. Needs `0 < warn < force < hard <= 100`, else all three fall back. |
| `auto_clear` | `true` | Type `/clear` for you inside tmux. `false` leaves it to you. |
| `handover_max_chars` | `8000` | Longest handover the check accepts. |
| `handover_max_age_minutes` | `60` | A marker older than this is outdated, whatever happened since. |
| `hard_max_denials` | `3` | Denials in the hard band per session before it only informs. |
| `archive_keep` | `20` | Injected handovers kept in `.claude/rotation/archive/`. |
| `busy_pattern` | `^\S+ \S+… \(\d+s` | Regular expression for the spinner line of a busy pane. |
| `prompt_pattern` | `^❯ ` | Regular expression for the input line of an empty prompt. The space after `❯` is a no-break space (U+00A0). |
| `ack_timeout_seconds` | `20` | How long the job waits for the fresh session to take the handover before it types the continuation line. |
| `force_deny_tools` | `[]` | Tool names, or `fnmatch` patterns, that the force band denies. None by default. |

To switch the plugin off in one project, put this in `.claude/context-rotation.json`:

```
{"enabled": false}
```

## The automatic clear

`auto_clear` is on by default. Set `"auto_clear": false` in either settings file and the plugin only tells you to type `/clear`. The first automatic clear on a machine says so in the fresh session.

The clear happens only when all of these hold:

- Claude Code sends `background_tasks` at Stop. A version that does not never gets the automatic clear, only the message.
- Nothing is running in the background. A background shell or subagent counts as running, and so does a task whose status the plugin does not recognise. Then you are told what still runs.
- The pane shows an idle prompt: no spinner, an input line matching `prompt_pattern`, and the session's own `claude` process running in that pane.
- The handover is still current when the job wakes up.

If anything is in the input line, the job types nothing. It shows the reason in the tmux status line, and the next prompt you send repeats it. What you typed stays as it is. Every key the job types is read back from the input line before Enter is sent.

## Limits

- The automatic clear needs tmux. Without it you type `/clear` yourself and the handover still arrives.
- The window size is a setting (see the top of this file).
- Windows is not supported.
- `/resume` brings back the old session, not the rotated one. The handover of the old session stays in `.claude/rotation/archive/`.
- Do not run it next to another rotation guard. Two guards hold back the same turn and clear the same pane. Switch this one off in a project with `"enabled": false` in `.claude/context-rotation.json`.
- A fresh session does not start empty. Your CLAUDE.md files, tools and the handover are already in its context, about 60K tokens on a setup with large CLAUDE.md imports. If that start is close to the force band (`window_tokens` x `force_pct` / 100), the fresh session crosses the band after one turn and rotates again, and nothing stops that loop yet. Keep the force band well above your start context. Run `/context` in a fresh Claude Code session to see it.
- What the write guard does and does not stop is in the next section.

## Files the plugin writes

In the project, under `.claude/rotation/` (a `.gitignore` with `*` is created first):

- `handover-<session id>.md`: the handover of a session.
- `ready-<session id>.json`, `outdated-<session id>.json`, `claimed-<origin>-<generation>.json`: the marker of a handover in its three stages.
- `injections.jsonl`: one line per step a handover goes through.
- `enforcement.jsonl`: one line per change of a session's force or hard state.
- `state/`: per-session counters, the enforcement state and the list `resume` showed.
- `archive/`: every injected handover, trimmed to `archive_keep`.
- `clear-<n>.json` and `clear-<n>.lock`: the record and the lock of the automatic clear in tmux pane `%<n>`.
- `errors.log`: the last failures a hook caught.

In `~/.claude/context-rotation/`: the job files of the automatic clear (in `jobs/`, removed once read) and the marker of the one-time note about `auto_clear`.

## Claude Code versions

The hook payloads and pane screens the plugin relies on were measured on Claude Code 2.1.300. The live smoke of the automatic clear passed on 2.1.302. `tests/live/smoke_tmux.sh` repeats that smoke in a throwaway tmux session.

Credits: the rotation mechanism comes from the context rotation that VNX Orchestration runs for its orchestrator terminal.

## What the guard does not do

The guard stops one thing: a direct write by Claude's file tools (Write, Edit, MultiEdit and NotebookEdit) to the plugin's own files. Those are the project's `.claude/rotation/` and the two settings files. `.claude/rotation/` holds the handovers, the markers and the per-session state that decides what the force and hard bands deny. The settings are in `.claude/context-rotation.json` in the project and in `~/.claude/context-rotation.json`, and the plugin reads the first of the two that exists.

Such a write is denied in every band, also below the warn threshold, in plan mode, after a release and inside a subagent. The one exception is a write of the session's own handover. A denial of this kind never counts toward `hard_max_denials`. Where a write lands is judged by the real directories, so a `..`, a symlink or a name in another case does not get one in. Each way of reading the path is judged on its own, and one that lands on a plugin file is enough for a denial. Only when none of them does and one could not be judged, for example on a directory the hook cannot read, does the call go ahead. The failure is then logged in `.claude/rotation/errors.log`.

It is no lock against a model that sets out to switch the plugin off. A shell command (the Bash tool) or a script runs with your rights and can change any of these files. The plugin does not parse shell commands. A change to Claude Code's own settings can remove the plugin's hooks. The tools of an MCP server are not looked at. None of that is stopped.

Typing `/context-rotation:rotate release` is the only release the plugin itself offers. You can always also edit the settings file yourself.
