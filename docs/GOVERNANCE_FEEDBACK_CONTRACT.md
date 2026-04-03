# Governance Feedback Loop Contract

**Feature**: Feature 18 — Learning-Loop Signal Enrichment And Governance Feedback Hardening
**Contract-ID**: governance-feedback-v1
**Status**: Canonical
**Last Updated**: 2026-04-03

---

## 1. Purpose

This contract defines how VNX converts repeated runtime, gate, orchestration, and
chain outcomes into reusable governance signals so recurring failure patterns are
detected earlier, retrospective evidence is stronger, and future execution policy
is hardened — all without removing human governance authority.

The contract exists so that:

- Recurring failure patterns are detected from canonical evidence rather than manual log archaeology
- Signal enrichment scope is locked before implementation
- Recommendation generation is explicitly separated from authority and merge decisions
- Optional analysis helpers cannot silently become governance authority

**Relationship to existing infrastructure**:
- `outcome_signals.py` already extracts 5 signal types with 14-day recency and MD5 deduplication. This contract extends the signal model with recurrence detection, cross-run correlation, and richer output surfaces.
- `open_items_manager.py` already tracks open items with severity/status/dedup_key. This contract defines how recurring open items escalate into governance signals.
- `governance_aggregator.py` already computes FPY, rework rate, and SPC alerts nightly. This contract defines how those metrics feed the feedback loop.
- `learning_loop.py` already tracks pattern confidence with decay/boost. This contract defines canonical signal classes and recurrence thresholds for that loop to consume.

---

## 2. Canonical Signal Classes

### 2.1 Signal Class Definitions

Every governance signal belongs to exactly one class. Signal classes partition the
full space of evidence that the feedback loop processes.

| Class | Code | Source | Description |
|-------|------|--------|-------------|
| Runtime Failure | `RUNTIME_FAILURE` | headless_runs, coordination_events | Subprocess exit, timeout, no-output hang, infrastructure failure |
| Gate Failure | `GATE_FAILURE` | review_gate results, codex_final_gate | Blocking findings, gate timeout, provider unavailability |
| Delivery Failure | `DELIVERY_FAILURE` | dispatch_broker, failure_classifier | Stale lease, transport error, worker handoff rejection |
| Orchestration Failure | `ORCH_FAILURE` | dispatcher, lease_manager | Stuck dispatch, zombie lease, queue divergence |
| Chain Failure | `CHAIN_FAILURE` | chain_recovery, chain_state_projection | Feature advancement blocked, recovery limit exceeded |
| Open Item Recurrence | `OI_RECURRENCE` | open_items_manager | Same dedup_key reappears across dispatches or features |
| Quality Regression | `QUALITY_REGRESSION` | governance_aggregator, spc_alerts | FPY drop, rework spike, SPC control-limit breach |

### 2.2 Signal Record Schema

Every signal record has this structure:

```json
{
  "signal_id": "sig-<uuid4>",
  "signal_class": "RUNTIME_FAILURE",
  "source_type": "headless_run|gate_result|dispatch|open_item|metric",
  "source_id": "run-xxx|gate-xxx|d-xxx|OI-123",
  "dispatch_id": "d-xxx",
  "feature_id": "Feature 18",
  "terminal_id": "T2",
  "timestamp": "ISO8601",
  "content": "Codex gate timed out after 600s on PR-2",
  "severity": "blocker|warn|info",
  "failure_class": "TIMEOUT",
  "recurrence_key": "<derived dedup key>",
  "recurrence_count": 3,
  "first_seen": "ISO8601",
  "last_seen": "ISO8601",
  "metadata": {}
}
```

| Field | Purpose |
|-------|---------|
| `signal_id` | Unique signal identity. Never reused. |
| `signal_class` | One of the 7 classes (Section 2.1). |
| `source_type` | What produced this signal. |
| `source_id` | Identity of the source entity. |
| `recurrence_key` | Derived deduplication key for matching recurrences (Section 3.1). |
| `recurrence_count` | How many times this pattern has been seen. |
| `first_seen` / `last_seen` | Temporal window of this recurrence. |

---

## 3. Recurrence Detection And Deduplication

### 3.1 Recurrence Key Derivation

Recurrence matching answers: "Is this the same problem happening again?" The
recurrence key is derived per signal class:

| Signal Class | Recurrence Key Components | Example |
|--------------|--------------------------|---------|
| `RUNTIME_FAILURE` | `(failure_class, target_type, task_class)` | `TIMEOUT:headless_codex_cli:research_structured` |
| `GATE_FAILURE` | `(gate_type, failure_reason_category)` | `codex_gate:provider_timeout` |
| `DELIVERY_FAILURE` | `(failure_class, terminal_id)` | `stale_lease:T2` |
| `ORCH_FAILURE` | `(failure_type, terminal_id)` | `zombie_lease:T1` |
| `CHAIN_FAILURE` | `(failure_class, feature_id)` | `non_recoverable:Feature18` |
| `OI_RECURRENCE` | `(dedup_key)` | Existing open-item dedup_key |
| `QUALITY_REGRESSION` | `(metric_name, scope_type, scope_value)` | `fpy:gate:codex_gate` |

### 3.2 Recurrence Thresholds

A signal becomes a **recurrence** when it matches an existing recurrence key
within the recurrence window. Thresholds determine escalation:

| Threshold | Count | Action |
|-----------|-------|--------|
| First occurrence | 1 | Record signal. No escalation. |
| Recurrence detected | 2 | Add to retrospective digest. Flag as recurring. |
| Persistent recurrence | 3+ | Escalate to recommendation surface. Generate operator recommendation. |
| Chronic recurrence | 5+ | Escalate to carry-forward signal bundle. Flag for governance review. |

### 3.3 Recurrence Window

- Default window: **14 days** (matches existing `RECENCY_WINDOW_DAYS` in outcome_signals.py)
- Signals older than the recurrence window are not counted for recurrence detection
- The window applies to `last_seen`, not `first_seen` — a pattern that recurs after dormancy resets

### 3.4 Deduplication Rules

1. **Within-dispatch**: Same recurrence key + same dispatch_id = single signal (update count)
2. **Cross-dispatch**: Same recurrence key + different dispatch_id = new occurrence (increment recurrence_count)
3. **Cross-feature**: Same recurrence key + different feature_id = carry-forward recurrence (highest escalation priority)
4. **Content deduplication**: Signal content is hashed (MD5, 12-char prefix) consistent with existing `outcome_signals.py` pattern. Duplicate content within the same recurrence key is merged.

---

## 4. Operator-Authoritative Output Surfaces

### 4.1 Output Surface Overview

The feedback loop produces three output surfaces. All are **read-only recommendations**
— none has merge authority, dispatch authority, or policy-write authority.

| Surface | Audience | Trigger | Path |
|---------|----------|---------|------|
| **Retrospective Digest** | T0 / Operator | Per-feature closeout or on-demand | `$VNX_DATA_DIR/feedback/retrospective_<feature_id>.json` |
| **Recommendation Surface** | T0 / Operator | When persistent recurrence (3+) is detected | `$VNX_STATE_DIR/governance_recommendations.json` |
| **Carry-Forward Signal Bundle** | Next-feature context | Feature boundary crossing | `$VNX_STATE_DIR/carry_forward_signals.json` |

### 4.2 Retrospective Digest

Generated at feature closeout. Contains:

```json
{
  "feature_id": "Feature 18",
  "generated_at": "ISO8601",
  "signal_summary": {
    "total_signals": 42,
    "by_class": { "RUNTIME_FAILURE": 15, "GATE_FAILURE": 8, ... },
    "by_severity": { "blocker": 3, "warn": 12, "info": 27 }
  },
  "recurrences": [
    {
      "recurrence_key": "TIMEOUT:headless_codex_cli:research_structured",
      "signal_class": "RUNTIME_FAILURE",
      "count": 4,
      "first_seen": "ISO8601",
      "last_seen": "ISO8601",
      "recommendation": "Increase Codex gate timeout or simplify prompts"
    }
  ],
  "top_recommendations": [
    {
      "priority": "P0",
      "recommendation": "string",
      "evidence_ids": ["sig-xxx", "sig-yyy"],
      "recurrence_key": "string"
    }
  ],
  "quality_trend": {
    "fpy_start": 0.82,
    "fpy_end": 0.88,
    "direction": "improving"
  }
}
```

### 4.3 Recommendation Surface

Live surface updated when persistent recurrences are detected:

```json
{
  "updated_at": "ISO8601",
  "recommendations": [
    {
      "id": "rec-<uuid4>",
      "priority": "P0|P1|P2",
      "signal_class": "RUNTIME_FAILURE",
      "recurrence_key": "TIMEOUT:headless_codex_cli:research_structured",
      "recurrence_count": 4,
      "recommendation": "Increase VNX_CODEX_GATE_TIMEOUT from 600s to 900s",
      "evidence_ids": ["sig-xxx"],
      "status": "pending|acknowledged|applied|dismissed",
      "acknowledged_at": null,
      "acknowledged_by": null
    }
  ]
}
```

**Status lifecycle**: `pending` -> `acknowledged` (operator saw it) -> `applied` (action taken) or `dismissed` (operator declined with reason).

### 4.4 Carry-Forward Signal Bundle

Produced at feature boundaries for next-feature context injection:

```json
{
  "source_feature": "Feature 18",
  "target_feature": "Feature 19",
  "generated_at": "ISO8601",
  "signals": [
    {
      "recurrence_key": "string",
      "signal_class": "string",
      "recurrence_count": 4,
      "last_seen": "ISO8601",
      "recommendation": "string",
      "severity": "warn"
    }
  ],
  "chronic_patterns": [
    {
      "recurrence_key": "string",
      "count": 7,
      "recommendation": "string"
    }
  ]
}
```

Only signals with `recurrence_count >= 2` are included. Chronic patterns (5+) are
highlighted separately for governance review.

---

## 5. Authority Boundary

### 5.1 What The Feedback Loop May Do

| Allowed | Example |
|---------|---------|
| Collect signals from canonical evidence sources | Read receipts, open items, gate results, coordination events |
| Detect recurrences via key matching | Count TIMEOUT signals with same recurrence key |
| Generate recommendations with evidence | "Increase timeout; evidence: sig-xxx, sig-yyy" |
| Write to recommendation surface | Update governance_recommendations.json |
| Write retrospective digest | Generate per-feature summary |
| Inject signals into next-dispatch context | Via carry-forward signal bundle -> ContextAssembler |

### 5.2 What The Feedback Loop Must NOT Do

| Forbidden | Reason |
|-----------|--------|
| Modify dispatch prompts, CLAUDE.md, or skill instructions | Policy authority is human-only |
| Approve, reject, or merge PRs | Merge authority is human-only |
| Create, promote, or cancel dispatches | Dispatch authority is T0-only |
| Modify feature plans or PR queue | Planning authority is T0-only |
| Override gate results or waive blockers | Gate authority is the gate system |
| Change runtime configuration (timeouts, thresholds) | Config authority is operator-only |
| Execute actions based on its own recommendations | Recommendations are advisory-only |

### 5.3 Authority Invariants

- **A-1**: No recommendation may be applied without explicit operator acknowledgment. The `status` field in recommendations must transition through `acknowledged` before `applied`.
- **A-2**: No signal or recommendation may modify canonical state (dispatch state, lease state, chain state, open item status). Signals are append-only evidence.
- **A-3**: The feedback loop's outputs are consumable by context injection (P7 priority per context_assembler contract) but never override higher-priority context components.
- **A-4**: Dismissing a recommendation is a valid operator action. Dismissed recommendations must record `dismissed_reason` for audit.
- **A-5**: The feedback loop must never fabricate signals. Every signal must trace to a specific `source_id` in a canonical evidence store (receipts, coordination events, open items, gate results, metrics).

---

## 6. Local-Model Helper Role

### 6.1 Purpose

An optional local model (e.g., a small on-device LLM) may assist with signal
analysis — specifically with pattern clustering, recommendation phrasing, and
retrospective summarization. This section defines its role boundaries.

### 6.2 What The Local Model May Do

| Allowed | Constraint |
|---------|-----------|
| Summarize recurrence patterns into human-readable recommendations | Output is tagged `source: local_model` and is advisory-only |
| Cluster similar signals that share no exact recurrence key | Suggestions must be surfaced as "potential cluster" not "confirmed recurrence" |
| Draft retrospective digest narrative sections | Operator must review before inclusion in final digest |
| Suggest recurrence key refinements | Suggestions must be logged but not auto-applied |

### 6.3 What The Local Model Must NOT Do

| Forbidden | Reason |
|-----------|--------|
| Write to canonical state | Authority boundary (Section 5.2) |
| Create signals without a canonical source | A-5 invariant — no fabricated signals |
| Modify recurrence keys or thresholds | Configuration authority is operator-only |
| Auto-apply its own recommendations | A-1 invariant — operator acknowledgment required |
| Run without explicit opt-in | Must be enabled via `VNX_LOCAL_MODEL_HELPER=1` (default: disabled) |
| Be required for core feedback-loop operation | The feedback loop must function fully without any local model |

### 6.4 Tagging

All local-model outputs must carry:

```json
{
  "generated_by": "local_model",
  "model_id": "string",
  "confidence": 0.0-1.0,
  "requires_operator_review": true
}
```

Outputs missing this tagging must be rejected by the recommendation surface.

---

## 7. Integration Points

### 7.1 Signal Sources (Input)

| Source | Data | Integration |
|--------|------|-------------|
| `t0_receipts.ndjson` | task_complete, task_failed, task_timeout events | Existing `outcome_signals.py` pipeline, extended with recurrence detection |
| `open_items.json` | Open items with severity, dedup_key, status | Existing `open_items_manager.py`, extended with OI_RECURRENCE class |
| `review_gate results/` | Gate pass/fail with blocking/advisory findings | Read gate result JSON for GATE_FAILURE signals |
| `coordination_events` | Delivery attempts, adapter events, lease transitions | Query for DELIVERY_FAILURE and ORCH_FAILURE signals |
| `governance_metrics` | FPY, rework rate, SPC alerts | Read for QUALITY_REGRESSION signals |
| `chain_carry_forward.json` | Cross-feature findings and residual risks | Read for CHAIN_FAILURE signals and carry-forward |

### 7.2 Signal Consumers (Output)

| Consumer | What It Reads | Purpose |
|----------|---------------|---------|
| `ContextAssembler` (P7 priority) | carry_forward_signals.json | Inject recurring patterns into next dispatch context |
| T0 orchestrator | governance_recommendations.json | Review recommendations during dispatch planning |
| Feature certification (PR-4) | retrospective_digest.json | Include in certification evidence |
| Chain advancement | carry_forward_signals.json | Include chronic patterns in advancement decision context |

---

## 8. Storage

### 8.1 Signal Store

Signals are persisted in `$VNX_STATE_DIR/feedback/signals.ndjson` as append-only
NDJSON. Each line is one signal record (Section 2.2).

### 8.2 Recurrence Index

Recurrence state is maintained in `$VNX_STATE_DIR/feedback/recurrence_index.json`:

```json
{
  "recurrences": {
    "TIMEOUT:headless_codex_cli:research_structured": {
      "signal_class": "RUNTIME_FAILURE",
      "count": 4,
      "first_seen": "ISO8601",
      "last_seen": "ISO8601",
      "source_ids": ["sig-xxx", "sig-yyy"],
      "escalation_level": "persistent"
    }
  },
  "updated_at": "ISO8601"
}
```

### 8.3 Retention

- Signal store: retained for 30 days (2x recurrence window)
- Recurrence index: retained indefinitely (compact, append-only keys)
- Retrospective digests: retained indefinitely (one per feature)
- Recommendations: retained until explicitly dismissed or applied

---

## 9. Testing Contract

### 9.1 Signal Collection Tests

1. Each signal class produces correctly structured records
2. Recurrence key derivation is deterministic per class
3. Signals always trace to a canonical source_id (A-5)
4. Content deduplication matches existing MD5 pattern

### 9.2 Recurrence Detection Tests

1. First occurrence does not escalate
2. Second occurrence flags as recurring
3. Third occurrence generates recommendation
4. Fifth occurrence escalates to carry-forward
5. Signals beyond recurrence window are not counted
6. Cross-feature recurrence has highest priority

### 9.3 Output Surface Tests

1. Retrospective digest contains correct signal summary
2. Recommendation surface updates on persistent recurrence
3. Carry-forward bundle includes only recurrence_count >= 2
4. Chronic patterns (5+) are highlighted separately
5. Recommendation status lifecycle is enforced (pending->acknowledged->applied|dismissed)

### 9.4 Authority Boundary Tests

1. Feedback loop cannot write to dispatch state
2. Feedback loop cannot modify open item status
3. Recommendations require acknowledgment before applied
4. Dismissed recommendations record reason
5. All signals have non-null source_id

### 9.5 Local Model Helper Tests

1. Local model outputs are tagged with generated_by
2. Untagged outputs are rejected
3. Local model disabled by default (VNX_LOCAL_MODEL_HELPER=0)
4. Core feedback loop operates without local model

---

## 10. Migration Path

### Phase 1: Contract Lock (This PR)
- Contract document is canonical
- No code changes

### Phase 2: Signal Enrichment (PR-1)
- Extend outcome_signals.py with recurrence detection
- Add signal collection from gates, delivery, orchestration
- Persist to feedback/signals.ndjson

### Phase 3: Retrospective And Recommendation Surfaces (PR-2)
- Generate retrospective digest at feature closeout
- Build recommendation surface with status lifecycle
- Connect to ContextAssembler for carry-forward injection

### Phase 4: Local Model Helper (PR-3)
- Optional local-model integration behind feature flag
- Tagging and rejection infrastructure
- Pattern clustering suggestions

### Phase 5: Certification (PR-4)
- Prove signal correctness and recurrence detection
- Prove authority boundary enforcement
- Update planning docs

---

## 11. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should recurrence detection use exact key matching or fuzzy similarity? | Exact key matching only. Fuzzy clustering is local-model-assisted and always tagged as "potential". |
| Should the feedback loop run continuously or on-demand? | Signal collection runs per-receipt (existing pipeline). Recurrence analysis runs at dispatch boundary and feature closeout. |
| Should recommendations auto-expire? | No. Recommendations persist until explicitly acknowledged, applied, or dismissed. Stale recommendations are surfaced in the retrospective digest. |
| Should the local model be required for any feature? | No. The feedback loop must function fully without any local model (Section 6.3). |
| Should carry-forward signals include raw content? | No. Only recurrence keys, counts, recommendations, and severity. Raw signal content is in the signal store for drill-down. |
