# Chain-Level Residual Governance Model

**Status**: Accepted
**PR**: PR-3
**Gate**: gate_pr3_chain_findings_carry_forward
**Date**: 2026-04-02
**Author**: T3 (Track C Quality Engineering)

This document defines how findings, open items, and residual risks are governed across multi-feature chain boundaries. It complements `MULTI_FEATURE_CHAIN_CONTRACT.md` (Section 6) with operational governance rules for chain-level residuals.

---

## 1. Governance Scope

This model governs three classes of chain-level residuals:

| Class | Definition | Lifecycle |
|-------|-----------|-----------|
| **Findings** | Observations from review gates, T3 analysis, or test results | Created during feature execution; carried in ledger until resolved or chain closes |
| **Open Items** | Tracked items requiring action (`open_items.json` records) | Created during feature execution; cumulative across features; closed with evidence |
| **Residual Risks** | Known risks accepted within a feature's scope | Accepted by a specific feature; carried until mitigated or chain closes |

---

## 2. Carry-Forward Ledger as Source of Truth

The carry-forward ledger (`chain_carry_forward.json`) is the single source of truth for cross-feature residuals. It accumulates state across feature boundaries and is never reset mid-chain.

### 2.1 Persistence Guarantees

- **Never silently dropped**: Items added to the ledger persist until explicitly closed or the chain terminates. No feature boundary can cause items to disappear.
- **Provenance preserved**: Every item retains its `origin_feature` — the feature that first created it. Subsequent updates to status do not overwrite provenance.
- **Status updates are monotonic for closures**: An item resolved in a later feature has its status updated in-place. The resolution is visible in the ledger alongside the original provenance.

### 2.2 Ledger Sections

| Section | Content | Accumulation Rule |
|---------|---------|-------------------|
| `findings` | All findings from all features, with severity and resolution status | Append-only; new findings added per feature |
| `open_items` | All open items, snapshotted at each feature boundary | Merge-by-ID; status updates, provenance preserved |
| `deferred_items` | Items explicitly deferred with rationale | Append-only |
| `residual_risks` | Accepted risks with accepting feature and rationale | Append-only |
| `feature_summaries` | Per-feature completion records with counts and gate results | Append-only; one record per feature |

---

## 3. Decision Rules at Feature Boundaries

At each feature boundary, the following governance decisions apply:

### 3.1 Findings

| Severity | Unresolved Action | Effect on Chain |
|----------|-------------------|-----------------|
| `blocker` | Must be resolved before advancement | Chain enters `ADVANCEMENT_BLOCKED` |
| `warn` | Carried forward; acknowledged in next dispatch | No block; visible in next feature context |
| `info` | Carried forward for audit | No block; no acknowledgment required |

### 3.2 Open Items

| Severity | Status at Boundary | Allowed Action |
|----------|-------------------|----------------|
| `blocker` | `open` | **Must resolve** — blocks advancement |
| `blocker` | `done`/`closed` | Advancement allowed |
| `warn` | `open` | Carry forward or defer with reason |
| `warn` | `deferred` | Allowed — reason recorded |
| `info` | any | Carry forward; no block |

### 3.3 Residual Risks

| Condition | Required Action |
|-----------|----------------|
| New risk identified | Record with accepting feature and acceptance rationale |
| Earlier risk becomes blocker in later feature | Escalate to `CHAIN_HALTED` with reference to original acceptance |
| Chain closes with open risks | Final certification must enumerate all risks and confirm acceptance or mitigation |

---

## 4. Chain Stop Conditions from Residuals

The chain MUST halt when any of these residual-driven conditions are true:

1. **Unresolved blocker open item** at feature boundary (O-2)
2. **Unresolved blocker finding** at feature boundary (F-2)
3. **Residual risk escalation**: a risk accepted in an earlier feature becomes a blocker in a later feature (RR-4)

These conditions produce explicit, operator-readable blocker messages in the advancement truth surface. They are never silently suppressed.

---

## 5. Final Certification Requirements

When the chain completes, the final certification PR (PR-4) must:

1. **Enumerate all findings** from the carry-forward ledger with their resolution status
2. **Confirm all open items** are either closed with evidence or explicitly deferred with rationale
3. **List all residual risks** with their accepting feature and current mitigation status
4. **Confirm zero unresolved blocker items** remain in the ledger
5. **Provide the complete feature summary sequence** showing progressive accumulation

The carry-forward ledger serves as the evidence artifact for this certification.

---

## 6. Relationship to Existing Governance

| Existing System | Relationship |
|----------------|--------------|
| `open_items_manager.py` | Single-feature item lifecycle unchanged; chain adds cross-feature accumulation |
| Review gate results | Gate findings feed into chain carry-forward as findings records |
| `t0_receipts.ndjson` | Chain events (halt, advance) emit receipts; residual state is in the ledger |
| `MULTI_FEATURE_CHAIN_CONTRACT.md` | This document operationalizes Section 6 of the chain contract |
