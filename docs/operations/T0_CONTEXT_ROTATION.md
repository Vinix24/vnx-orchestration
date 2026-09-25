# T0 context rotation at 500K — enforcement and handoff contract

> **Operator decision 2026-09-25.** At 500K context a T0 orchestrator MUST rotate: a new
> session takes over from the current one through a handoff, starts with the official kickoff,
> and closes the old tmux window. After 500K, work in flight may be finished, but nothing new
> is started. A `/goal` that was running continues in the new session.
>
> **Scope: T0 only.** Workers (T1-T3, dispatch worktrees, headless `claude -p`) and non-VNX
> sessions are never touched. The machine-written rotation handoff (`handoff.md` under the
> rotation dir, `vnx handoff`) is a different contract: see `CONTEXT_ROTATION.md`.

## The three pieces

| Piece | File | Role |
|---|---|---|
| Measurement | `scripts/lib/t0_context_budget.py` | context size from the transcript, rotation band |
| Guard hook | `scripts/hooks/t0_context_guard.py` | UserPromptSubmit / PreToolUse / Stop enforcement |
| Spawner | `scripts/t0_rotate_spawn.sh` | opens the successor window, types the kickoff, resumes the `/goal` |
| Shared state | `scripts/lib/t0_rotation_state.py` | pending marker, latch, handoff parser, receipts |

The rotate skill (personal, `~/.claude/skills/rotate/SKILL.md`) runs `/build-log wrap` first and
then calls the spawner. The PreCompact reminder in the personal settings stays as it is; with a
1M window it only fires around 800K, which is why the guard exists.

## Measurement

The last `type: assistant` line in the session transcript (`transcript_path` in every hook
payload) carries `message.usage`. The context the model saw on that request is

```
input_tokens + cache_read_input_tokens + cache_creation_input_tokens
```

Sidechain entries (`isSidechain: true`) and harness-written `<synthetic>` messages are skipped.
An empty, unreadable or usage-less transcript measures as *unknown*, and unknown never blocks
anything. CLI: `python3 scripts/lib/t0_context_budget.py <transcript>` prints size, band and
thresholds as JSON.

Thresholds live in the config registry (`scripts/lib/config_registry.py`, subsystem
`t0-context-rotation`), so they follow the usual precedence (`VNX_OVERRIDE_*`, project config,
env, default):

| Key | Default | Band |
|---|---|---|
| `VNX_T0_ROTATE_WARN_TOKENS` | 400000 | warn |
| `VNX_T0_ROTATE_FORCE_TOKENS` | 500000 | force: rotation pending |
| `VNX_T0_ROTATE_HARD_TOKENS` | 600000 | hard: rotate now |

## What the guard does per band

| Band | UserPromptSubmit | PreToolUse | Stop |
|---|---|---|---|
| below warn | nothing | nothing | nothing |
| warn | adds context: size, "wind down and prepare the rotation" | nothing | nothing |
| force | adds context; refuses a prompt that starts a new `/goal` | denies starting new work (list below) | blocks the end of the turn once: finish to a boundary, start nothing, then rotate |
| hard | as force | as force | blocks once: rotate now, even with work in flight |

**Starting new work** (denied at force and above), and why each is on the list:

| Action | Why |
|---|---|
| `vnx dispatch <id>` without `--dry-run`, incl. `vnx dispatch stage` and `vnx dispatch-agent` | fires or stages a new worker |
| `dispatch_bridge.py` / a python call to `stage_spec_bundle`, `bridge_dispatch`, `deliver_via_door` | stages a new dispatch in the pending dir |
| `gh pr create` | opens a PR that needs a gate round and a merge this session will not finish |
| a new `/goal` (Skill or SlashCommand tool, or a typed `/goal` prompt) | starts a new autonomous run; a running `/goal` goes along through the handoff |

Everything else stays allowed, because finishing is allowed: `pr_merge.py`, gates, waiting on
CI, closing open items, reading, `vnx dispatch --dry-run`, and the rotation itself. The
UserPromptSubmit refusal of a typed `/goal` only works if Claude Code runs UserPromptSubmit
hooks for that native command; that was not measured.

**Loop protection.** The Stop block fires at most once per turn: on the stop that follows a
Stop-hook continuation Claude Code sets `stop_hook_active`, and the guard lets that one through.
Once the spawner started the successor it writes a latch for the session (and for the tmux
pane); a latched session is not Stop-blocked again. A latch older than 30 minutes no longer
counts: an old session still alive by then means the successor died during boot.

**Who is guarded.** `session_stop_rotation._is_t0_session` (explicit `VNX_TERMINAL`, else
no `VNX_DISPATCH_ID`/`VNX_TMUX_SIGNAL_DIR`, else a cwd outside `.claude/terminals/T1-T3`) plus
a cwd outside `.vnx-data/worktrees/`. Anything else is a silent no-op.

**Registration.** Fabric: `.claude/settings.json`. Consumers: `templates/settings_vnx_keys.json.tmpl`
(what `vnx regen-settings --merge`, and so `vnx init` / `bootstrap_hooks`, writes) and the
`vnx init` templates `templates/init/{default,minimal}/settings.json.j2`. The PreToolUse entry
matches `Bash|Skill|SlashCommand` only.

## Handoff contract

The spawner reads `daily-log/handoff.md` (default; `--handoff` overrides). `/build-log wrap`
must write:

1. **Next steps, numbered**, under a heading that starts with `## Next steps` or
   `## Volgende stappen`. The first numbered item (continuation lines included) is what the
   successor is told to pick up, literally. A handoff without a numbered step is refused.
2. **`## Actief /goal`**, only when a `/goal` was running:

   ```markdown
   ## Actief /goal
   Directive: <the /goal directive, with or without the leading /goal>
   Resterend:
   - <remaining task>
   - <remaining task>
   ```

   `**Directive**:` and `**Resterend**: a; b` (semicolon-separated on one line) are accepted
   too, as is a bare `/goal ...` line as directive. A section that says `geen`, is empty, or
   has no directive counts as no goal.

The handoff must be fresh: older than 15 minutes (`--max-age-minutes`) and the spawner refuses.
Handing over a stale handoff is the worst case, because the successor would resume the wrong
state with full confidence.

## What the spawner types

1. In a new window next to the old one, with the old window's name:
   `claude --model opus`, plus `--remote-control "<name>"` when `CLAUDE_CODE_BRIDGE_SESSION_ID`
   is set (`--rc` / `--no-rc` override), prefixed with `CLAUDE_CONFIG_DIR=...` when that is set
   (tmux does not carry a bare export into a new pane).
2. After the footer (`auto mode`) shows, one line of natural language: close the old window
   (`tmux kill-window -t <old>`), run the kickoff skill on the handoff, then take up step 1:
   `<first next step>`. Natural language, because a leading `/` opens the slash autocomplete.
3. The latch and a `state_mutation` receipt (`t0_context_rotation_started`).
4. With an active goal: `/goal <directive>, hervat na context-rotatie; resterend: <tasks>`
   (shortened to stay within the 4000-character `/goal` limit).

Enter is always its own `send-keys` call. Every line is collapsed to one line, since a newline
inside `send-keys -l` would submit early.

**Why the `/goal` step runs on the tmux server.** The successor kills the old window in its
first turn, and with it the spawner, which runs as a tool call inside that window. The follow-up
is handed to `tmux run-shell -b`, which runs on the tmux server and survives the kill. It waits
until the first turn is over (busy marker `esc to interrupt` seen and then gone twice in a row
with the footer present), types the `/goal` line, and waits for `/goal active` on screen.

**How the input box takes `/goal`: not measured live.** This dispatch was not allowed to start
a real claude or touch a real tmux session, so the sequence is designed, not observed:

- The line is typed literally (`send-keys -l`), then Enter separately.
- No Escape: in the Claude Code input box a double Escape clears the input or opens rewind.
- If `/goal active` does not show within 20 s: when the typed text is still visible in the
  pane, the first Enter was taken by the autocomplete and one more Enter is sent; otherwise the
  line is typed again. Then one more check.
- Still not active: a tmux `display-message` in the new window plus a natural-language message
  to the successor with the exact `/goal` line to type, and a receipt with
  `outcome=unconfirmed`. The markers are configurable (`VNX_T0_ROTATE_FOOTER_PATTERN`,
  `VNX_T0_ROTATE_BUSY_PATTERN`, `VNX_T0_ROTATE_GOAL_ACTIVE_PATTERN`); set the real footer text
  there after the first live rotation if it differs.

Every run leaves `prompt.txt`, `first_step.txt`, `goal.txt` and `goal_followup.log` in
`~/.vnx-data/<project>/state/t0_rotation/rotation-<timestamp>-<pid>/`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | successor started (and the goal follow-up handed to the tmux server, if any) |
| 2 | usage error |
| 3 | not in tmux, project root unresolvable, handoff missing or stale |
| 4 | handoff has no numbered next step |
| 5 | the successor never showed its footer; nothing was typed, the old window keeps running |

## Receipts

All `state_mutation`, `source: t0_context_rotation`:

| Trigger | When |
|---|---|
| `t0_context_rotation_pending` | a T0 session first crosses force, and again when it crosses hard |
| `t0_context_rotation_started` | the successor window is up and the kickoff was typed |
| `t0_context_rotation_goal_followup` | the `/goal` follow-up ended: `outcome` = `confirmed`, `unconfirmed` or `turn_timeout` |

## The retired worker monitor

`hooks/vnx_context_monitor.sh` read `context_window_T{1,2,3}.json`, which nothing writes any
more. Its dead branch is gone rather than rewired to the new measurement, because rotation is
T0-only by operator decision. The file remains as a silent no-op because SEOcrawler_v2 still
pins it; a deleted hook target would print a hook error on every tool call there.
