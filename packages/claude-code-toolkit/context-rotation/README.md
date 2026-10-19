# context-rotation

A Claude Code plugin that rotates a long session before it degrades. It measures how full the context is, asks Claude for a handover note at set points, checks that note, and gives it to the fresh session after `/clear`.

**Check your window first.** The bands are a share of `window_tokens`, which defaults to 200,000. Claude Code does not tell a hook the real window size, so it is a setting. On a 1M window with the defaults the plugin rotates early, never late: the warn band starts at 110,000 tokens, about 11% of the real window. Set `"window_tokens": 1000000` in `.claude/context-rotation.json` to move the bands up, or set the bands directly in tokens with `warn_tokens`, `force_tokens` and `hard_tokens` (see "Large windows").

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
2. **Warn.** At `warn_pct` of the window — or at `warn_tokens` when the three token thresholds are set — Claude is told to wrap up and prepare a handover. At `force_pct` (or `force_tokens`) it is told to write it now, and the turn is held back once so it can. At `hard_pct` (or `hard_tokens`) every tool except reads, questions, the todo list and the handover write itself is denied. Typing `/context-rotation:rotate release` ends the enforcement for that session.
3. **Hand over.** Claude writes `.claude/rotation/handover-<session id>.md` with the `rotate` skill (`skills/rotate/SKILL.md`): goal, current state, what still runs, numbered next steps, files touched, open risks. At most `handover_max_chars` characters.
4. **Check.** `hooks/handover_detector.py` validates the file after each Write or Edit. A valid one gets a ready-marker. Any later prompt or work that changes the picture makes the marker outdated, and an outdated handover is never injected.
5. **Clear.** At Stop with a current marker, inside tmux and with `auto_clear` on, `scripts/tmux_clear.sh` types `/clear` into the session's own pane once it is idle. Anywhere else the plugin tells you to type `/clear` yourself.
6. **Continue.** `hooks/session_start.py` runs in the fresh session. It copies the handover to `.claude/rotation/archive/`, logs it in `injections.jsonl` and puts it in the session's context. The job then types one fixed line, `Continue from the handover in your context, step 1.`

The hook entries are `hooks/context_guard.py` (prompt, tool call, Stop), `hooks/handover_detector.py` (Write and Edit), `hooks/session_start.py` (SessionStart) and the shim `hooks/run.sh` that starts them. The logic sits in `lib/`. `/context-rotation:rotate resume` lists earlier handovers and injects the one you pick.

## Settings

Settings are read from `.claude/context-rotation.json` in the project, else `~/.claude/context-rotation.json`, else the defaults. The user settings file is always `~/.claude/context-rotation.json` in the home directory, also when `CLAUDE_CONFIG_DIR` points Claude Code at another config directory, so one file serves several accounts. A bad value falls back to its own default with a message on stderr. The environment variable `CLAUDE_CONTEXT_ROTATION=off` switches the hooks off for a shell.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | `false` switches the plugin off for the project (or for every project in the user file). |
| `window_tokens` | `200000` | The context window the percentages refer to. |
| `warn_pct` | `55` | Share of the window where Claude is told to prepare a handover. |
| `force_pct` | `65` | Share where Claude is told to write it now and the turn is held back once. |
| `hard_pct` | `75` | Share where tools other than the allowed few are denied. Needs `0 < warn < force < hard <= 100`, else all three fall back. |
| `warn_tokens` | — | Absolute token count for the warn band. When `warn_tokens`, `force_tokens` and `hard_tokens` are all set to positive integers with `0 < warn < force < hard`, they decide the bands instead of the percentages. |
| `force_tokens` | — | Absolute token count for the force band (see `warn_tokens`). |
| `hard_tokens` | — | Absolute token count for the hard band (see `warn_tokens`). |
| `min_work_tokens` | `20000` | For a session continued from an injected handover, how much its context must grow past the start size before the force and hard bands apply. |
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

## Large windows

The percentages are a share of `window_tokens`, which defaults to 200,000. On a 1M-token window the warn band then starts at 110,000 tokens, about 11% of the real window, so the plugin rotates early. Two ways to move the bands up:

- Set `window_tokens` to the real window and keep the percentages, for example `{"window_tokens": 1000000}`.
- Or set the bands directly in tokens. On a 1M window:

```json
{"warn_tokens": 400000, "force_tokens": 500000, "hard_tokens": 600000}
```

When all three token settings are set (and in ascending order) the percentages and `window_tokens` no longer decide a band; when both the percentages and the tokens are configured and valid, the tokens win.

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
- The plugin is untested in non-interactive `claude -p` runs. A user-scope install also reaches automated runs started on that account (CI, scripts, agent harnesses), so install with `--scope project` where you work interactively.
- A fresh session does not start empty. Your CLAUDE.md files, tools and the handover are already in its context, about 60K tokens on a setup with large CLAUDE.md imports. The session that continues from an injected handover after `/clear` records that start size and does not reach the force or hard band until it has grown at least `min_work_tokens` past it, so the rotation cannot loop. A session started fresh, or continued with `/resume`, is enforced as before. When the injected start is so close to force that rotating will not make the next session smaller, the user is told once, with the two remedies: raise the force threshold, or start with a smaller context (fewer CLAUDE.md imports). Keep the force band well above your start context. Run `/context` in a fresh Claude Code session to see it.
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

The hook payloads and pane screens the plugin relies on were measured on Claude Code 2.1.300. The live smoke of the automatic clear passed on 2.1.302. The live smoke of 1.0.0 (run 1, the automatic clear, 5 of 5 checks) passed on 2.1.303 against a copy installed from the public marketplace. `tests/live/smoke_tmux.sh` repeats that smoke in a throwaway tmux session.

Credits: the rotation mechanism comes from the context rotation that VNX Orchestration runs for its orchestrator terminal.

## What the guard does not do

The guard stops one thing: a direct write by Claude's file tools (Write, Edit, MultiEdit and NotebookEdit) to the plugin's own files. Those are the project's `.claude/rotation/` and the two settings files. `.claude/rotation/` holds the handovers, the markers and the per-session state that decides what the force and hard bands deny. The settings are in `.claude/context-rotation.json` in the project and in `~/.claude/context-rotation.json`, and the plugin reads the first of the two that exists.

Such a write is denied in every band, also below the warn threshold, in plan mode, after a release and inside a subagent. The one exception is a write of the session's own handover. A denial of this kind never counts toward `hard_max_denials`. Where a write lands is judged by the real directories, so a `..`, a symlink or a name in another case does not get one in. Each way of reading the path is judged on its own, and one that lands on a plugin file is enough for a denial. Only when none of them does and one could not be judged, for example on a directory the hook cannot read, does the call go ahead. The failure is then logged in `.claude/rotation/errors.log`.

It is no lock against a model that sets out to switch the plugin off. A shell command (the Bash tool) or a script runs with your rights and can change any of these files. The plugin does not parse shell commands. A change to Claude Code's own settings can remove the plugin's hooks. The tools of an MCP server are not looked at. None of that is stopped.

Typing `/context-rotation:rotate release` is the only release the plugin itself offers. You can always also edit the settings file yourself.

## Maintainers

This section is for whoever publishes a release of the plugin. The plugin is developed in its source repository and exported, as one commit, into a local clone of the public toolkit repository. Nothing is pushed or tagged by the export.

### Export

Run the export script from the source repository, on a commit that is already merged into `main`:

```
scripts/maintenance/export_toolkit_plugin.sh \
  --commit <sha> \
  --target <path to the local toolkit clone> \
  --tag context-rotation-v<version>
```

The script exports only the tracked files of that commit, never the working tree, with the exact bytes and modes the commit holds. The result is one commit on the branch `export/context-rotation-<version>` in the clone. When that branch exists the commit goes on top of it. Otherwise the commit goes on the commit the clone's checkout is on. That commit is the base.

The script never checks out, writes into, resets or cleans the clone's own working tree or index. Staged, unstaged, untracked and ignored files there stay exactly as they are, and so does the branch the clone is on. The commit is built in a temporary worktree of the script's own, outside the clone, which is removed before the script exits. No hook of the clone runs, and no filter or `core.fileMode` setting of the clone changes a byte or a mode.

The commit changes three things against the base:

- the folder `context-rotation/`, with the source prefix stripped and files that no longer exist in the source removed (nothing outside that folder is deleted);
- the `context-rotation` entry in `.claude-plugin/marketplace.json`, derived from `context-rotation/.claude-plugin/plugin.json` (name, version, description) with the source `./context-rotation`. The file is created when it is missing. Every other entry the base has stays byte for byte;
- one row for `context-rotation/` in the `## Tools` table of the toolkit `README.md`, added once. Any other text the base has stays.

The commit message carries the source commit sha. Running the script again on the same commit and clone changes nothing and exits 0.

The branch is the only ref the script writes in the clone, and it writes it last: after the leak scan, as a compare-and-swap on the value it read at the start. It writes the branch itself, never the branch a symbolic ref points at, and the commit is written without a ref. The run counts the branch as its own only once its own write succeeded. A branch that moved during the run, for instance because another export got there first, is left as that writer left it. The run then fails, and the message says what the branch holds now.

### What it refuses

The script stops, with the branch untouched, when:

- the commit is not an ancestor of `origin/main` in the source repository;
- `refs/remotes/origin/main` in the source repository is a symbolic ref. The commit is checked against `origin/main` itself, not against the branch a symbolic ref points at;
- the plugin folder at that commit contains a symbolic link or a submodule (the plugin ships regular files only);
- the target is not the root of a git repository, or has no commit yet;
- the clone's checkout has a merge, rebase, cherry-pick, revert or bisect in progress. A new export branch starts on the commit that checkout is on, and during those operations that is a commit in passing;
- the export branch `export/context-rotation-<version>` is a symbolic ref. The message names the branch it points at, and the command that removes the symbolic ref and leaves that branch as it is;
- the export branch is checked out in the clone or in any other worktree, or a worktree is rebasing or bisecting it, also under a symbolic ref that points at it. The message names the worktree;
- the base tracks a symbolic link or a submodule at `context-rotation/` or anything under it, at `.claude-plugin/`, `.claude-plugin/marketplace.json` or `README.md`, or tracks `context-rotation` as a file;
- `.claude-plugin/marketplace.json` on the base is not valid JSON with a `plugins` list;
- `plugin.json` and `--tag` carry different versions;
- the content of `context-rotation/` changed but the version in `plugin.json` is not higher than the one the base already has. Content is every file with its bytes and its mode, so a mode-only change counts;
- `TMPDIR` points inside the clone.

Before the branch is written, the script runs the leak scan of `tests/test_isolation.py` over the blobs of the commit it built: the exported folder, the marketplace file and the README. When there is nothing to export, it scans what the branch already carries. A hit on a line of the marketplace file or the README that the base already had is marked pre-existing: the export did not bring it, but it still has to go before a publish. A hit inside `context-rotation/` is never marked that way, because that content always comes from the source commit.

### When it fails

Every exit that is not a successful export leaves the clone as it was found, apart from what another writer changed in the meantime. The temporary worktree is removed. If this run wrote the branch, it is moved back to where it was, or deleted when the run created it, as a compare-and-swap on the commit the run wrote, on the branch itself. A branch this run did not write, because another writer changed it first, is left alone. The clone's checkout was never touched, so there is nothing to restore there.

| Exit | Meaning |
|---|---|
| 0 | exported, or nothing to export |
| 1 | refused for any reason under "What it refuses", a symbolic export branch included, or failed; the clone is back as it was found |
| 2 | usage error |
| 3 | failed, and the clone could not be put back; the message lists what is left and the command that removes it |

### After the export

1. Review the branch: `git -C <clone> show --stat export/context-rotation-<version>`.
2. Push it: `git -C <clone> push -u origin export/context-rotation-<version>`.
3. Open and merge the pull request in the toolkit repository.
4. Tag the merge: `git tag context-rotation-v<version>` on the updated `main`, then `git push origin context-rotation-v<version>`.

The script is POSIX `sh` and needs `git` and `python3`. It makes no network calls.
