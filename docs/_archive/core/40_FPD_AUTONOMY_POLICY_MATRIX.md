# FP-D Autonomy Policy Matrix — Action Classes, Policy Evaluation, And Governance Envelopes

**Feature**: FP-D — Safe Autonomy, Governance Envelopes, And End-To-End Provenance
**PR**: PR-0
**Status**: Canonical
**Purpose**: Defines what VNX may do automatically, what requires a gate, and what is forbidden. All FP-D implementation PRs evaluate actions against this matrix. No action class may be added or reclassified without updating this document.

---

## 1. Policy Classes

A **policy class** categorizes a runtime decision by its governance impact. Every evaluable action maps to exactly one policy class.

| Policy Class | Description | Gate Requirement | Examples |
|---|---|---|---|
| `operational` | Routine runtime coordination with no governance significance | None (automatic) | Heartbeat checks, health state updates, lease renewals |
| `dispatch_lifecycle` | Dispatch state transitions within normal flow | None (automatic) | queued -> claimed, delivering -> accepted, running -> completed |
| `recovery` | Bounded recovery actions within retry budget | None (automatic, budget-limited) | Process restart (within budget), re-delivery after timeout, lease recovery |
| `routing` | Execution target selection and task class assignment | None (automatic, invariant-bound) | Router selects target per R-1..R-8, fallback to interactive |
| `intelligence` | Bounded intelligence injection into dispatches | None (automatic, bounded by injection policy) | Attach up to 3 intelligence items at dispatch creation/resume |
| `escalation` | Actions that change governance visibility or operator attention | Operator review required | T0 escalation events, hold state entry, repeated failure alerts |
| `completion` | Dispatch completion, PR closure, feature closure | T0 or explicit human gate | Mark dispatch completed, close PR, certify feature |
| `configuration` | Changes to runtime policy, thresholds, or enforcement behavior | T0 or explicit human gate | Modify retry budget, change routing invariants, toggle feature flags |
| `merge` | Git merge operations to protected branches | T0 or explicit human gate | Merge to main, merge feature branch |
| `override` | Bypass of a governance rule or policy constraint | T0 with explicit justification | Skip a gate check, override routing invariant, force-promote dispatch |

### 1.1 Policy Class Invariants

1. **Exhaustive**: Every evaluable action maps to exactly one policy class.
2. **Non-overlapping**: No action belongs to two policy classes simultaneously.
3. **Immutable per evaluation**: The policy class assigned to an action does not change during evaluation.
4. **Additive only**: New policy classes require a contract update; existing classes are never silently redefined.

---

## 2. Decision Types

A **decision type** describes the specific kind of runtime action being evaluated within a policy class.

### 2.1 Operational Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `heartbeat_check` | `operational` | Periodic terminal health verification |
| `health_state_update` | `operational` | Update target health based on check result |
| `lease_renewal` | `operational` | Extend active lease within normal lifecycle |
| `event_append` | `operational` | Append coordination event to audit trail |

### 2.2 Dispatch Lifecycle Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `dispatch_create` | `dispatch_lifecycle` | Create new dispatch from T0 instruction |
| `dispatch_claim` | `dispatch_lifecycle` | Target claims queued dispatch |
| `dispatch_deliver` | `dispatch_lifecycle` | Deliver dispatch payload to target |
| `dispatch_accept` | `dispatch_lifecycle` | Target acknowledges delivery |
| `dispatch_run` | `dispatch_lifecycle` | Dispatch execution begins |
| `dispatch_timeout` | `dispatch_lifecycle` | Dispatch exceeds time threshold |
| `dispatch_fail` | `dispatch_lifecycle` | Delivery or execution failure recorded |

### 2.3 Recovery Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `process_restart` | `recovery` | Restart crashed worker process (within budget) |
| `delivery_retry` | `recovery` | Re-deliver after transient failure |
| `lease_recover` | `recovery` | Recover expired lease to idle state |
| `dispatch_recover` | `recovery` | Move timed-out or failed dispatch to recovered state |
| `inbox_retry` | `recovery` | Retry failed inbox event processing |

### 2.4 Routing Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `target_select` | `routing` | Select execution target for dispatch |
| `fallback_route` | `routing` | Apply fallback when preferred target unavailable |
| `override_route` | `routing` | T0-specified target override (within R-5, R-6 constraints) |

### 2.5 Intelligence Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `intelligence_inject` | `intelligence` | Attach bounded intelligence payload to dispatch |
| `intelligence_suppress` | `intelligence` | Suppress items that fail threshold or budget |

### 2.6 Escalation Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `escalation_emit` | `escalation` | Emit escalation event for operator attention |
| `hold_enter` | `escalation` | Transition action to hold state pending review |
| `hold_release` | `escalation` | Release held action after operator review |
| `escalate_to_t0` | `escalation` | Escalate to T0 for governance decision |

### 2.7 Completion Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `dispatch_complete` | `completion` | Mark dispatch as successfully completed |
| `pr_close` | `completion` | Close PR after all gates pass |
| `feature_certify` | `completion` | Certify feature against certification matrix |

### 2.8 Configuration Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `policy_update` | `configuration` | Modify autonomy policy or threshold |
| `feature_flag_toggle` | `configuration` | Enable or disable feature flag |
| `budget_adjust` | `configuration` | Change retry budget or timeout threshold |

### 2.9 Merge Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `branch_merge` | `merge` | Merge branch to protected target |
| `force_push` | `merge` | Force push to any branch |

### 2.10 Override Decisions

| Decision Type | Policy Class | Description |
|---|---|---|
| `gate_bypass` | `override` | Skip a quality gate check |
| `invariant_override` | `override` | Override a routing or policy invariant |
| `dispatch_force_promote` | `override` | Force-promote dispatch past normal lifecycle |
| `dead_letter_override` | `override` | Manually recover dead-lettered dispatch |

---

## 3. Action Classification — Automatic, Gated, Forbidden

Every decision type is classified into exactly one action class. This classification is the core governance contract.

### 3.1 Automatic Actions

Automatic actions execute without operator intervention. They are bounded by policy invariants, retry budgets, and injection limits.

| Decision Type | Bound | Escalation Trigger |
|---|---|---|
| `heartbeat_check` | Health check interval | Never |
| `health_state_update` | Transition rules (healthy/degraded/unhealthy/offline) | Target becomes unhealthy |
| `lease_renewal` | Lease duration | Never |
| `event_append` | Append-only, immutable | Never |
| `dispatch_create` | T0 instruction required | Never |
| `dispatch_claim` | One claim per target | Never |
| `dispatch_deliver` | Delivery timeout threshold | Delivery failure |
| `dispatch_accept` | ACK timeout threshold | ACK timeout |
| `dispatch_run` | Execution timeout threshold | Execution timeout |
| `dispatch_timeout` | Timeout threshold | Always emits escalation after threshold |
| `dispatch_fail` | Failure recorded | Always emits escalation |
| `process_restart` | Max 3 restarts per budget window | Budget exhausted -> hold |
| `delivery_retry` | Max 3 retries per dispatch | Budget exhausted -> hold |
| `lease_recover` | One recovery attempt | Recovery fails -> escalate |
| `dispatch_recover` | One recovery per timeout/failure cycle | Recovery fails -> dead_letter |
| `inbox_retry` | Max retries per inbox config | Budget exhausted -> dead_letter |
| `target_select` | R-1..R-8 invariants | No eligible target -> queue + escalate |
| `fallback_route` | Fallback rules from FP-C Section 3.3 | All fallbacks exhausted -> escalate |
| `intelligence_inject` | Max 3 items, max 2000 chars, threshold >= 0.6 | Never |
| `intelligence_suppress` | Suppression always logged | Never |

### 3.2 Gated Actions

Gated actions require explicit T0 or operator approval before execution. The system prepares the action, emits a review request, and waits.

| Decision Type | Gate Authority | Evidence Required |
|---|---|---|
| `escalation_emit` | Automatic (emits for review) | Trigger reason + context |
| `hold_enter` | Automatic (system enters hold) | Trigger condition evidence |
| `hold_release` | T0 or operator | Review confirmation |
| `escalate_to_t0` | Automatic (emits to T0) | Full escalation context |
| `dispatch_complete` | T0 | Receipt evidence + gate pass |
| `pr_close` | T0 or operator | All gate checks pass |
| `feature_certify` | T0 | Full certification matrix pass |
| `policy_update` | T0 | Justification + impact assessment |
| `feature_flag_toggle` | T0 or operator | Rollback plan documented |
| `budget_adjust` | T0 | Justification |
| `override_route` | T0 (within R-5, R-6) | T0 dispatch metadata |
| `dead_letter_override` | T0 | Manual review of dead-letter evidence |

### 3.3 Forbidden Actions

Forbidden actions are never permitted under any circumstances in FP-D. Attempting a forbidden action is itself a governance event.

| Decision Type | Rationale |
|---|---|
| `branch_merge` (autonomous) | G-R4: Merge authority remains with T0 or explicit human gate |
| `force_push` (autonomous) | Destructive and irreversible |
| `gate_bypass` (autonomous) | G-R2: High-risk actions are always gated |
| `invariant_override` (autonomous) | Policy invariants cannot be self-modified |
| `dispatch_force_promote` (autonomous) | Lifecycle integrity requires normal state transitions |
| `policy_update` (autonomous, from recommendation) | G-R1, A-R9: No silent policy mutation from recommendation logic |

**Clarification**: Forbidden means "forbidden without human authority." T0 or an operator may still perform these actions through explicit gated flows. The `override` policy class exists for this purpose — but the override itself must be a durable governance event (A-R7).

---

## 4. Policy Evaluation Contract

### 4.1 Evaluation Input

```json
{
  "action": "<decision_type>",
  "policy_class": "<policy_class>",
  "actor": "runtime | router | broker | t0 | operator",
  "context": {
    "dispatch_id": "<optional>",
    "target_id": "<optional>",
    "terminal_id": "<optional>",
    "retry_count": "<optional, integer>",
    "budget_remaining": "<optional, integer>",
    "escalation_level": "<optional, info | review_required | hold | escalate>"
  }
}
```

### 4.2 Evaluation Output

```json
{
  "outcome": "automatic | gated | forbidden",
  "action": "<decision_type>",
  "policy_class": "<policy_class>",
  "reason": "<human-readable explanation>",
  "escalation_level": "<info | review_required | hold | escalate | null>",
  "gate_authority": "<t0 | operator | null>",
  "evidence": {
    "evaluated_at": "ISO-8601",
    "evaluated_by": "governance_evaluator",
    "policy_version": "<policy matrix version hash>"
  }
}
```

### 4.3 Evaluation Rules

1. **Lookup**: Map `(decision_type)` to its action class from Section 3.
2. **Budget check**: If the action is automatic but budget-limited, verify remaining budget. Budget exhaustion promotes the outcome to `gated` with escalation.
3. **Invariant check**: If the action is routing, verify R-1..R-8 constraints. Invariant violation promotes the outcome to `gated` or `forbidden`.
4. **Actor check**: If the action is gated, verify the actor has gate authority. `runtime` and `router` cannot satisfy gates; `t0` and `operator` can.
5. **Forbidden check**: If the action is forbidden without human authority, return `forbidden` regardless of actor unless the actor is `t0` or `operator` executing through an explicit override flow.
6. **Event emission**: Every evaluation emits a `coordination_event` with `event_type = "policy_evaluation"`.

---

## 5. Feature Flag Controls

FP-D autonomy is controlled by feature flags for reversible rollout (A-R10).

| Flag | Default | Effect When Disabled |
|---|---|---|
| `VNX_AUTONOMY_EVALUATION=1` | 0 (off until PR-5 cutover) | Policy evaluation is logged but outcomes are advisory-only; all actions follow pre-FP-D behavior |
| `VNX_PROVENANCE_ENFORCEMENT=1` | 0 (off until PR-5 cutover) | Trace token validation logs warnings but does not block commits |

### 5.1 Rollout Phases

| Phase | Flags | Behavior |
|---|---|---|
| Shadow (PR-1, PR-2) | Both off | Evaluation runs, emits events, but does not gate or block |
| Enforcement (PR-3, PR-4) | `VNX_PROVENANCE_ENFORCEMENT=1` optional | Provenance validation active; autonomy still advisory |
| Cutover (PR-5) | Both on | Full policy evaluation and provenance enforcement |
| Rollback | Both off | Returns to pre-FP-D behavior |

---

## 6. Governance Rule Traceability

Every governance rule from FEATURE_PLAN.md maps to this matrix:

| Rule | Implemented By |
|---|---|
| G-R1: Every automatic action maps to a policy class | Section 1 (policy classes) + Section 3.1 (automatic actions) |
| G-R2: High-risk actions are always gated | Section 3.2 (gated actions) + Section 3.3 (forbidden actions) |
| G-R3: Repeated failure loops escalate | Section 3.1 escalation triggers + Section 3.2 hold/escalate gates |
| G-R4: Completion and merge authority remain with T0 | Section 3.2 (completion gates) + Section 3.3 (merge forbidden autonomous) |
| G-R5: Trace token required | Provenance Contract (42_FPD_PROVENANCE_CONTRACT.md) |
| G-R6: Receipts are primary evidence | Provenance Contract (42_FPD_PROVENANCE_CONTRACT.md) |
| G-R7: Bidirectional traceability | Provenance Contract (42_FPD_PROVENANCE_CONTRACT.md) |
| G-R8: No CLI-specific primary enforcement | Section 5 feature flags + Provenance Contract CI validation path |

| Architecture Rule | Implemented By |
|---|---|
| A-R1: Policy matrix is canonical data | This document |
| A-R2: Escalation states are explicit | Escalation Model (41_FPD_ESCALATION_MODEL.md) |
| A-R3: Autonomy evaluation emits events | Section 4.3 rule 6 |
| A-R4: Git enforcement at Git/CI level | Provenance Contract (42_FPD_PROVENANCE_CONTRACT.md) |
| A-R5: Receipt schema carries commit linkage | Provenance Contract (42_FPD_PROVENANCE_CONTRACT.md) |
| A-R6: Local hooks assist, CI is durable backstop | Provenance Contract (42_FPD_PROVENANCE_CONTRACT.md) |
| A-R7: Policy overrides are durable events | Section 3.3 forbidden clarification + override policy class |
| A-R8: Tolerate approved legacy refs | Provenance Contract (42_FPD_PROVENANCE_CONTRACT.md) |
| A-R9: No silent policy mutation from recommendations | Section 3.3 (forbidden: autonomous policy_update from recommendation) |
| A-R10: Autonomy rollout reversible by feature flag | Section 5 |

---

## Appendix A: Policy Class JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "VNX Policy Class",
  "type": "string",
  "enum": [
    "operational",
    "dispatch_lifecycle",
    "recovery",
    "routing",
    "intelligence",
    "escalation",
    "completion",
    "configuration",
    "merge",
    "override"
  ]
}
```

## Appendix B: Action Class JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "VNX Action Class",
  "type": "string",
  "enum": ["automatic", "gated", "forbidden"]
}
```

## Appendix C: Policy Evaluation Event Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "VNX Policy Evaluation Event",
  "type": "object",
  "required": ["event_type", "entity_type", "entity_id", "actor", "metadata_json"],
  "properties": {
    "event_type": { "const": "policy_evaluation" },
    "entity_type": { "type": "string", "enum": ["dispatch", "target", "lease", "inbox_event", "recommendation"] },
    "entity_id": { "type": "string" },
    "from_state": { "type": ["string", "null"] },
    "to_state": { "type": ["string", "null"] },
    "actor": { "type": "string", "enum": ["runtime", "router", "broker", "t0", "operator"] },
    "reason": { "type": ["string", "null"] },
    "metadata_json": {
      "type": "object",
      "required": ["action", "policy_class", "outcome"],
      "properties": {
        "action": { "type": "string" },
        "policy_class": { "$ref": "#/definitions/policy_class" },
        "outcome": { "$ref": "#/definitions/action_class" },
        "escalation_level": { "type": ["string", "null"], "enum": ["info", "review_required", "hold", "escalate", null] },
        "gate_authority": { "type": ["string", "null"], "enum": ["t0", "operator", null] },
        "budget_remaining": { "type": ["integer", "null"] },
        "policy_version": { "type": "string" }
      }
    }
  },
  "definitions": {
    "policy_class": {
      "type": "string",
      "enum": ["operational", "dispatch_lifecycle", "recovery", "routing", "intelligence", "escalation", "completion", "configuration", "merge", "override"]
    },
    "action_class": {
      "type": "string",
      "enum": ["automatic", "gated", "forbidden"]
    }
  }
}
```
