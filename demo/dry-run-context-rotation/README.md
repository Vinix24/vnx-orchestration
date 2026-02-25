# Context Rotation — Dry-Run Demo

A visual replay of the VNX context rotation lifecycle, showing how a terminal agent seamlessly hands over its session when its context window fills up and resumes in a fresh session — with zero human intervention and full receipt traceability.

## What This Demonstrates

When an agent (T1, T2, or T3) approaches context exhaustion, VNX intercepts the next tool call, instructs the agent to write a structured handover document, then automatically clears and restarts the session with the same dispatch, same skill, and a fresh context window. The entire sequence is governed by three hook scripts and produces an auditable receipt chain.

## Quick Start

```bash
bash replay.sh          # Normal speed (2s pauses)
bash replay.sh --fast   # Accelerated (0.5s pauses)
```

## Context Rotation Lifecycle

```
PreToolUse hook (vnx_context_monitor.sh)
  ├── 50% used  → WARNING logged, tool allowed
  ├── 65% used  → ROTATION triggered
  │     ├── Stage 1: block tool, instruct handover write
  │     └── Stage 2: block all tools (handover already exists)
  │
  └── Write/Read/Glob/Grep always allowed through

PostToolUse hook (vnx_handover_detector.sh)
  └── Write tool + filename contains "ROTATION-HANDOVER"
        ├── Acquire rotation lock
        ├── Emit context_rotation receipt
        ├── Launch vnx_rotate.sh (nohup background)
        └── Return {"continue":false} — agent stops

vnx_rotate.sh (background)
  ├── Sleep 3s (allow Claude to fully stop)
  ├── Send /clear to terminal pane via tmux
  ├── Wait for clear completion signal file
  ├── Extract dispatch ID from handover
  ├── Look up original dispatch file
  ├── Validate skill (backend-developer, etc.)
  ├── Send /{skill} + continuation prompt via tmux paste-buffer
  ├── Update terminal state → working
  └── Emit context_rotation_continuation receipt
```

## Demo Scenario

- **Project**: LeadFlow (AI-powered lead scoring platform)
- **Terminal**: T1 (Codex CLI, Sonnet)
- **Dispatch**: PR-2 — Lead Scoring Engine
- **Dispatch ID**: `20260221-165539-lead-scoring-engine-A`
- **Rotation at**: 66% context used (16:18)
- **Recovery time**: ~12 seconds
- **Final outcome**: PR-2 completed across 2 sessions, 49 tests passing

## Phases

| Phase | Description |
|-------|-------------|
| 1 | Setup — active dispatch and terminal state at 16:10 |
| 2 | Context filling up — tool calls at 48%, 52%, 66% |
| 3 | Rotation triggered — block, handover write, detector, agent stop |
| 4 | Session recovery — vnx_rotate.sh clear + skill + continuation |
| 5 | Resumed session — T1 resumes with 8% context, completes work |
| 6 | Task completion — quality advisory, receipt with rotation metadata |
| 7 | Summary — stats, receipt chain, governance integrity |

## Evidence Files

| File | Description |
|------|-------------|
| `evidence/context_pressure_events.ndjson` | 3 pressure events at 48% (normal), 52% (warning), 66% (rotation) |
| `evidence/rotation_receipt.json` | `context_rotation` receipt emitted by handover detector |
| `evidence/continuation_receipt.json` | `context_rotation_continuation` receipt emitted by vnx_rotate.sh |
| `evidence/handover.md` | Handover document written by T1 at rotation |
| `evidence/completion_receipt.json` | `task_complete` receipt with `context_rotation_metadata` block |

## Hook Files (Production)

The production hooks demonstrated in this replay:

- `.claude/vnx-system/hooks/vnx_context_monitor.sh` — PreToolUse
- `.claude/vnx-system/hooks/vnx_handover_detector.sh` — PostToolUse
- `.claude/vnx-system/hooks/vnx_rotate.sh` — Background rotation script

## Context Window State Format

The hooks read per-terminal state files at:
```
.vnx-data/state/context_window_T{n}.json
{"remaining_pct": 34, "ts": 1772018577}
```

The monitor computes `used_pct = 100 - remaining_pct` and checks against:
- `WARNING_THRESHOLD = 50`
- `ROTATION_THRESHOLD = 65`
