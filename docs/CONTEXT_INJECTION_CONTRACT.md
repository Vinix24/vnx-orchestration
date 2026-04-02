# Context Injection and Handover Contract

**Status**: Accepted
**PR**: PR-0
**Gate**: gate_pr0_context_and_handover_contract
**Date**: 2026-04-02
**Author**: T3 (Track C Architecture)

This document defines the canonical context bundle structure, handover and resume payload contracts, measurement targets, and stale-context rejection rules for autonomous coding dispatches in VNX.

All subsequent implementation PRs (PR-1 through PR-4) share this contract as their single source of truth for context injection and handover behavior.

---

## 1. Context Bundle Structure

### 1.1 Definition

A **context bundle** is the complete set of information injected into a dispatch prompt when a worker begins execution. It consists of mandatory components (must always be present) and optional components (included when relevant and budget permits).

A context bundle is **not** the full conversation history. It is a bounded, curated selection of information assembled at dispatch-create or dispatch-resume time per the existing FP-C Intelligence Contract.

### 1.2 Context Components

Every dispatch context bundle is composed of these component classes, in priority order:

| Priority | Component | Class | Description | Budget Share |
|----------|-----------|-------|-------------|-------------|
| P0 | Dispatch Identity | mandatory | dispatch_id, PR, track, gate, feature name | fixed (~200 tokens) |
| P1 | Task Specification | mandatory | skill command, scoped task description, deliverables, success criteria, quality gate checklist | 30-40% |
| P2 | Mandatory Code Context | mandatory | file references (`@file` annotations) listed in the dispatch | variable, from dispatch |
| P3 | Chain Position | mandatory-when-chained | current feature, dependency status, carry-forward summary (blocker count, warn count, residual risk count) | 5-10% |
| P4 | Intelligence Payload | optional-bounded | proven patterns, failure prevention, recent comparables per FP-C contract (max 3 items, 2000 chars) | max 10% |
| P5 | Prior PR Evidence | optional | key findings from immediately preceding PR in the dependency chain | max 10% |
| P6 | Open Items Digest | optional | relevant open items with severity, filtered to dispatch scope | max 5% |
| P7 | Reusable Signals | optional | outcome signals extracted from prior receipts and chain history | max 5% |

### 1.3 Component Invariants

- **CTX-1**: P0 and P1 components are always present. A dispatch without identity or task specification is invalid.
- **CTX-2**: P2 components are present when the dispatch references specific files. File references use `@file` syntax resolved at assembly time.
- **CTX-3**: P3 components are present when the dispatch is part of a multi-feature chain. Presence is determined by querying `chain_state_projection.build_chain_projection()`, which derives chain state from the dispatch filesystem and carry-forward ledger. The chain projection output is the canonical source — not any single JSON file.
- **CTX-4**: P4 components follow the existing FP-C Intelligence Contract (max 3 items, max 2000 chars total, confidence thresholds enforced).
- **CTX-5**: Optional components (P5-P7) are included only when the total context bundle fits within the dispatch prompt budget. When budget is exceeded, components are trimmed in reverse priority order (P7 first).
- **CTX-6**: No component may contain raw conversation history or full prior session transcripts. All components must be structured summaries or curated references.

### 1.4 Context Budget

| Budget Class | Target | Hard Limit |
|-------------|--------|------------|
| Context overhead (P3 + P4 + P5 + P6 + P7 combined) | < 20% of total dispatch prompt | 25% |
| Intelligence payload (P4) | < 2000 chars | 2000 chars (FP-C contract) |
| Chain position summary (P3) | < 500 tokens | 750 tokens |
| Prior PR evidence (P5) | < 800 tokens | 1000 tokens |
| Open items digest (P6) | < 300 tokens | 500 tokens |
| Reusable signals (P7) | < 300 tokens | 500 tokens |

**Budget model**: The overhead metric measures P3 through P7 combined (all supplementary context beyond identity, task spec, and code references). P0 (dispatch identity), P1 (task specification), and P2 (mandatory code context) are excluded from the overhead metric because they are structurally required and not subject to optimization — they are the work itself.

P3 (chain position) is included in the overhead metric even when mandatory-when-chained, because chain position is supplementary context that can be optimized for size. Its mandatory status means it must be present, not that it is exempt from budget.

**Budget enforcement**: The context assembler MUST measure the total token count of P3-P7 components and calculate the ratio against the full dispatch prompt token count. Assembly must be rejected when the hard limit (25%) is exceeded. Budget overflow is a defect, not a warning.

---

## 2. Mandatory vs Optional Context

### 2.1 Mandatory Components

These components MUST be present in every dispatch context bundle:

| Component | Source | Validation Rule |
|-----------|--------|----------------|
| `dispatch_id` | Dispatch system | Non-empty string matching `YYYYMMDD-HHMMSS-*` pattern |
| `pr_id` | FEATURE_PLAN.md | Non-empty string matching `PR-N` pattern |
| `track` | Dispatch assignment | One of `A`, `B`, `C` |
| `gate` | FEATURE_PLAN.md | Non-empty gate identifier |
| `feature_name` | FEATURE_PLAN.md | Non-empty human-readable name |
| `skill_command` | Dispatch skill routing | Valid skill activation command |
| `task_description` | Dispatch scope | Non-empty structured task description |
| `deliverables` | FEATURE_PLAN.md | At least one deliverable listed |
| `success_criteria` | FEATURE_PLAN.md | At least one success criterion listed |
| `quality_gate_checklist` | FEATURE_PLAN.md | Gate checklist items |

### 2.2 Mandatory-When-Chained Components

These components are mandatory when the dispatch is part of a multi-feature chain:

| Component | Source | Validation Rule |
|-----------|--------|----------------|
| `chain_position` | `chain_state_projection.build_chain_projection()` | Current feature index and total count |
| `carry_forward_summary` | `chain_state_projection.build_carry_forward_summary()` | Blocker count, warn count, deferred count, residual risk count |
| `blocking_items` | open_items.json | List of blocker-severity open items (empty list if none) |
| `dependency_status` | Dispatch filesystem (`.vnx-data/dispatches/`) with `pr_queue_state.json` as read-optimized fallback (staleness < 60s required; see Note below) |

**Dependency source note**: Per the Queue Truth Contract (`docs/core/70_QUEUE_TRUTH_CONTRACT.md`), `pr_queue_state.json` is a cached projection view and must not be the sole basis for dispatch decisions. The canonical dependency status is derived from the dispatch filesystem (completed dispatch records). `pr_queue_state.json` may be used as a read-optimized view when its `updated_at` timestamp is within 60 seconds of the current time. When stale, the context assembler must fall back to the dispatch filesystem.

### 2.3 Optional Components

These are included when budget permits and relevance criteria are met:

| Component | Inclusion Criteria | Relevance Filter |
|-----------|--------------------|-----------------|
| Intelligence payload | FP-C contract criteria met | Task-class and skill-aware selection |
| Prior PR evidence | Dependency PR completed within same feature | Only immediate predecessor's key findings |
| Open items digest | Open items exist for this feature | Filtered to current PR scope and severity >= warn |
| Reusable signals | Outcome signals exist from prior chain history | Within 14-day recency window, matching task class |

---

## 3. Handover Payload Contract

### 3.1 Definition

A **handover payload** is the structured output a worker produces when completing a dispatch, enabling the next actor (T0, another worker, or a resume session) to continue without requiring full re-investigation of the completed work.

### 3.2 Handover Payload Structure

Every handover MUST contain these sections:

```json
{
  "handover_version": "1.0",
  "dispatch_id": "<dispatch-id>",
  "pr_id": "<PR-N>",
  "track": "<A|B|C>",
  "gate": "<gate-id>",
  "status": "<success|failed|partial>",

  "completion_summary": {
    "what_was_done": "<1-3 sentence summary>",
    "key_decisions": ["<decision 1>", "..."],
    "files_modified": [
      {"path": "<relative-path>", "change_type": "<created|modified|deleted>", "description": "<brief>"}
    ]
  },

  "evidence": {
    "tests_run": "<count or 'none'>",
    "tests_passed": "<count>",
    "tests_failed": "<count>",
    "commands_executed": ["<command 1>", "..."],
    "verification_method": "<local_tests|ci_green|manual_review|none>"
  },

  "next_action": {
    "recommended_action": "<advance|review|fix|block|escalate>",
    "reason": "<why this action>",
    "blocking_conditions": ["<condition 1>", "..."]
  },

  "residual_state": {
    "open_items_created": [
      {"id": "<OI-N>", "severity": "<blocker|warn|info>", "title": "<brief>"}
    ],
    "findings": [
      {"severity": "<blocker|warn|info>", "description": "<brief>"}
    ],
    "residual_risks": [
      {"risk": "<description>", "mitigation": "<plan or 'none'>"}
    ],
    "deferred_items": [
      {"id": "<D-N>", "severity": "<warn|info>", "reason": "<deferral reason>"}
    ]
  },

  "context_for_next": {
    "critical_context": "<1-2 sentences the next actor absolutely must know>",
    "gotchas": ["<non-obvious issue 1>", "..."],
    "relevant_file_paths": ["<path 1>", "..."]
  }
}
```

### 3.3 Handover Invariants

- **HO-1**: Every dispatch completion MUST produce a handover payload, whether the dispatch succeeded or failed.
- **HO-2**: The `status` field must honestly reflect the outcome. A failed dispatch with `status: "success"` is a handover defect.
- **HO-3**: The `next_action` section must always be present with a concrete recommendation. "unknown" is not a valid recommended action.
- **HO-4**: The `residual_state` section must be present even when empty (empty arrays for all fields).
- **HO-5**: The `context_for_next` section must contain at least `critical_context`. This is the most important field for downstream actors.

### 3.4 Handover Validation

A handover is valid when:

1. All mandatory sections are present and non-null
2. `dispatch_id` matches the executing dispatch
3. `status` is one of: `success`, `failed`, `partial`
4. `next_action.recommended_action` is one of: `advance`, `review`, `fix`, `block`, `escalate`
5. `files_modified` entries have valid `change_type` values
6. `open_items_created` entries have valid severity values

Invalid handovers are flagged as quality defects and trigger a handover-quality open item.

---

## 4. Resume Payload Contract

### 4.1 Definition

A **resume payload** is the context injected when a worker resumes an interrupted dispatch or continues from a context rotation. It enables the resuming actor to pick up work without starting from scratch.

### 4.2 Resume Payload Structure

```json
{
  "resume_version": "1.0",
  "resume_type": "<rotation|interruption|redispatch>",
  "original_dispatch_id": "<dispatch-id>",
  "original_session_id": "<session-id, if available>",

  "prior_progress": {
    "work_completed": "<summary of what was done before interruption>",
    "work_remaining": "<summary of what still needs to be done>",
    "files_in_progress": ["<path 1>", "..."],
    "last_known_state": "<description of where work stopped>"
  },

  "context_snapshot": {
    "key_decisions_made": ["<decision 1>", "..."],
    "findings_so_far": [
      {"severity": "<blocker|warn|info>", "description": "<brief>"}
    ],
    "blockers_encountered": ["<blocker 1>", "..."]
  },

  "dispatch_context": {
    "task_specification": "<original task spec, full>",
    "carry_forward_summary": "<chain position summary, if chained>"
  }
}
```

### 4.3 Resume Type Definitions

| Type | Trigger | Expected Behavior |
|------|---------|-------------------|
| `rotation` | Context window approaching limit (>65% used) | Continue same work with fresh context; prior progress summary injected |
| `interruption` | Worker process killed or timed out | Resume from last known state; may need to re-verify prior work |
| `redispatch` | T0 decides to redispatch after failed attempt | Fresh start with lessons from prior attempt; prior findings injected |

### 4.4 Resume Invariants

- **RS-1**: A resume payload must always include the original task specification. The resuming actor should not need to re-derive what was asked.
- **RS-2**: For `rotation` resumes, `prior_progress.work_completed` must be specific enough that the resuming actor does not redo completed work.
- **RS-3**: For `interruption` resumes, `prior_progress.last_known_state` must describe where the interruption occurred. "in progress" is not specific enough.
- **RS-4**: For `redispatch` resumes, `context_snapshot.findings_so_far` must include any findings from the failed attempt so they are not lost.
- **RS-5**: Resume payloads must not contain raw conversation history or full session transcripts.

### 4.5 Resume Acceptance Criteria

A resume is accepted (no immediate redispatch needed) when:

1. The resuming actor can identify what work remains without re-reading the full prior context
2. No critical prior decisions are lost between the interruption and the resume
3. The resume contains enough evidence of prior work to avoid duplication
4. The original task specification is intact and actionable

---

## 5. Measurement Contract

### 5.1 Context Waste

**Definition**: Context waste is the proportion of the total dispatch prompt budget consumed by context that does not contribute to the worker's task execution.

**Measurement Method**:
1. Calculate total token count of supplementary context components (P3 + P4 + P5 + P6 + P7)
2. Calculate total token count of the full dispatch prompt (P0 through P7 inclusive)
3. Context overhead ratio = supplementary context tokens (P3-P7) / total prompt tokens

**Target**: Context overhead ratio < 20% of total dispatch prompt budget on the validated path.

**Hard ceiling**: Context overhead ratio must not exceed 25%. Exceeding this triggers a context-waste open item.

### 5.2 Resume Acceptance Rate

**Definition**: The proportion of resume events where the resuming actor can continue work without T0 needing to intervene with a corrective redispatch within the same dispatch scope.

**Measurement Method**:
1. Count total resume events (rotation + interruption + redispatch)
2. Count resume events where the next dispatch for the same PR was a corrective redispatch caused by insufficient resume context
3. Resume acceptance rate = 1 - (corrective redispatches / total resumes)

**Target**: Resume acceptance rate >= 80% across sampled review cases.

**Measurement caveat**: Redispatches caused by new scope changes or unrelated failures do not count against the resume acceptance rate. Only redispatches attributable to inadequate resume context are counted.

### 5.3 Handover Completeness

**Definition**: The proportion of handover payloads that pass structural validation (Section 3.4) without manual correction.

**Measurement Method**:
1. Count total handover payloads produced
2. Count handovers that pass all validation rules
3. Handover completeness = valid handovers / total handovers

**Target**: Handover completeness >= 90%.

### 5.4 Measurement Recording

All measurements are recorded in the chain carry-forward ledger as feature summary metadata:

```json
{
  "context_metrics": {
    "context_overhead_ratio": 0.15,
    "resume_acceptance_rate": 0.85,
    "handover_completeness": 0.92,
    "measured_at": "ISO 8601",
    "sample_size": 12
  }
}
```

---

## 6. Stale-Context Rejection Rules

### 6.1 Definition

**Stale context** is any context component whose source data has changed since the context was assembled, making the injected information potentially misleading or incorrect.

### 6.2 Staleness Criteria

| Component | Staleness Signal | Max Age |
|-----------|-----------------|---------|
| Chain position summary | `main` has advanced since assembly | 0 (must reflect current HEAD) |
| Prior PR evidence | Referenced PR has new commits since evidence was extracted | 0 (must reflect latest merge) |
| Open items digest | Open items state has changed since digest was generated | 1 hour |
| Intelligence payload | Intelligence DB updated since payload was selected | 24 hours (FP-C recency window) |
| Reusable signals | Source receipts older than recency window | 14 days (FP-C contract) |
| Carry-forward summary | Carry-forward ledger updated since summary was generated | 0 (must reflect current ledger) |

### 6.3 Rejection Rules

- **STALE-1**: A context component that exceeds its max age MUST be refreshed or excluded from the bundle. Stale components must not be silently injected.
- **STALE-2**: For components with max age 0 (chain position, carry-forward, prior PR evidence), staleness means the context assembler must re-derive the component at assembly time. Caching is not permitted for these.
- **STALE-3**: A dispatch whose mandatory chain-position context is stale (main has advanced past the expected SHA) MUST be rejected at the dispatch level, not just at the context level. This integrates with the branch baseline guard from the chain contract.
- **STALE-4**: Stale-context injection is classified as a **defect** — it triggers an open item with severity `warn` and the tag `stale_context_injection`.
- **STALE-5**: The context assembler must record a freshness timestamp for each component in the bundle metadata. This enables post-hoc staleness auditing.

### 6.4 Freshness Metadata

Every assembled context bundle includes a freshness record:

```json
{
  "bundle_freshness": {
    "assembled_at": "ISO 8601",
    "main_sha_at_assembly": "<git rev-parse main>",
    "component_freshness": {
      "chain_position": {"source_updated_at": "ISO 8601", "is_fresh": true},
      "intelligence_payload": {"source_updated_at": "ISO 8601", "is_fresh": true},
      "prior_pr_evidence": {"source_updated_at": "ISO 8601", "is_fresh": true},
      "open_items_digest": {"source_updated_at": "ISO 8601", "is_fresh": true},
      "reusable_signals": {"source_updated_at": "ISO 8601", "is_fresh": true}
    }
  }
}
```

---

## 7. Contract Boundaries

### 7.1 What This Contract Governs

- Context bundle structure, components, and budget
- Handover payload structure and validation
- Resume payload structure and acceptance criteria
- Measurement targets and recording
- Stale-context detection and rejection

### 7.2 What This Contract Does Not Govern

- Individual intelligence selection algorithms (governed by FP-C Intelligence Contract)
- Context rotation mechanics (governed by CONTEXT_ROTATION.md)
- Dispatch lifecycle and state machine (governed by Dispatch Guide and Headless Run Contract)
- Chain state transitions (governed by Multi-Feature Chain Contract)
- Review gate behavior (governed by review gate contracts)

### 7.3 Relationship to Existing Contracts

| Contract | Relationship |
|----------|-------------|
| FP-C Intelligence Contract | Context bundle P4 component follows FP-C rules; this contract adds budget enforcement on top |
| Context Rotation (CONTEXT_ROTATION.md) | Resume payloads formalize what rotation handovers currently do ad hoc |
| Multi-Feature Chain Contract | Chain position and carry-forward summary components derive from chain state |
| Dispatch Guide | Context bundles are assembled at dispatch-create time per existing dispatch lifecycle |
| Headless Run Contract | Handover payloads integrate with headless run completion artifacts |

---

## 8. Implementation Notes for Downstream PRs

- **PR-1** (Context Selection and Budget): Implement the context assembler following the component priority and budget rules in Sections 1 and 2. Implement staleness checks from Section 6.
- **PR-2** (Handover and Resume Quality): Implement handover payload generation (Section 3) and resume payload generation (Section 4). Add validation per Section 3.4.
- **PR-3** (Outcome Signals): Implement reusable signal extraction (P7 component) and integrate with the context assembler. Verify stale-history exclusion per Section 6.
- **PR-4** (Certification): Measure context waste, resume acceptance, and handover completeness against the targets in Section 5. Certify the contract holds under operational conditions.
