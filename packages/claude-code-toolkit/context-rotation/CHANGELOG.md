# Changelog

All notable changes to this plugin are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the plugin follows [Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-10-20

### Changed

- Documentation: the README names the absolute thresholds (`warn_tokens`, `force_tokens`, `hard_tokens`) next to `window_tokens`, says the bands are the percentages or the three token thresholds, warns that a user-scope install also reaches automated `claude -p` runs, notes that the user settings file is always `~/.claude/context-rotation.json` in the home directory, and records the 1.0.0 live smoke on Claude Code 2.1.303.

## [1.0.0] - 2026-10-19

### Added

- Context measurement from the session transcript: input, cache-read and cache-creation tokens of the last assistant message, the size Claude Code records after a compaction, and an unknown size that never warns or blocks.
- Three bands as a share of `window_tokens`: warn at 55%, force at 65%, hard at 75%. Force asks for the handover now and holds the turn back once. Hard denies every tool except reads, questions, the todo list and the handover write.
- The `rotate` skill with the actions `write`, `release` and `resume`, and a handover contract: goal, current state, what still runs, numbered next steps, files touched, open risks.
- Handover check after every Write or Edit, a ready-marker per session, and outdated markers when work follows the handover. An outdated handover is never injected.
- Injection of the checked handover into the fresh session after `/clear`, with an archive, an `injections.jsonl` log and recovery through `resume`.
- Automatic `/clear` inside tmux (`scripts/tmux_clear.sh`), on by default and switched off with `auto_clear: false`. It types nothing when a task still runs in the background, when the input line is not empty or when the pane is busy, and it reads back every typed line before Enter.
- A write guard on the plugin's own files: `.claude/rotation/` and the two settings files cannot be written by Claude's file tools.
- Settings in `.claude/context-rotation.json` (project) or `~/.claude/context-rotation.json` (user), an `enabled` switch per project and `CLAUDE_CONTEXT_ROTATION=off` per shell.
- A shell shim that keeps a missing or failing hook from blocking a session.
- `tests/live/smoke_tmux.sh`, a live smoke of the automatic clear in a throwaway tmux session, and `tests/live/install_test.sh`, an install test against an exported marketplace.
- Absolute thresholds `warn_tokens`, `force_tokens` and `hard_tokens`. When all three are set to positive integers in ascending order the bands come from these token counts, and the percentages and `window_tokens` no longer decide a band; when both the percentages and the tokens are configured and valid, the tokens win.
- A loop guard for a session that starts heavy from an injected handover. It records the start size and reaches the force and hard bands only after it has grown at least `min_work_tokens` past that start, so a rotation cannot loop. When the start is already so close to force that rotating will not make the next session smaller, the user is told once, with the two remedies.

### Known limits

- A session that continues from an injected handover after `/clear` and starts close to the force band is held below force and hard until it has done `min_work_tokens` of real work, and the user is told once when rotating will not make the next session smaller. The guard only covers that `/clear` injection: a fresh `startup` and a `/resume` are enforced as before, and the warn band is unchanged.
- Windows is not supported. The automatic clear needs tmux.
- The hook payloads were measured on Claude Code 2.1.300. The live smoke passed on 2.1.302.
