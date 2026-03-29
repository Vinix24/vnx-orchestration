# FP-C Intelligence Contract — Bounded Injection And Recommendation Classes

**Feature**: FP-C — Execution Modes, Headless Routing, And Intelligence Quality
**PR**: PR-0
**Status**: Canonical
**Purpose**: Defines the intelligence item schema, injection constraints, recommendation classes, and acceptance semantics. PR-3 (injection policy) and PR-4 (usefulness metrics) implement against this contract.

---

## 1. Intelligence Item Contract

An **intelligence item** is a single piece of evidence-backed advisory content injected into a dispatch at creation or resume time. Items are selected from the existing quality intelligence database and formatted to this canonical schema.

### 1.1 Intelligence Item Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "VNX Intelligence Item",
  "type": "object",
  "required": [
    "item_id",
    "item_class",
    "title",
    "content",
    "confidence",
    "evidence_count",
    "last_seen",
    "scope_tags"
  ],
  "properties": {
    "item_id": {
      "type": "string",
      "description": "Unique identifier for this intelligence item instance"
    },
    "item_class": {
      "type": "string",
      "enum": ["proven_pattern", "failure_prevention", "recent_comparable"],
      "description": "Classification of the intelligence item"
    },
    "title": {
      "type": "string",
      "maxLength": 120,
      "description": "Human-readable title for the item"
    },
    "content": {
      "type": "string",
      "maxLength": 500,
      "description": "The advisory content itself — concise, actionable"
    },
    "confidence": {
      "type": "number",
      "minimum": 0.0,
      "maximum": 1.0,
      "description": "Confidence score based on evidence quality (0.0 = no evidence, 1.0 = strongly proven)"
    },
    "evidence_count": {
      "type": "integer",
      "minimum": 0,
      "description": "Number of distinct evidence sources supporting this item"
    },
    "last_seen": {
      "type": "string",
      "format": "date-time",
      "description": "ISO 8601 timestamp of when the pattern/rule was last observed in production data"
    },
    "scope_tags": {
      "type": "array",
      "items": { "type": "string" },
      "minItems": 1,
      "description": "Tags scoping relevance: skill names, track IDs, file patterns, task classes"
    },
    "source_refs": {
      "type": "array",
      "items": { "type": "string" },
      "description": "References to source evidence: dispatch IDs, pattern IDs, incident IDs"
    },
    "task_class_filter": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Task classes this item is relevant to. Empty = all classes."
    }
  }
}
```

### 1.2 Intelligence Item Classes

| Item Class | Description | Source | Selection Priority |
|---|---|---|---|
| `proven_pattern` | A success pattern with demonstrated positive outcomes | `success_patterns` table (confidence >= 0.6, usage_count >= 2) | Highest — proven value |
| `failure_prevention` | A rule or antipattern that prevents known failure modes | `prevention_rules` + `antipatterns` tables (confidence >= 0.5) | High — risk reduction |
| `recent_comparable` | A recent dispatch in similar scope that provides execution context | `dispatch_metadata` table (same skill/gate, last 14 days) | Medium — context only |

---

## 2. Bounded Injection Rules

### 2.1 Injection Points

Intelligence is injected at exactly two points in the dispatch lifecycle:

| Injection Point | When | Actor |
|---|---|---|
| `dispatch_create` | When T0 creates a new dispatch via the broker | Broker / intelligence selector |
| `dispatch_resume` | When a recovered dispatch is re-delivered after failure | Recovery flow / intelligence selector |

Intelligence is **never** injected:
- During dispatch execution (no mid-turn injection)
- At receipt processing time
- As ambient context into terminal state
- Into T0's own orchestration context

### 2.2 Payload Bounds

| Constraint | Limit | Rationale |
|---|---|---|
| Maximum items per injection | **3** | One per class maximum: one proven pattern, one failure prevention, one recent comparable |
| Maximum content length per item | **500 characters** | Keeps context short; long items are noise |
| Maximum total payload size | **2000 characters** (including JSON structure) | Prevents context stuffing |
| Minimum confidence threshold | **0.4** | Items below this threshold are suppressed |
| Minimum evidence count | **1** | Zero-evidence items are never injected |

### 2.3 Selection Algorithm

```
1. Determine task_class and scope_tags from dispatch metadata
2. Query eligible items:
   a. proven_pattern:     WHERE confidence >= 0.6 AND evidence_count >= 2 AND scope matches
   b. failure_prevention: WHERE confidence >= 0.5 AND evidence_count >= 1 AND scope matches
   c. recent_comparable:  WHERE last_seen within 14 days AND scope matches
3. For each class, select the highest-confidence item that passes task_class_filter
4. If no items meet thresholds for a class, that slot is empty (not filled with a lower-quality substitute)
5. Assemble payload (0-3 items)
6. If total payload exceeds 2000 chars, drop recent_comparable first, then failure_prevention
7. Emit injection_decision event (see Section 2.4)
```

### 2.4 Injection Decision Events

Every injection decision emits a `coordination_event`:

```json
{
  "event_type": "intelligence_injection",
  "entity_type": "dispatch",
  "entity_id": "<dispatch_id>",
  "actor": "intelligence_selector",
  "reason": "injected 2 items at dispatch_create",
  "metadata_json": {
    "injection_point": "dispatch_create",
    "task_class": "research_structured",
    "items_injected": 2,
    "items_suppressed": 1,
    "suppression_reasons": ["recent_comparable below confidence threshold"],
    "payload_chars": 847,
    "item_ids": ["item_abc123", "item_def456"]
  }
}
```

When no items meet thresholds, a suppression event is emitted:

```json
{
  "event_type": "intelligence_suppression",
  "entity_type": "dispatch",
  "entity_id": "<dispatch_id>",
  "actor": "intelligence_selector",
  "reason": "no items met minimum thresholds",
  "metadata_json": {
    "injection_point": "dispatch_create",
    "task_class": "coding_interactive",
    "candidates_evaluated": 5,
    "all_below_threshold": true
  }
}
```

---

## 3. Recommendation Classes And Acceptance Semantics

A **recommendation** is a higher-level advisory derived from intelligence analysis that suggests a change to VNX behavior: prompt patches, routing preferences, guardrail adjustments, or process changes. Recommendations are distinct from intelligence items — items are injected into dispatches; recommendations are proposed to operators.

### 3.1 Recommendation Classes

| Recommendation Class | Description | Scope | Example |
|---|---|---|---|
| `prompt_patch` | Suggested modification to dispatch prompt templates or skill instructions | Skill / task class | "Add explicit file-list constraint to architect skill for research_structured dispatches" |
| `routing_preference` | Suggested change to default routing for a task class or skill | Task class / target type | "Route data-analyst dispatches headless — last 5 completed without interactive intervention" |
| `guardrail_adjustment` | Suggested threshold change for retry budgets, timeouts, or escalation triggers | Runtime config | "Increase ack_timeout for headless_claude_cli from 120s to 300s — 3 of 5 timed out" |
| `process_improvement` | Suggested workflow change not captured by the other classes | Operational | "Split large architecture dispatches into research + synthesis phases" |

### 3.2 Recommendation Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "VNX Recommendation",
  "type": "object",
  "required": [
    "recommendation_id",
    "recommendation_class",
    "title",
    "description",
    "evidence_summary",
    "confidence",
    "proposed_at"
  ],
  "properties": {
    "recommendation_id": {
      "type": "string"
    },
    "recommendation_class": {
      "type": "string",
      "enum": ["prompt_patch", "routing_preference", "guardrail_adjustment", "process_improvement"]
    },
    "title": {
      "type": "string",
      "maxLength": 120
    },
    "description": {
      "type": "string",
      "maxLength": 1000,
      "description": "Detailed description of what is recommended and why"
    },
    "evidence_summary": {
      "type": "string",
      "maxLength": 500,
      "description": "Summary of supporting evidence with dispatch/pattern references"
    },
    "confidence": {
      "type": "number",
      "minimum": 0.0,
      "maximum": 1.0
    },
    "scope_tags": {
      "type": "array",
      "items": { "type": "string" }
    },
    "proposed_at": {
      "type": "string",
      "format": "date-time"
    },
    "acceptance_state": {
      "type": "string",
      "enum": ["proposed", "accepted", "rejected", "expired", "superseded"],
      "default": "proposed"
    },
    "accepted_at": {
      "type": ["string", "null"],
      "format": "date-time"
    },
    "rejected_at": {
      "type": ["string", "null"],
      "format": "date-time"
    },
    "rejection_reason": {
      "type": ["string", "null"]
    },
    "outcome_window_start": {
      "type": ["string", "null"],
      "format": "date-time",
      "description": "Start of the measurement window after acceptance"
    },
    "outcome_window_end": {
      "type": ["string", "null"],
      "format": "date-time",
      "description": "End of the measurement window (default: 7 days after acceptance)"
    }
  }
}
```

### 3.3 Acceptance Semantics

| State | Meaning | Transition From | Transition To |
|---|---|---|---|
| `proposed` | Recommendation created, awaiting operator review | (initial) | `accepted`, `rejected`, `expired` |
| `accepted` | Operator approved; outcome measurement window opens | `proposed` | `expired` (window closes) |
| `rejected` | Operator declined with reason | `proposed` | (terminal) |
| `expired` | Not acted upon within 14 days, or measurement window closed | `proposed`, `accepted` | (terminal) |
| `superseded` | A newer recommendation replaces this one | Any non-terminal | (terminal) |

### 3.4 Acceptance Rules

1. **Advisory only**: No recommendation automatically mutates runtime behavior. All accepted recommendations require manual implementation by T0 or an operator.
2. **Outcome measurement**: When a recommendation is accepted, a measurement window opens (default 7 days). Metrics collected during this window are compared to the pre-acceptance baseline.
3. **No silent adoption**: Every acceptance and rejection is recorded with actor, timestamp, and reason.
4. **Expiration**: Unreviewed recommendations expire after 14 days. This prevents recommendation backlog from growing unbounded.
5. **Supersession**: When a new recommendation addresses the same scope and class as an existing proposed recommendation, the older one is superseded.

### 3.5 Usefulness Metrics

These metrics are collected per recommendation class during the outcome window (PR-4 implements collection):

| Metric | Description | Collected For |
|---|---|---|
| `first_pass_success_rate` | Percentage of dispatches completing on first attempt (no redispatch) | All classes |
| `redispatch_rate` | Percentage of dispatches requiring re-delivery | All classes |
| `open_item_carry_rate` | Percentage of dispatches producing blocker/warn open items | All classes |
| `ack_timeout_rate` | Percentage of dispatches timing out before ACK | `guardrail_adjustment` |
| `repeated_failure_rate` | Percentage of dispatches hitting repeated_failure_loop incidents | `guardrail_adjustment` |
| `operator_override_rate` | Percentage of routing decisions overridden by T0 after recommendation | `routing_preference` |

### 3.6 Before/After Comparison

For each accepted recommendation:

1. **Baseline window**: The 7 days before acceptance (or all available data if less than 7 days).
2. **Outcome window**: The 7 days after acceptance.
3. **Comparison scope**: Only dispatches matching the recommendation's `scope_tags` and `recommendation_class` are included.
4. **Minimum sample**: At least 3 dispatches in each window. If fewer, the comparison is marked `insufficient_data`.
5. **Delta reporting**: Each metric reports `baseline_value`, `outcome_value`, `delta`, and `direction` (improved/degraded/neutral).

---

## 4. Intelligence Payload In Dispatch Bundle

The `intelligence_payload` field in `bundle.json` and the `dispatches` table carries the bounded injection:

```json
{
  "intelligence_payload": {
    "injection_point": "dispatch_create",
    "injected_at": "2026-03-29T16:41:35.001Z",
    "items": [
      {
        "item_id": "intel_abc123",
        "item_class": "proven_pattern",
        "title": "Use structured output format for architecture reviews",
        "content": "Architecture review dispatches that use the three-section format (Analysis → Design → Blueprint) have 87% first-pass success vs 62% for unstructured.",
        "confidence": 0.87,
        "evidence_count": 8,
        "last_seen": "2026-03-28T14:00:00.000Z",
        "scope_tags": ["architect", "research_structured", "Track-C"],
        "source_refs": ["pattern_142", "dispatch_20260325-001"]
      },
      {
        "item_id": "intel_def456",
        "item_class": "failure_prevention",
        "title": "Avoid unbounded file reads in review dispatches",
        "content": "Review dispatches that read >20 files have 3x higher context pressure incidents. Scope file reads to dispatch-specified paths only.",
        "confidence": 0.72,
        "evidence_count": 5,
        "last_seen": "2026-03-27T09:30:00.000Z",
        "scope_tags": ["reviewer", "research_structured"],
        "source_refs": ["antipattern_23", "incident_20260326-T2"]
      }
    ],
    "suppressed": [
      {
        "item_class": "recent_comparable",
        "reason": "confidence 0.35 below threshold 0.4"
      }
    ]
  }
}
```

---

## 5. Integration With Existing Intelligence System

The FP-C intelligence contract builds on the existing `quality_intelligence.db` infrastructure:

| Existing Table | FP-C Usage |
|---|---|
| `success_patterns` | Source for `proven_pattern` items (confidence >= 0.6, usage_count >= 2) |
| `antipatterns` | Source for `failure_prevention` items |
| `prevention_rules` | Source for `failure_prevention` items |
| `dispatch_metadata` | Source for `recent_comparable` items and recommendation evidence |
| `pattern_usage` | Tracks whether injected patterns are used or ignored |
| `dispatch_quality_context` | Tracks injection effectiveness per dispatch |

New tables for recommendations and usefulness metrics are defined in `schemas/runtime_coordination_v4.sql`.
