# T0 Orchestrator Output Template

## Output method

T0 dispatches through the single-entry door. It never prints a block to the terminal for a
tap to pick up, and it never writes a dispatch file by hand.

1. Stage a spec bundle in the central pending dir:
   `vnx dispatch stage --instruction <instruction-file> --dispatch-id <id> --role <role> --slot T0|T1|T2|T3 [--provider claude] [--model sonnet] [--track <track-id>]`
   (or `dispatch_bridge.stage_spec_bundle(...)`). It writes
   `<data_dir>/dispatches/pending/<dispatch-id>/{instruction.md,dispatch-spec.json}` and fires nothing.
2. Dry-run the door: `vnx dispatch <dispatch-id> --dry-run` prints the compiled plan (lane, model, route reason).
3. Fire: `vnx dispatch <dispatch-id>`.

The staging flow, the flags and the lane rules live in `docs/core/DISPATCH_RULES.md` (§12 for
staging). T0 never writes reports, only dispatches.

## Dispatch spec fields

| Field | Meaning |
|---|---|
| `--dispatch-id` | Unique id, `YYYYMMDD-HHMMSS-descriptor-slug` |
| `--role` | Skill name from `skills.yaml`. Drives worker spawn, permission profile and skill activation |
| `--slot` | `T0`-`T3`. Legacy terminal pin; the role-scoped ephemeral pool is the default |
| `--provider`, `--model` | Provider string and model; routing table in DISPATCH_RULES §8 |
| `--track` | Horizon track the dispatch belongs to |
| gate | A name registered in `scripts/lib/dispatch_spec.py` (`REGISTERED_GATE_NAMES`) |

The instruction file carries the concrete requirements and the success criteria. A worker
writes its report to `$VNX_DATA_DIR/unified_reports/<dispatch-id>.md` with the four headings of
the report contract (`## Summary`, `## Changes`, `## Verification`, `## Open Items`).

## Decision output

When analyzing receipts, print the decision as plain text before staging anything:

```
=== ORCHESTRATION DECISION ===
Status: WAIT|DISPATCH|ESCALATE|COMPLETE
Reason: <brief explanation>

Live work: live=0 starting=0 stale=0 unmeasured=0
Queue: pending=0 conflicts=0
================================
```

The live-work and queue lines come from `bash scripts/dispatch_guard.sh` (`vnx status --json --live`:
measured at the call, its `Measured:` line says when), not from memory of the last receipt or the last build.

### WAIT
Live work in the air, a pending queue or a degraded system:
```
=== ORCHESTRATION DECISION ===
Status: WAIT
Reason: one dispatch still running (authentication feature)

Live work: live=1 starting=0 stale=0 unmeasured=0
Queue: pending=2 conflicts=0
================================
```

### DISPATCH
Ready to create the next dispatch; stage it as above after the block:
```
=== ORCHESTRATION DECISION ===
Status: DISPATCH
Reason: nothing in the air, proceeding with the next priority task

Live work: live=0 starting=0 stale=0 unmeasured=0
Queue: pending=0 conflicts=0
================================
```

### ESCALATE
Intervention needed:
```
=== ORCHESTRATION DECISION ===
Status: ESCALATE
Reason: a stale dispatch for more than 30 minutes needs manual intervention

Live work: live=0 starting=0 stale=1 unmeasured=0
Queue: pending=5 conflicts=2
================================
```

### COMPLETE
Feature complete:
```
=== ORCHESTRATION DECISION ===
Status: COMPLETE
Reason: authentication feature fully implemented and tested

Live work: live=0 starting=0 stale=0 unmeasured=0
Queue: pending=0 conflicts=0
================================
```

## Common patterns

### After a success receipt
1. Analyze what was completed
2. Check if more work is needed
3. Progress to the next gate if applicable
4. Print DISPATCH or COMPLETE

### After a failure receipt
1. Analyze the failure reason
2. Determine if a retry is needed
3. Consider deep cognition
4. Print DISPATCH with higher priority

### After a blocked receipt
1. Analyze the blocker type
2. Check if other work can help
3. Consider escalation
4. Print ESCALATE or reassign

## Available skills reference

Check `skills.yaml` (shipped with the skills) for the current list. Common skills:
- `planner` - Feature planning and breakdown
- `architect` - System design and architecture
- `backend-developer` - Server-side implementation
- `frontend-developer` - UI/UX implementation
- `security-engineer` - Security audits and hardening
- `quality-engineer` - Comprehensive testing
- `test-engineer` - TDD and test creation
- `reviewer` - Code review and validation
- `debugger` - Problem investigation

## Output rules

1. **One dispatch at a time**: stage, dry-run, fire; never several at once.
2. **Through the door**: `vnx dispatch`, never a hand-written file or a raw lane script.
3. **Clear decisions**: always explain the orchestration reasoning.
4. **Governance metadata**: set the risk class, merge policy and review stack explicitly for any PR-backed dispatch; the default review stack is `VNX_DEFAULT_REVIEW_STACK` in `scripts/lib/config_registry.py`, so do not copy a literal here, it drifts.
