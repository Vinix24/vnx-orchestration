# Zero-Touch Context Rotation for Claude Code

**Status**: Experimental (opt-in) · **Version**: 2.5 · **Live validated**: 2026-02-23

Claude Code sessions degrade when the context window fills up. Auto-compaction loses nuance, long sessions drift, and there's no native way to clear and resume from a hook. Anthropic closed the feature request for hook-based `/clear` as [NOT_PLANNED](https://github.com/anthropics/claude-code/issues/9118).

This system solves it with a fully automated pipeline: detect pressure → block → write handover → clear via tmux → inject handover into fresh session → resume. Zero human intervention.

## The Problem

Long-running Claude Code sessions hit a wall. At ~65% context usage:

- Auto-compaction silently drops context, causing subtle regressions
- The agent loses track of architectural decisions made earlier in the session
- Multi-step tasks fail because intermediate state is gone
- You don't notice until the output is wrong

The only clean fix is `/clear` — but that wipes everything. You lose all context, all progress, all momentum. And you can't trigger `/clear` from a hook ([#9118](https://github.com/anthropics/claude-code/issues/9118) — NOT_PLANNED).

## How It Works

```
Context hits 65% used
  → PreToolUse hook fires, returns decision: "block"
    → Claude writes ROTATION-HANDOVER.md (task state, progress, next steps)
      → PostToolUse hook detects handover file
        → Acquires atomic lock (prevents race conditions)
          → Launches vnx_rotate.sh async (nohup)
            → tmux sends /clear to the correct pane
              → SessionStart hook detects source: "clear"
                → Injects handover into fresh session
                  → Agent resumes where it left off
```

Three hooks, one bash script, zero manual steps.

### The Hook Chain

| Hook | Script | Trigger | Action |
|------|--------|---------|--------|
| **PreToolUse** | `vnx_context_monitor.sh` | Every tool call | Checks `remaining_pct`. At ≥65% used → `decision: "block"` with instruction to write handover |
| **PostToolUse** | `vnx_handover_detector.sh` | File write | Detects `ROTATION-HANDOVER` in path → acquires lock → launches rotator |
| **SessionStart** | `vnx_rotation_recovery.sh` | Session clear/compact | Finds most recent handover (<300s old) → injects as context |

The `Stop` hook is also registered as a safety net for idle sessions. The primary detection path is `PreToolUse` — it fires before every tool call, allowing interruption mid-task. `Stop` only fires when the agent is fully idle.

### The Rotator (`vnx_rotate.sh`)

Runs detached (`nohup`) after PostToolUse triggers it:

1. Resolves the correct tmux pane for the terminal (T1/T2/T3)
2. Sends `/clear` via `tmux send-keys`
3. Waits for SessionStart signal file (15s timeout, 5s fallback)
4. Extracts `Dispatch-ID` from the handover document (supports plain and markdown bold format)
5. Locates the original dispatch file and recovers the agent skill (`/{skill}`)
6. Injects continuation prompt via `tmux load-buffer` + `paste-buffer`
7. Updates terminal state to `working | dispatch-id` so the orchestrator sees correct status
8. Releases lock, cleans up

### The Handover Document

Claude writes a structured markdown file before rotation:

```markdown
# T1 Context Rotation Handover

**Dispatch-ID**: pdf-assembler-split
**Context Used**: 67%

## Completed Work
- Scanned 7/11 SME targets (14 reports generated)
- Fixed browser pool memory leak (PR-3)

## Remaining Tasks
- Scan remaining 4 targets
- Generate comparison matrix

## Next Steps for Incoming Context
1. Continue SME scan batch from target #8
2. Server running on PID 42891, port 8077
```

This is the handover contract. The incoming session gets exactly enough context to continue — not a full transcript dump, but a structured task state.

## What Makes This Different

Five projects attempt parts of this problem. None solve the full loop:

| | Auto-detect | tmux /clear | Handover inject | Zero-touch |
|---|---|---|---|---|
| **VNX Context Rotation** | ✅ PreToolUse hook | ✅ async nohup | ✅ SessionStart | ✅ |
| [claude-code-handoff](https://github.com/Sonovore/claude-code-handoff) | — | — | ✅ | — |
| [claude-session-restore](https://github.com/ZENG3LD/claude-session-restore) | — | — | ✅ | — |
| [claude_code_agent_farm](https://github.com/Dicklesworthstone/claude_code_agent_farm) | Semi | ✅ | — | — |
| [/wipe gist](https://gist.github.com/GGPrompts/62bbf077596dc47d9f424276575007a1) | — | ✅ | ✅ | — |
| [claude-code-context-sync](https://github.com/Claudate/claude-code-context-sync) | — | — | ✅ | — |

The closest is the `/wipe` gist — it uses the same `tmux send-keys` + `load-buffer` pattern. But it's manually triggered and has no context pressure detection.

The `agent_farm` project has multi-agent tmux orchestration with a `--context-threshold` flag, but agents restart from their task queue after clearing — no handover document, no session continuity.

## Concurrency & Safety

**Atomic locking**: `mkdir`-based locks with timestamp-based stale detection (TTL=300s). Prevents race conditions when multiple terminals rotate simultaneously.

**Loop prevention**: The PreToolUse hook passes `Write`, `Read`, `Glob`, and `Grep` through unconditionally so Claude can write the handover without being blocked by its own rotation trigger.

**Multi-terminal awareness**: Each terminal (T1/T2/T3) has independent pane resolution, lock files, and handover documents. T0 (orchestrator) is excluded from rotation.

**Fallback chain**: SessionStart recovery supports a `--fallback` parameter so worker terminals don't get disrupted by rotation recovery when bootstrapping fresh tasks.

**Why 65% (not 80%)**: Claude Code's built-in auto-compact fires at ~80%. Rotating 15 points earlier gives the pipeline time to write the handover and execute `/clear` before auto-compact can race and win.

## Setup

### Prerequisites

- Claude Code with hooks support
- tmux (sessions must run inside tmux panes)
- Bash 4+

### Enable

Context rotation is **opt-in**. Set the environment variable:

```bash
export VNX_CONTEXT_ROTATION_ENABLED=1
```

Or add to your `.bashrc` / `.zshrc`.

### Hook Registration

Add to `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [{
          "type": "command",
          "command": ".claude/vnx-system/hooks/vnx_context_monitor.sh"
        }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [{
          "type": "command",
          "command": ".claude/vnx-system/hooks/vnx_handover_detector.sh"
        }]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [{
          "type": "command",
          "command": ".claude/vnx-system/hooks/vnx_context_monitor.sh"
        }]
      }
    ],
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [{
          "type": "command",
          "command": ".claude/hooks/vnx_rotation_recovery.sh"
        }]
      }
    ]
  }
}
```

### File Structure

```
.claude/
├── hooks/
│   └── vnx_rotation_recovery.sh        # SessionStart recovery
├── vnx-system/
│   ├── hooks/
│   │   ├── vnx_context_monitor.sh       # PreToolUse + Stop (pressure detection)
│   │   ├── vnx_handover_detector.sh     # PostToolUse (handover trigger)
│   │   ├── vnx_rotate.sh               # tmux rotator (async)
│   │   └── lib/
│   │       └── _vnx_hook_common.sh      # Shared utilities
│   └── scripts/
│       ├── append_receipt.py            # Receipt logging
│       ├── pane_config.sh               # Pane ID resolution
│       ├── terminal_state_shadow.py     # Terminal state updater
│       └── lib/
│           └── vnx_paths.sh             # Path configuration
.vnx-data/
├── rotation_handovers/                  # Handover documents
├── logs/                                # Rotation execution logs
└── state/
    ├── context_window_{TERMINAL}.json   # Context % tracking
    ├── terminal_state.json              # Terminal claim state (for orchestrator)
    └── panes.json                       # Runtime pane mapping
```

## Known Limitations

1. **tmux timing race**: There's a small window (~1-2s) after `/clear` where the terminal may not be ready for input. Mitigated with a settle delay + signal file, but not 100% deterministic.

2. **Handover quality depends on Claude**: The handover document is written by Claude under context pressure. Quality varies. The structured format (completed/remaining/next steps) constrains it enough to be useful.

3. **No native Anthropic support**: This entire system exists because `/clear` can't be called from hooks ([#9118](https://github.com/anthropics/claude-code/issues/9118)). If Anthropic adds native support, the tmux workaround becomes unnecessary.

4. **Skill context lost on /clear**: Active skills are not preserved across rotation. The rotator re-activates the original skill via `/{skill}` prefix on the continuation prompt, recovered from the original dispatch file.

## Evidence

Live validation completed 2026-02-23 on T1 and T3:

- PreToolUse hook correctly blocks at ≥65% context usage
- PostToolUse detector triggers on handover file writes
- Atomic lock acquisition prevents double-rotation
- tmux `/clear` + continuation prompt injection works end-to-end
- SessionStart recovery injects handover into fresh session
- Receipt audit trail logs every rotation event (`context_pressure` + `context_rotation_continuation`)

Evidence bundle: `.claude/vnx-system/docs/intelligence/evidence/context-rotation-live-20260223-163501/`

Test report: `.claude/vnx-system/docs/intelligence/VNX_ROTATION_TEST_REPORT.md`

## Changelog

### v2.5 (2026-02-24)
- **Fix**: Dispatch-ID regex now uses `[^:]*` to absorb optional markdown bold (`**`) before the colon — handles both `Dispatch-ID: foo` and `**Dispatch-ID**: foo` formats
- **Feature**: Terminal state updated to `working | dispatch-id` after rotation so orchestrator (T0) sees correct status via `track-status`

### v2.4 (2026-02-23)
- Initial live-validated release
- Three-hook pipeline (PreToolUse → PostToolUse → SessionStart)
- Skill recovery from original dispatch file
- NDJSON receipt emission for context-rot research

## Anthropic Issues (Related)

| Issue | Title | Status |
|-------|-------|--------|
| [#9118](https://github.com/anthropics/claude-code/issues/9118) | Ability to /clear from hook scripts | **NOT_PLANNED** |
| [#3314](https://github.com/anthropics/claude-code/issues/3314) | Context Window Reset Without Session Restart | Open |
| [#11455](https://github.com/anthropics/claude-code/issues/11455) | Session Handoff / Continuity Support | Open |
| [#3656](https://github.com/anthropics/claude-code/issues/3656) | Restore Blocking Stop Command Hooks | Open |
| [#3046](https://github.com/anthropics/claude-code/issues/3046) | /clear causes transcript issues breaking Stop hooks | Open |

## License

Part of the VNX Orchestration System. MIT License.
