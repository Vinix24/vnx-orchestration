# FP-D Escalation Model — States, Transitions, And Override Semantics

**Feature**: FP-D — Safe Autonomy, Governance Envelopes, And End-To-End Provenance
**PR**: PR-0
**Status**: Canonical
**Purpose**: Defines the escalation state machine that governs how actions move from informational to operator-blocked. Escalation states are orthogonal to dispatch states — a dispatch can be `running` while its escalation level is `hold`.

---

## 1. Escalation States

Escalation states track the governance attention level for an action or entity. They are not dispatch states; they overlay the existing dispatch/lease state machines.

| State | Description | Operator Action Required | Automatic Exit |
|---|---|---|---|
| `info` | Informational event logged. No operator action needed. Normal operations. | No | N/A (default state) |
| `review_required` | Event flagged for operator review. Action may proceed but operator should inspect. | Recommended | Timeout to `hold` if unreviewed after threshold |
| `hold` | Action paused pending operator decision. No further automatic progress until released. | Yes | No automatic exit — operator must release or escalate |
| `escalate` | Action requires immediate T0 intervention. System cannot proceed without governance decision. | Yes (T0 specifically) | No automatic exit — T0 must resolve |

### 1.1 State Invariants

1. **Exclusive**: An entity has exactly one escalation level at any time. Default is `info`.
2. **Monotonically increasing severity**: Automatic transitions only move toward higher severity (`info` -> `review_required` -> `hold` -> `escalate`). Only operator/T0 actions can decrease severity.
3. **Durable**: Every escalation state change is recorded as a `coordination_event`.
4. **Non-blocking for `info` and `review_required`**: These states do not pause execution. Only `hold` and `escalate` block progress.
5. **Orthogonal to dispatch state**: A dispatch in state `running` can have escalation level `hold` — the dispatch execution is not interrupted, but the next lifecycle transition (e.g., completion) is blocked.

---

## 2. Escalation Transitions

### 2.1 Valid Transitions

```
info ──────────────> review_required ──────────────> hold ──────────────> escalate
  ^                       |    ^                       |    ^                |
  |                       |    |                       |    |                |
  └── operator_dismiss ───┘    └── operator_dismiss ───┘    └── t0_resolve ─┘
                               └── timeout_promote ────┘
```

| From | To | Trigger | Actor |
|---|---|---|---|
| `info` | `review_required` | Policy evaluation flags action for review | `runtime` |
| `info` | `hold` | Budget exhausted or repeated failure detected | `runtime` |
| `info` | `escalate` | Forbidden action attempted or critical governance violation | `runtime` |
| `review_required` | `hold` | Review timeout exceeded (configurable, default 30 min) | `runtime` |
| `review_required` | `escalate` | Operator explicitly escalates | `operator` or `t0` |
| `review_required` | `info` | Operator dismisses after review | `operator` or `t0` |
| `hold` | `escalate` | Hold timeout exceeded or operator escalates | `runtime` or `operator` |
| `hold` | `review_required` | Operator partially resolves; downgrades to review | `operator` or `t0` |
| `hold` | `info` | Operator fully resolves and releases hold | `operator` or `t0` |
| `escalate` | `hold` | T0 acknowledges but needs more time | `t0` |
| `escalate` | `review_required` | T0 partially resolves | `t0` |
| `escalate` | `info` | T0 fully resolves | `t0` |

### 2.2 Forbidden Transitions

| Transition | Reason |
|---|---|
| `hold` -> `info` by `runtime` | Only operator/T0 can release holds |
| `escalate` -> anything by `runtime` | Only T0 can resolve escalations |
| Any decrease by `runtime` | Runtime cannot de-escalate — only humans can |

### 2.3 Timeout Promotions

Unresolved escalation states auto-promote to prevent silent governance drift.

| From | To | Timeout | Configurable |
|---|---|---|---|
| `review_required` | `hold` | 30 minutes | Yes (`VNX_REVIEW_TIMEOUT_MIN`, default 30) |
| `hold` | `escalate` | 60 minutes | Yes (`VNX_HOLD_TIMEOUT_MIN`, default 60) |
| `escalate` | (stays `escalate`) | No timeout | N/A — T0 must resolve |

---

## 3. Escalation Triggers

### 3.1 Automatic Escalation Triggers (Runtime -> Escalation)

| Trigger Condition | Starting Level | Policy Class | Decision Type |
|---|---|---|---|
| First delivery failure | `info` | `recovery` | `delivery_retry` |
| Second delivery failure (same dispatch) | `review_required` | `recovery` | `delivery_retry` |
| Retry budget exhausted | `hold` | `recovery` | Any retry action |
| Process crash (first) | `info` | `recovery` | `process_restart` |
| Process crash (second, same terminal) | `review_required` | `recovery` | `process_restart` |
| Process crash (third, budget exhausted) | `hold` | `recovery` | `process_restart` |
| No routing target available | `review_required` | `routing` | `target_select` |
| All targets unhealthy | `hold` | `routing` | `target_select` |
| Inbox event dead-lettered | `review_required` | `dispatch_lifecycle` | `inbox_retry` |
| Forbidden action attempted by non-human actor | `escalate` | Any | Any forbidden decision type |
| Dispatch in `timed_out` for > 2 cycles | `hold` | `dispatch_lifecycle` | `dispatch_timeout` |
| Dead-letter accumulation (> 3 in 1 hour) | `escalate` | Multiple | Multiple |

### 3.2 Escalation Context

Every escalation event carries structured context for operator review:

```json
{
  "escalation_id": "<uuid>",
  "entity_type": "dispatch | target | lease | inbox_event",
  "entity_id": "<id>",
  "escalation_level": "info | review_required | hold | escalate",
  "previous_level": "info | review_required | hold | null",
  "trigger": "<trigger condition description>",
  "trigger_category": "budget_exhausted | repeated_failure | no_target | forbidden_action | timeout_promotion | dead_letter_accumulation",
  "policy_class": "<policy class>",
  "decision_type": "<decision type>",
  "retry_count": "<integer or null>",
  "budget_remaining": "<integer or null>",
  "related_events": ["<event_id references>"],
  "recommended_action": "<human-readable suggestion>",
  "occurred_at": "ISO-8601"
}
```

---

## 4. Override Semantics

An **override** is an explicit governance action where T0 or an operator bypasses a policy constraint. Overrides are not silent — they are first-class governance events.

### 4.1 Override Flow

```
1. Operator/T0 requests override
2. System records override_request event
3. System evaluates override against override policy
4. If permitted: override_granted event + action proceeds
5. If denied: override_denied event + action remains blocked
6. Override justification is durable and queryable
```

### 4.2 Override Event Schema

```json
{
  "event_type": "governance_override",
  "entity_type": "<dispatch | target | lease | policy>",
  "entity_id": "<id>",
  "actor": "t0 | operator",
  "override_type": "gate_bypass | invariant_override | dispatch_force_promote | dead_letter_override | hold_release | escalation_resolve",
  "justification": "<required, human-readable>",
  "outcome": "granted | denied",
  "previous_escalation_level": "<level before override>",
  "new_escalation_level": "<level after override>",
  "metadata_json": {
    "policy_class": "<affected policy class>",
    "decision_type": "<affected decision type>",
    "override_scope": "<narrow description of what was overridden>"
  }
}
```

### 4.3 Override Constraints

1. **Justification required**: Every override must carry a non-empty justification string.
2. **Scope-limited**: An override applies to one entity instance, not to the policy class globally.
3. **Non-precedent-setting**: An override does not modify the policy matrix. The same action evaluated later will still receive its original classification.
4. **Auditable**: Override events are queryable and included in governance audit views (PR-4).
5. **T0 authority for escalate-level**: Only T0 can resolve `escalate`-level situations. Operators can release `hold` but cannot resolve `escalate`.

---

## 5. Escalation State In Runtime Coordination

### 5.1 Schema Extension

The escalation model requires these additions to the runtime coordination schema:

```sql
-- Escalation tracking table
CREATE TABLE IF NOT EXISTS escalation_state (
    entity_type TEXT NOT NULL,           -- dispatch | target | lease | inbox_event
    entity_id TEXT NOT NULL,             -- Foreign key to entity
    escalation_level TEXT NOT NULL DEFAULT 'info',  -- info | review_required | hold | escalate
    trigger_category TEXT,               -- budget_exhausted | repeated_failure | etc.
    trigger_description TEXT,            -- Human-readable trigger
    escalated_at TEXT NOT NULL,          -- ISO-8601
    resolved_at TEXT,                    -- ISO-8601, NULL if unresolved
    resolved_by TEXT,                    -- actor who resolved
    resolution_note TEXT,               -- Resolution justification
    PRIMARY KEY (entity_type, entity_id)
);

-- Override audit log
CREATE TABLE IF NOT EXISTS governance_overrides (
    override_id TEXT PRIMARY KEY,        -- UUID
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    actor TEXT NOT NULL,                 -- t0 | operator
    override_type TEXT NOT NULL,
    justification TEXT NOT NULL,
    outcome TEXT NOT NULL,               -- granted | denied
    previous_level TEXT,
    new_level TEXT,
    occurred_at TEXT NOT NULL            -- ISO-8601
);
```

### 5.2 State Integration

- Escalation state is checked before dispatch lifecycle transitions that are gated.
- `hold` blocks: `dispatch_complete`, `pr_close`, `feature_certify`, and any transition out of `running` to `completed`.
- `escalate` blocks: All gated transitions plus prevents new dispatches to the affected entity.
- `info` and `review_required` do not block transitions.

---

## 6. Escalation State Enumeration (Canonical)

```python
ESCALATION_LEVELS = frozenset({
    "info",
    "review_required",
    "hold",
    "escalate",
})

ESCALATION_TRANSITIONS = {
    "info":             frozenset({"review_required", "hold", "escalate"}),
    "review_required":  frozenset({"info", "hold", "escalate"}),
    "hold":             frozenset({"info", "review_required", "escalate"}),
    "escalate":         frozenset({"info", "review_required", "hold"}),
}

# Actor constraints on de-escalation
DE_ESCALATION_AUTHORITY = {
    "hold":     frozenset({"t0", "operator"}),
    "escalate": frozenset({"t0"}),
}
```

---

## Appendix A: Escalation Level JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "VNX Escalation Level",
  "type": "string",
  "enum": ["info", "review_required", "hold", "escalate"]
}
```

## Appendix B: Trigger Category JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "VNX Escalation Trigger Category",
  "type": "string",
  "enum": [
    "budget_exhausted",
    "repeated_failure",
    "no_target",
    "forbidden_action",
    "timeout_promotion",
    "dead_letter_accumulation",
    "operator_escalation",
    "policy_violation"
  ]
}
```
