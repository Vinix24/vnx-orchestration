# FP-C Mixed Execution Cutover Guide

**Feature**: FP-C — Execution Modes, Headless Routing, And Intelligence Quality
**PR**: PR-5
**Status**: Certified
**Purpose**: Operator guide for mixed execution routing cutover, rollback, and intelligence review.

---

## Overview

FP-C introduces mixed execution routing: dispatches can now route to either interactive tmux workers or headless CLI workers based on task class. Coding work stays interactive by default. Non-coding work (research, documentation) may route headless when enabled.

## Feature Flags

| Flag | Default | Description |
|------|---------|-------------|
| `VNX_MIXED_EXECUTION` | `0` | Master switch. `0` = all-interactive (pre-FPC behavior). `1` = mixed routing active. |
| `VNX_HEADLESS_ROUTING` | `0` | Headless target selection. `0` = headless targets never selected. `1` = eligible task classes may route headless. |
| `VNX_HEADLESS_ENABLED` | `0` | Headless subprocess execution. `0` = headless adapter disabled. `1` = CLI subprocess execution active. |
| `VNX_BROKER_SHADOW` | `1` | Broker mode. `1` = shadow (registers but does not replace tmux delivery). `0` = authoritative. |
| `VNX_INTELLIGENCE_INJECTION` | `1` | Intelligence injection. `1` = bounded injection at create/resume. `0` = disabled. |
| `VNX_HEADLESS_TIMEOUT` | `600` | Headless subprocess timeout in seconds. |
| `VNX_HEADLESS_CLI` | `claude` | CLI binary for headless execution. |

## Cutover Sequence

### Phase 1: Shadow Validation (recommended)

```bash
export VNX_MIXED_EXECUTION=1
export VNX_HEADLESS_ROUTING=1
export VNX_HEADLESS_ENABLED=0
export VNX_BROKER_SHADOW=1
```

In this phase, routing decisions are made and logged but headless execution does not occur. All dispatches still flow through tmux. Review routing_decision events in coordination_events to verify correct task class resolution.

### Phase 2: Headless Execution

```bash
export VNX_HEADLESS_ENABLED=1
```

Now headless-eligible dispatches (research_structured, docs_synthesis) will execute via CLI subprocess. Monitor headless_execution_completed and headless_execution_failed events.

### Phase 3: Full Cutover

```bash
export VNX_BROKER_SHADOW=0
```

The broker becomes the authoritative dispatch registration path.

## Rollback

Set any of the following to revert:

```bash
# Full rollback to pre-FPC behavior
export VNX_MIXED_EXECUTION=0

# Disable only headless routing (keep intelligence injection)
export VNX_HEADLESS_ROUTING=0

# Disable only headless execution (keep routing decisions)
export VNX_HEADLESS_ENABLED=0
```

No data migration is needed. Routing decisions are stateless — the next dispatch uses current flag values. In-flight headless processes will complete or timeout naturally.

## Task Class Routing

| Task Class | Default Target | Headless Eligible | Example Skills |
|------------|---------------|-------------------|----------------|
| `coding_interactive` | interactive_tmux | No (G-R2) | backend-developer, frontend-developer, debugger |
| `research_structured` | interactive_tmux | Yes | architect, reviewer, planner, security-engineer |
| `docs_synthesis` | interactive_tmux | Yes | excel-reporter, technical-writer |
| `ops_watchdog` | interactive_tmux | No (prefers interactive) | monitoring-specialist |
| `channel_response` | channel_adapter | No (must enter via inbox) | inbound events |

## Intelligence Review

Every dispatch shows its intelligence payload in bundle.json:

```json
{
  "intelligence_payload": {
    "injection_point": "dispatch_create",
    "injected_at": "2026-03-29T16:00:00.000000Z",
    "items": [
      {
        "item_class": "proven_pattern",
        "title": "...",
        "confidence": 0.8,
        "evidence_count": 3,
        "scope_tags": ["architect", "Track-C"]
      }
    ],
    "suppressed": [
      {"item_class": "recent_comparable", "reason": "no candidates available"}
    ]
  }
}
```

Maximum 3 items per injection. Each item carries confidence, evidence_count, last_seen, and scope_tags.

## Execution Target Selection

View registered targets:

```python
from execution_target_registry import ExecutionTargetRegistry
registry = ExecutionTargetRegistry(state_dir)
for target in registry.list_by_health("healthy"):
    print(f"{target.target_id}: {target.target_type} ({target.capabilities})")
```

T0 can override routing via target_id metadata in dispatch. The override respects R-5 (capability) and R-6 (health) invariants.

## Monitoring

Key coordination events to watch:

| Event Type | Meaning |
|------------|---------|
| `routing_decision` | Target selected for dispatch |
| `mixed_routing_decision` | Mixed router made a routing + execution decision |
| `intelligence_injection` | Intelligence items injected into dispatch |
| `intelligence_suppression` | No items met threshold |
| `headless_subprocess_start` | CLI subprocess launched |
| `headless_execution_completed` | Headless dispatch succeeded |
| `headless_execution_failed` | Headless dispatch failed |
| `recommendation_proposed` | New advisory recommendation |
| `recommendation_accepted` | Operator accepted recommendation |

## Certification

Run the FP-C certification runner:

```bash
cd scripts/lib
python fpc_certification.py --output /tmp/fpc_cert.json
```

The report maps every certification matrix row to pass/fail/skip with evidence.

## Residual Risks

1. Task class boundaries may need refinement after real-world headless routing
2. Headless CLI execution may have different failure modes than tmux delivery
3. Intelligence confidence thresholds may need tuning after measurement data accumulates
4. Recommendation measurement windows may be too short for low-volume dispatches
5. Channel adapter reliability is untested in production
6. Cutover rollback should include graceful shutdown for in-flight headless processes

## Governance Compliance

| Rule | Status | Evidence |
|------|--------|----------|
| G-R1: Routing explicit and reviewable | Pass | routing_decision events with full metadata |
| G-R2: Coding stays interactive | Pass | R-1 invariant enforced in DispatchRouter |
| G-R3: Headless is durable and receipt-producing | Pass | HeadlessAdapter records attempts and transitions |
| G-R4: Channel events become dispatches first | Pass | InboundInbox.receive() before any routing |
| G-R5: Intelligence bounded to 3 items | Pass | MAX_ITEMS_PER_INJECTION=3 enforced in selector |
| G-R6: Evidence metadata on every item | Pass | IntelligenceItem schema includes confidence, evidence_count, last_seen, scope_tags |
| G-R7: Recommendations advisory-only | Pass | RecommendationTracker.export_usefulness_report() sets advisory_only=True |
| G-R8: No bypass of T0 authority | Pass | All routing emits coordination_events |
