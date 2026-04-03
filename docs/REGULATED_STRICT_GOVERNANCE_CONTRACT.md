# Regulated-Strict Governance Contract

**Feature**: Feature 21 — Regulated-Strict Governance Profile And Audit Bundle
**Contract-ID**: regulated-strict-governance-v1
**Status**: Canonical
**Last Updated**: 2026-04-03

---

## 1. Purpose

This contract defines the `regulated_strict` governance profile — the strictest
governance mode in VNX. It establishes explicit approval semantics, audit-bundle
composition requirements, and closure rules that prohibit any implicit closeout.

The contract exists so that:

- Regulated governance is explicit before implementation
- Approval steps are unambiguous and independently auditable
- Audit bundles are reproducible from retained evidence
- The profile cannot weaken `coding_strict` or `business_light` behavior

**Relationship to existing contracts**:
- `AGENT_OS_LIFT_IN_CONTRACT.md` defines the `regulated_strict` row in the capability profile matrix. This contract fills in the governance rules.
- `BUSINESS_LIGHT_GOVERNANCE_CONTRACT.md` defines a lighter profile. This contract defines a stricter one — they share the substrate but differ in every governance dimension.

---

## 2. Regulated-Strict Governance Rules

### 2.1 Profile Summary

| Aspect | `coding_strict` | `business_light` | `regulated_strict` |
|--------|-----------------|-------------------|---------------------|
| Scope model | Git worktree | Folder | Sandbox |
| Review policy | Gate per PR | Review-by-exception | Gate per dispatch + approval |
| Gate requirement | Codex + Gemini | None required | Codex + Gemini + audit gate |
| Closure authority | Human only | Manager (with audit) | Human only + approval record |
| Policy mutation | Blocked | Blocked | Blocked |
| Evidence retention | 30 days | 14 days | 365 days |
| Audit trail | Coordination events | Lifecycle events only | Full audit bundle per dispatch |
| Approval required | No (gate-based) | No (exception-based) | Yes (explicit step) |
| Runtime adapter | tmux (primary) | headless (primary) | headless (primary) |

### 2.2 Approval Workflow

Every regulated_strict dispatch requires an explicit approval step. Approvals are not inferred from gate results or manager decisions.

```
DISPATCH_CREATED -> PENDING_APPROVAL -> APPROVED -> EXECUTING -> PENDING_REVIEW -> CLOSED
                         |                                            |
                         v                                            v
                    REJECTED (terminal)                      REVIEW_FAILED -> PENDING_APPROVAL (loop)
```

| State | Description | Transition Trigger |
|-------|-------------|-------------------|
| `PENDING_APPROVAL` | Dispatch registered, awaiting explicit operator approval | Dispatch created |
| `APPROVED` | Operator approved execution | Operator records approval |
| `EXECUTING` | Worker executing the dispatch | Delivery successful |
| `PENDING_REVIEW` | Execution complete, awaiting post-execution review | Worker receipt received |
| `CLOSED` | Operator reviewed and accepted | Operator records closure |
| `REJECTED` | Operator rejected the dispatch | Operator records rejection |
| `REVIEW_FAILED` | Post-execution review found issues | Reviewer records failure |

### 2.3 Approval Record

Every approval must produce an immutable record:

```json
{
  "approval_id": "appr-<uuid4>",
  "dispatch_id": "d-xxx",
  "approved_by": "operator|T0",
  "approved_at": "ISO8601",
  "approval_type": "pre_execution|post_review",
  "rationale": "string (required, non-empty)",
  "evidence_refs": ["sig-xxx", "gate-xxx"],
  "conditions": []
}
```

**Invariants**:
- **RA-1**: `rationale` must be non-empty. Empty-string approvals are rejected.
- **RA-2**: `approved_by` must be `"operator"` or `"T0"`. Automated approvals are forbidden.
- **RA-3**: Approval records are immutable after creation. No amendments — create a new record instead.
- **RA-4**: Every dispatch must have at least one pre-execution approval and one post-review closure record.

---

## 3. Audit Bundle

### 3.1 Purpose

A regulated-strict dispatch produces an **audit bundle** — a self-contained directory
of evidence sufficient to reconstruct what was requested, approved, executed, and
reviewed. The bundle must be reproducible from retained artifacts.

### 3.2 Bundle Composition

Every audit bundle contains these mandatory artifacts:

| Artifact | Description | Source |
|----------|-------------|--------|
| `dispatch.json` | Dispatch registration record with prompt, metadata, config | dispatch_broker |
| `approval_pre.json` | Pre-execution approval record(s) | approval workflow |
| `execution_log.txt` | Raw stdout/stderr from worker | session lifecycle |
| `event_stream.ndjson` | Structured event stream (if session_event_stream=True) | headless_event_stream |
| `gate_results/` | Directory of gate result JSONs (one per gate) | review gates |
| `open_items.json` | Open items created during this dispatch | open_items_manager |
| `receipt.json` | Final receipt with provenance chain | receipt pipeline |
| `approval_close.json` | Post-review closure approval record | approval workflow |
| `bundle_manifest.json` | Inventory of all artifacts with checksums | bundle generator |

### 3.3 Bundle Manifest

```json
{
  "bundle_id": "bundle-<uuid4>",
  "dispatch_id": "d-xxx",
  "domain": "regulated",
  "governance_profile": "regulated_strict",
  "created_at": "ISO8601",
  "artifacts": [
    {
      "name": "dispatch.json",
      "path": "dispatch.json",
      "sha256": "abc123...",
      "size_bytes": 1234,
      "required": true,
      "present": true
    }
  ],
  "completeness": {
    "all_required_present": true,
    "missing": []
  }
}
```

### 3.4 Bundle Completeness

A bundle is **complete** when all required artifacts are present and their checksums
match the manifest. Incomplete bundles block dispatch closure (see Section 4).

### 3.5 Bundle Location

```
$VNX_DATA_DIR/audit_bundles/<dispatch_id>/
  bundle_manifest.json
  dispatch.json
  approval_pre.json
  execution_log.txt
  event_stream.ndjson
  gate_results/
    codex_gate.json
    gemini_review.json
    audit_gate.json
  open_items.json
  receipt.json
  approval_close.json
```

### 3.6 Retention

Audit bundles are retained for **365 days** (matching `audit_retention_days` in the capability profile). Bundles must not be deleted by automated cleanup within this window.

---

## 4. Closure Semantics

### 4.1 No Implicit Closeouts

`regulated_strict` prohibits all forms of implicit closure:

| Prohibited | Reason |
|------------|--------|
| Manager auto-closure | No `closure_authority: "manager"` allowed |
| Gate-pass auto-closure | Passing all gates does not close the dispatch |
| Timeout-based closure | Dispatch timeout transitions to `PENDING_APPROVAL`, not `CLOSED` |
| Receipt-triggered closure | Receipt arrival triggers `PENDING_REVIEW`, not `CLOSED` |
| Stale-dispatch cleanup | Stale regulated dispatches escalate to operator, never auto-close |

### 4.2 Closure Requirements

A dispatch may transition to `CLOSED` only when ALL of these are true:

1. At least one pre-execution approval record exists (RA-4)
2. Worker execution has completed (receipt received)
3. All required gates have terminal results (pass or fail)
4. Audit bundle is complete (Section 3.4)
5. Operator has recorded an explicit post-review closure approval (RA-4)

### 4.3 Closure Record

```json
{
  "closure_id": "close-<uuid4>",
  "dispatch_id": "d-xxx",
  "closed_by": "operator",
  "closed_at": "ISO8601",
  "closure_type": "approved|rejected|exception",
  "rationale": "string (required)",
  "bundle_id": "bundle-xxx",
  "bundle_complete": true,
  "open_items_resolved": true,
  "residual_risks": []
}
```

---

## 5. Capability Profile Declaration

Per `AGENT_OS_LIFT_IN_CONTRACT.md` Section 5:

```python
regulated_strict_profile = CapabilityProfile(
    domain_id="regulated",
    governance_profile="regulated_strict",
    manager_persistence=True,
    manager_judgment_authority=True,  # with escalation
    worker_headless_default=True,
    worker_disposable=True,
    worker_scope_model="sandbox",
    session_attempt_tracking=True,
    session_evidence_required=True,
    session_event_stream=True,
    gate_required=True,
    gate_types=["codex_gate", "gemini_review", "audit_gate"],
    closure_requires_human=True,
    policy_mutation_blocked=True,
    audit_retention_days=365,
    runtime_adapter_type="headless",
    provider_types=["claude_code"],
)
```

Note: The `audit_gate` gate type does not yet exist in `IMPLEMENTED_GATES`. This intentionally blocks regulated-strict domain activation until the gate is implemented (per `AGENT_OS_LIFT_IN_CONTRACT.md` Section 7.1 anti-goals).

---

## 6. Cross-Profile Boundaries

### 6.1 Isolation From Other Profiles

| Rule | Description |
|------|-------------|
| **RS-1** | Regulated dispatches cannot access coding worktrees or business folders |
| **RS-2** | Regulated open items are namespaced: `OI-RS-NNN` |
| **RS-3** | Regulated workers use separate terminal pool: RT0 (manager), RW1, RW2 |
| **RS-4** | Regulated approval records are stored separately from coding/business receipts |
| **RS-5** | Regulated audit bundles cannot reference artifacts from other domains |
| **RS-6** | coding_strict and business_light operations are unaware of regulated_strict state |

### 6.2 Sandbox Scope Model

Regulated work uses a **sandbox** isolation model:

| Aspect | Detail |
|--------|--------|
| Root | A dedicated sandbox directory (e.g., `~/.vnx-sandboxes/<sandbox-id>/`) |
| Isolation | Directory boundaries + read-only access to input artifacts |
| Write scope | Only within sandbox root. No writing outside. |
| Network | Configurable (default: restricted to local APIs) |
| Cleanup | Sandbox preserved for audit retention period, then eligible for cleanup |
| Git | Not applicable (no git operations in sandbox) |

---

## 7. Non-Goals And Pilot Limits

### 7.1 Non-Goals

| Non-Goal | Reason |
|----------|--------|
| No real regulated-domain rollout | Profile is defined but not activatable (audit_gate missing) |
| No external compliance integration | Compliance APIs are out of scope |
| No automated approval | RA-2 prohibits automated approvals |
| No cross-domain audit sharing | RS-5 prevents cross-domain artifact references |
| No replacing coding_strict or business_light | Each profile is independent |

### 7.2 Pilot Limits (When Eventually Activated)

| Constraint | Value |
|------------|-------|
| Max concurrent sandboxes | 1 |
| Max active regulated workers | 1 |
| Max dispatch age | 2 hours |
| Approval timeout | 24 hours (escalate if no approval) |
| Bundle retention | 365 days |
| Feature flag | `VNX_REGULATED_STRICT_ENABLED=0` (default disabled) |

### 7.3 Rollback Criteria

| Trigger | Action |
|---------|--------|
| Audit bundle integrity failure (checksums don't match) | Disable regulated dispatches, investigate |
| Approval record tampering detected | Disable immediately, escalate |
| Regulated dispatch escapes sandbox boundary | Disable, revert sandbox changes |
| coding_strict or business_light regression | Revert regulated-strict changes |
| Evidence retention violation (bundle deleted prematurely) | Restore from backup, investigate |

---

## 8. Testing Contract

### 8.1 Approval Workflow Tests

1. Dispatch requires pre-execution approval before delivery
2. Empty-rationale approval rejected (RA-1)
3. Automated approvals rejected (RA-2)
4. Approval records are immutable (RA-3)
5. Closure requires both pre and post approval (RA-4)

### 8.2 Audit Bundle Tests

1. Complete bundle has all required artifacts
2. Incomplete bundle blocks closure
3. Manifest checksums match artifacts
4. Bundle location follows convention
5. Bundle retained for 365 days (retention flag)

### 8.3 Closure Semantics Tests

1. Manager auto-closure rejected
2. Gate-pass does not auto-close
3. Timeout transitions to PENDING_APPROVAL, not CLOSED
4. Stale dispatches escalate, never auto-close
5. All 5 closure requirements enforced

### 8.4 Isolation Tests

1. Regulated dispatch cannot access coding paths (RS-1)
2. Open items namespaced with `OI-RS-` (RS-2)
3. Regulated terminals separate from coding/business (RS-3)
4. Sandbox write scope enforced

---

## 9. Migration Path

### Phase 1: Contract Lock (This PR)
- Contract document is canonical
- No code changes

### Phase 2: Approval Workflow (PR-1)
- Implement approval state machine
- Implement approval record with invariants
- Add pre-execution and post-review approval flow

### Phase 3: Audit Bundle Generator (PR-2)
- Implement bundle composition and manifest
- Implement completeness checking
- Add checksum verification

### Phase 4: Sandbox Scope And Closure Integration (PR-3)
- Implement sandbox isolation model
- Integrate closure requirements
- Add regulated terminal pool

### Phase 5: Certification (PR-4)
- Prove approval workflow correctness
- Prove audit bundle completeness
- Prove closure prohibition enforcement
- Update planning docs

---

## 10. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should the audit_gate be implemented in this feature? | No. The gate type is declared in the profile but not implemented. This intentionally blocks activation until a dedicated audit-gate feature. |
| Should approval records support amendments? | No. RA-3 requires immutability. Create a new approval record with a correction rationale instead. |
| Should regulated_strict share the coordination DB? | Yes. Same DB with domain column, same as business_light. Domain isolation is enforced at the application layer. |
| Can regulated dispatches use tmux? | No. Headless-by-default. Sandbox isolation and tmux panes are incompatible (pane content is visible to other terminals). |
| Should the 365-day retention be configurable? | Not in this feature. Hardcoded in the profile. Configuration deferred to a future governance evolution feature. |
