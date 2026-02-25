# VNX Context Rotation System - Technical Reference

**Status**: Active (Production)
**Last Updated**: 2026-02-24
**Owner**: T-MANAGER
**Version**: 2.5

---

## Overview

The VNX Context Rotation System automatically manages Claude Code context window limits across worker terminals (T1, T2, T3). When a terminal approaches its context limit, the system:

1. Detects the pressure via `PreToolUse` hook (fires before every tool call)
2. Blocks the tool call and instructs Claude to write a structured handover document
3. Force-stops Claude once the handover is written
4. Sends `/clear` via tmux to reset the context window
5. Injects a continuation prompt (with original skill re-activation) into the fresh session

This enables indefinite long-running dispatches across context boundaries without losing work state.

---

## Pipeline Diagram

```
  WORKER TERMINAL (T1 / T2 / T3)
  ─────────────────────────────────────────────────────────────────
  Claude executes any tool call
        │
        ▼
  ┌─────────────────────────────────────────────────────┐
  │  PreToolUse Hook: vnx_context_monitor.sh            │
  │  ┌──────────────────────────────────────────────┐   │
  │  │ Read context_window_{T}.json                 │   │
  │  │ used% = 100 - remaining_pct                  │   │
  │  │                                              │   │
  │  │  < 50%? → exit 0 (pass through)             │   │
  │  │  50-64%? → log warning, exit 0              │   │
  │  │  ≥ 65%? → ROTATION PATH                     │   │
  │  └──────────────────────────────────────────────┘   │
  └─────────────────────────────────────────────────────┘
        │
        │ [≥ 65% used]
        ▼
  ┌─────────────────────────────────────────────────────┐
  │  STAGE 1: Fresh handover exists? (written < 5 min)  │
  │  NO  → {"decision":"block"} + handover instructions │◄─ Claude writes
  │  YES → {"continue":false} → force-stop Claude        │   handover
  └─────────────────────────────────────────────────────┘
        │
        │ [Stage 2: continue:false]
        ▼
  ┌─────────────────────────────────────────────────────┐
  │  PostToolUse Hook: vnx_handover_detector.sh         │
  │  (fires on Write of *ROTATION-HANDOVER*.md)         │
  │  → Launches vnx_rotate.sh in background             │
  └─────────────────────────────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────────────────────────────┐
  │  vnx_rotate.sh (background process)                 │
  │  1. sleep 3  (allow continue:false to take effect)  │
  │  2. C-u → "/clear" (literal) → sleep 1 → Enter     │
  │  3. sleep 3  (Claude context reset)                 │
  │  4. Wait for signal file (max 15s)                  │
  │  5. Extract Dispatch-ID from handover               │
  │  6. Find dispatch file (active/ or completed/)      │
  │  7. Validate Role via validate_skill.py             │
  │  8. Send: C-u + /{skill} (send-keys literal)       │
  │  9. Paste continuation prompt (paste-buffer)        │
  │  10. Emit context_rotation_continuation receipt     │
  └─────────────────────────────────────────────────────┘
        │
        ▼
  ┌─────────────────────────────────────────────────────┐
  │  SessionStart Hook: vnx_rotation_recovery.sh        │
  │  (fires on /clear → new Claude Code session)        │
  │  → Writes signal file (rotation_clear_done_{T})     │
  │  → Unblocks vnx_rotate.sh wait loop                 │
  └─────────────────────────────────────────────────────┘
        │
        ▼
  Fresh Claude session starts with:
    /{skill}  [continuation prompt: dispatch + handover paths]
```

---

## Hook Architecture

### Hook Registration

All hooks are registered in `.claude/settings.json` and `.claude/settings.local.json`.

**Critical**: `settings.local.json` **completely replaces** `settings.json` for the `hooks` key — it does not merge. Both files must contain the full hook list. Terminal-level settings files (`terminals/T1/settings.json`, etc.) are NOT read by Claude Code for hook registration; they are only used as CLAUDE.md-style context.

```json
{
  "PreToolUse": [
    { "matcher": "*",   "command": "vnx_context_monitor.sh",  "timeout": 3000 },
    { "matcher": "Bash", "command": "activate_venv.sh" }
  ],
  "PostToolUse": [
    { "matcher": "Write", "command": "vnx_handover_detector.sh", "timeout": 3000 }
  ],
  "Stop": [
    { "command": "vnx_context_monitor.sh", "timeout": 3000 }
  ],
  "SessionStart": [
    { "matcher": "*", "command": "...terminal-specific recovery..." }
  ]
}
```

### Why PreToolUse (not Stop)

The `Stop` hook only fires when Claude becomes **completely idle** — i.e., after the agent finishes a full task and awaits user input. For long-running dispatches (15+ minutes, dozens of tool calls), Stop never fires during execution.

`PreToolUse` fires before **every tool call**, allowing the monitor to interrupt mid-task at any context level. This is the correct hook type for context pressure detection.

The `Stop` hook registration is retained as a safety net for idle sessions that accumulate context through conversational turns.

---

## Hook Details

### 1. vnx_context_monitor.sh (PreToolUse + Stop)

**Path**: `.claude/vnx-system/hooks/vnx_context_monitor.sh`

**Trigger**: Every tool call on T1, T2, T3

**Key behaviors**:

- Auto-enables: `export VNX_CONTEXT_ROTATION_ENABLED="${VNX_CONTEXT_ROTATION_ENABLED:-1}"` — handles env var propagation issues across settings layers
- **Loop prevention**: `Write`, `Read`, `Glob`, `Grep` pass through unconditionally so Claude can write the handover without being blocked
- Terminal detection via `vnx_detect_terminal()` — only T1/T2/T3 are monitored; T0 and T-MANAGER are excluded
- Reads `$VNX_STATE_DIR/context_window_{T}.json` for `remaining_pct`

**Thresholds**:

| Range | Action |
|-------|--------|
| < 50% used | Pass through (exit 0) |
| 50–64% used | Log warning + emit `context_pressure` receipt (phase=warning) |
| ≥ 65% used, no fresh handover | **Stage 1**: `{"decision":"block"}` + handover instructions |
| ≥ 65% used, fresh handover exists (< 5 min) | **Stage 2**: `{"continue":false}` |

**Why 65% threshold** (not 80%): Claude Code's built-in auto-compact fires at ~80%. To prevent the race condition where auto-compact beats rotation, the threshold is set 15 points lower, giving rotation time to write the handover and execute `/clear` before auto-compact engages.

**Stage 1 block message** provides:
- Timestamped filename: `YYYYMMDD-HHMMSS-{T}-ROTATION-HANDOVER.md`
- Destination directory: `$VNX_DATA_DIR/rotation_handovers/`
- Required document structure (see [Handover Format](#handover-format))

**Stage 2 force-stop**: Once a fresh handover is detected (mtime < 300s), returns `{"continue":false}` with a message. This cleanly terminates Claude so the `/clear` command can land in an empty input bar.

### 2. vnx_handover_detector.sh (PostToolUse:Write)

**Path**: `.claude/vnx-system/hooks/vnx_handover_detector.sh`

**Trigger**: After every `Write` tool call

**Function**: Detects when the written file path matches `*ROTATION-HANDOVER*`. When matched, launches `vnx_rotate.sh` in background with:
```bash
vnx_rotate.sh "$TERMINAL" "$HANDOVER_PATH"
```

The glob pattern matches both legacy format (`T3-ROTATION-HANDOVER.md`) and timestamped format (`20260223-181000-T3-ROTATION-HANDOVER.md`).

### 3. vnx_rotate.sh (Background executor)

**Path**: `.claude/vnx-system/hooks/vnx_rotate.sh`

**Trigger**: Launched by `vnx_handover_detector.sh` after handover is written

**Arguments**: `$1=TERMINAL`, `$2=HANDOVER_PATH`

**Guard**: Exits immediately if `VNX_CONTEXT_ROTATION_ENABLED != 1`

**Execution sequence**:

```
sleep 3                          # Allow continue:false to take effect first
C-u                              # Clear input buffer (safe; no mode side-effects)
sleep 0.3
send-keys -l "/clear"            # Literal text (prevents tmux key interpretation)
sleep 1                          # Let CLI render the command
send-keys Enter                  # Submit /clear
sleep 3                          # Post-clear reset (Claude needs ~3s)
Wait for signal file (max 15s)   # SessionStart writes rotation_clear_done_{T}
```

**Skill recovery**:
1. Extract `Dispatch-ID:` from handover via macOS-safe `sed`
2. Locate dispatch file: `active/{ID}*.md` or `completed/{ID}*.md`
3. Call `vnx_dispatch_extract_agent_role()` from `dispatch_metadata.sh`
4. Validate role with `validate_skill.py` — no role-to-skill mapping needed (role IS the skill name, already validated at original dispatch time)
5. Send `/{skill}` via `send-keys -l` (literal, hybrid dispatch pattern)
6. Paste continuation prompt via `paste-buffer`

**Fallback**: If Dispatch-ID missing or dispatch file not found, sends plain continuation prompt without skill prefix.

**Continuation prompt format**:
```
/{skill}  Continue dispatch {DISPATCH_ID}.

Read handover: {HANDOVER_PATH}
Read dispatch: {DISPATCH_FILE}

Resume from where the previous session left off. The handover doc contains
status, completed work, and remaining tasks.
```

Note: The prompt body starts with a space so it concatenates naturally after the skill name in the Claude Code input bar.

### 4. vnx_rotation_recovery.sh (SessionStart)

**Path**: `.claude/hooks/vnx_rotation_recovery.sh`

**Trigger**: New session start on T1, T2, T3

**Function**: Detects if rotation is in progress (by checking for fresh handover file). If found, writes the signal file `$VNX_STATE_DIR/rotation_clear_done_{T}` to unblock `vnx_rotate.sh`'s wait loop.

---

## Handover Format

The `vnx_context_monitor.sh` Stage 1 block message instructs Claude to produce:

```markdown
# {TERMINAL} Context Rotation Handover
**Timestamp**: {ISO-8601}
**Terminal**: {T1|T2|T3}
**Dispatch-ID**: {dispatch-id}
**Context Used**: {N}%

## Status
[complete | in-progress | blocked]

## Completed Work
- Bullet list of what was done

## Remaining Tasks
- Bullet list of what is left (or 'None')

## Files Modified
- file path: brief description

## Next Steps
[what the incoming session should do first]
```

**Filename convention**: `YYYYMMDD-HHMMSS-{TERMINAL}-ROTATION-HANDOVER.md`

**Storage**: `$VNX_DATA_DIR/rotation_handovers/`

The `Dispatch-ID` field is critical — `vnx_rotate.sh` parses it with `sed` to recover the original dispatch and re-activate the correct skill.

---

## Research Instrumentation

The rotation system emits two receipt types to the NDJSON pipeline for context-rot research.

### context_pressure

Emitted by `vnx_context_monitor.sh` at warning (≥50%) and rotation (≥65%) thresholds.

```json
{
  "event_type": "context_pressure",
  "terminal": "T2",
  "dispatch_id": "20260223-171110-653e5edf-C",
  "context_used_pct": 67,
  "context_remaining_pct": 33,
  "phase": "rotation",
  "timestamp": "2026-02-24T07:30:00Z"
}
```

`dispatch_id` is read from `terminal_state.json` (`terminals.{T}.claimed_by`).

### context_rotation_continuation

Emitted by `vnx_rotate.sh` after the continuation prompt is sent to the fresh session.

```json
{
  "event_type": "context_rotation_continuation",
  "terminal": "T2",
  "dispatch_id": "20260223-171110-653e5edf-C",
  "handover_path": "/path/to/handover.md",
  "skill": "backend-developer",
  "context_used_pct_at_rotation": 67,
  "timestamp": "2026-02-24T07:30:45Z"
}
```

### Context Rot Hypothesis

These receipts enable future correlation between:
- `context_used_pct` at rotation → quality/confidence of work delivered
- `dispatch_id` continuity → multi-session dispatch tracking (dispatch continues across rotations)
- Number of continuations per dispatch → degradation pattern analysis

Hypothesis: Terminals operating at higher context usage percentages produce lower confidence work — "context rot". The receipt chain (context_pressure → context_rotation_continuation → task_complete) will enable evidence-based validation.

---

## Settings Architecture

### Why settings.local.json is Authoritative

Claude Code merges `settings.json` + `settings.local.json` with the following behavior:
- **`env`**: Additive merge (local adds/overrides individual keys)
- **`hooks`**: **Full replacement** — if `hooks` key exists in local, it replaces the entire hooks object from project settings.json

A `settings.local.json` with `"hooks": {}` silently removes all hooks. This was the root cause of hooks not firing before this fix.

**Current policy**: Both files maintain the full hook list. `settings.local.json` is used for environment-specific overrides (e.g., absolute paths, local-only env vars).

### settings.local.json is Git-Ignored

`settings.local.json` is not committed to the repository. Any developer using the VNX system must populate it with the required hooks. See `core/12_PERMISSION_SETTINGS.md` for the canonical hook list.

### Terminal Settings Files (NOT Hook Sources)

Files like `.claude/terminals/T1/settings.json` are **not loaded by Claude Code for hook registration**. Claude Code only reads:
- `~/.claude/settings.json` (user global)
- `{project}/.claude/settings.json` (project)
- `{project}/.claude/settings.local.json` (local override)

Terminal settings files are read only as CLAUDE.md-style context documents. The `env` and `hooks` keys in those files have no effect on Claude Code behavior.

---

## tmux Key Sequence Design

The `/clear` sequence was aligned with the dispatcher's proven `prepare_terminal_mode()` pattern:

```bash
# DO: Safe input clear
tmux send-keys -t "$PANE_ID" C-u 2>/dev/null || true
sleep 0.3

# DO: Literal text (prevents tmux interpreting '/' as key sequence)
tmux send-keys -t "$PANE_ID" -l "/clear"
sleep 1

# DO: Enter as separate send-keys (not bundled with text)
tmux send-keys -t "$PANE_ID" Enter
sleep 3

# DON'T: Escape (can trigger mode changes in Claude Code)
# DON'T: C-c (kills the CLI process)
# DON'T: "/clear" Enter (bundled — tmux can race, Enter gets swallowed)
```

---

## Known Limitations

| Limitation | Impact | Status |
|------------|--------|--------|
| Signal file wait (15s max) | If SessionStart is slow, rotate proceeds with 5s fallback delay | Acceptable |
| Handover age window (5 min) | If rotation takes > 5 min end-to-end, Stage 2 may re-enter Stage 1 | Rare; log monitored |
| Auto-compact race (< 65%) | If context jumps 15%+ in a single tool call, auto-compact may beat rotation | Threshold tuning ongoing |
| Sessions started before settings change | Existing sessions don't reload hooks on settings change | Require /clear or restart |
| T0/T-MANAGER excluded | Only T1/T2/T3 monitored; T0 is read-only so rotation not needed | By design |
| No cross-rotation state merge | Each rotation creates a new session; long chains rely on handover quality | Claude writing quality |

---

## File Reference

| File | Purpose |
|------|---------|
| `.claude/vnx-system/hooks/vnx_context_monitor.sh` | Context pressure detector (PreToolUse + Stop) |
| `.claude/vnx-system/hooks/vnx_handover_detector.sh` | Handover written signal → launch rotate |
| `.claude/vnx-system/hooks/vnx_rotate.sh` | /clear + skill recovery + continuation prompt |
| `.claude/hooks/vnx_rotation_recovery.sh` | SessionStart: writes signal file for rotate wait loop |
| `.claude/vnx-system/scripts/validate_skill.py` | Validates role name is a registered skill |
| `.claude/vnx-system/scripts/lib/dispatch_metadata.sh` | `vnx_dispatch_extract_agent_role()` |
| `.claude/vnx-system/scripts/append_receipt.py` | NDJSON receipt emitter |
| `.vnx-data/state/context_window_{T}.json` | Live context window percentage (updated by Claude hook) |
| `.vnx-data/state/terminal_state.json` | Terminal claim state (provides dispatch_id for receipts) |
| `.vnx-data/rotation_handovers/` | Handover document storage |
| `.vnx-data/state/rotation_clear_done_{T}` | Signal file: SessionStart → vnx_rotate.sh unblock |
| `.vnx-data/logs/vnx_rotate_{T}.log` | Per-terminal rotation execution log |
| `.vnx-data/logs/hook_events.log` | Invocation trace for all context monitor calls |

---

## Related Documentation

- Architecture overview: `core/00_VNX_ARCHITECTURE.md`
- Hook integration report (v2.4 test evidence): `intelligence/VNX_HOOK_INTEGRATION_REPORT.md`
- Rotation test report (v2.4): `intelligence/VNX_ROTATION_TEST_REPORT.md`
- Original rotation plan: `CONTEXT_ROTATION.md` (project root, legacy spec)
- Receipt format: `core/11_RECEIPT_FORMAT.md`
- Permission settings: `core/12_PERMISSION_SETTINGS.md`
