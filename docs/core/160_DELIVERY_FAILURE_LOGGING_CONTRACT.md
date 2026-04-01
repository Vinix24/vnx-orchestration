# Delivery Failure Logging Contract

**Status**: Canonical
**Feature**: Fine-Grained Delivery Rejection Logging
**PR**: PR-0
**Gate**: `gate_pr0_delivery_failure_logging_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document defines the canonical rejection taxonomy for delivery failures: unique failure codes for every delivery substep, required structured fields in logs and audit artifacts, operator-readable summaries, and retry classification. All downstream PRs (PR-1 and PR-2) implement against this contract.

---

## 1. Why This Exists

### 1.1 The Problem

The VNX dispatcher has three layers of failure identification that evolved independently:

1. **Substep IDs** (Contract 150): `send_skill`, `load_buffer`, `paste_buffer`, `send_enter`, etc. — identifies *where* in the tmux transport a failure occurred.
2. **Failure classes** (`failure_classifier.py`): `invalid_skill`, `stale_lease`, `tmux_transport_failure`, etc. — identifies *what kind* of failure occurred.
3. **Block classifications** (`_classify_blocked_dispatch()`): `busy`, `ambiguous`, `invalid` — determines *what to do* with the dispatch.

These three taxonomies are not formally connected. The reason strings passed between them are ad-hoc (`"tmux delivery failed: substep=send_skill"`, `"tmux Enter failed"`, `"input_mode_blocked"`). When T0 receives a failure reason, it cannot deterministically:

- Map the reason to a unique failure code.
- Look up the operator-readable summary.
- Determine retry semantics without keyword matching.
- Distinguish pre-delivery failures (no lease held) from transport failures (lease released).

### 1.2 The Fix

Define a single canonical failure code registry that:
1. Assigns a unique `failure_code` to every delivery failure mode.
2. Specifies the exact structured fields emitted per failure.
3. Maps every code to a failure class, retry semantics, and operator summary.
4. Uses the `failure_code` as the single identifier across all three layers (substep logging, classifier, block classification).

This is a contract extension, not a replacement. Contracts 90, 140, and 150 remain authoritative for their respective concerns. This contract adds the unified code registry and structured emission requirements that connect them.

---

## 2. Canonical Failure Code Registry

### 2.1 Failure Code Format

All failure codes follow the format:

```
{phase}_{operation}
```

Where `{phase}` is one of:
- `pre` — before lease acquisition (Phase 0 per Contract 90)
- `post` — after lease, before tmux transport (Phase 1)
- `tx` — during tmux transport (Phase 2)

And `{operation}` is the specific failing operation.

### 2.2 Pre-Delivery Failures (Phase 0 — No Lease Held)

These failures occur before the canonical lease is acquired. The dispatch stays in `pending/` and is automatically retried.

| Failure Code | Operation | Retryable | Failure Class | Operator Summary |
|-------------|-----------|-----------|---------------|------------------|
| `pre_executor_resolution` | `determine_executor` fails — no available terminal for track | Yes | `tmux_transport_failure` | No terminal available for the target track. Retry when a terminal is free. |
| `pre_mode_configuration` | `configure_terminal_mode` fails — clear-context, model switch, or feedback modal stuck | Yes | `hook_feedback_interruption` | Terminal mode configuration failed (clear/switch/modal). Terminal may need operator reset. |
| `pre_skill_empty` | `map_role_to_skill` returns empty — role has no skill mapping | No | `invalid_skill` | Dispatch role has no skill mapping. Fix the Role field in the dispatch. |
| `pre_skill_registry` | Skill not found in `skills.yaml` | No | `invalid_skill` | Skill not found in the skills registry. Fix the Role or Skill field. |
| `pre_instruction_empty` | `extract_instruction_content` returns empty | No | `invalid_skill` | Dispatch contains no instruction content. Rework the dispatch body. |
| `pre_terminal_resolution` | Cannot resolve terminal ID from pane and track | Yes | `tmux_transport_failure` | Terminal ID resolution failed. Transient — retry on next cycle. |
| `pre_canonical_lease_busy` | Canonical lease held by another dispatch | Yes (defer) | N/A (busy) | Terminal is occupied by another dispatch. Deferred until lease is released. |
| `pre_canonical_lease_expired` | Canonical lease check returns expired/recovering | Yes | `stale_lease` | Lease expired or recovering. Retry after reconciler runs. |
| `pre_canonical_check_error` | Canonical lease check JSON parse or I/O failure | Yes | `stale_lease` | Lease check failed (transient I/O). Safe to retry. |
| `pre_canonical_acquire_failed` | Lease acquisition failed (contention) | Yes | `stale_lease` | Lease acquisition contention. Safe to retry. |
| `pre_legacy_lock_busy` | Legacy terminal lock held | Yes (defer) | N/A (busy) | Terminal lock held by prior dispatch. Deferred until lock clears. |
| `pre_claim_failed` | `acquire_terminal_claim` fails | Yes | `tmux_transport_failure` | Terminal claim acquisition failed. Transient — retry. |
| `pre_duplicate_delivery` | Same dispatch already holds lease on target terminal | Yes (defer) | N/A (busy) | Duplicate delivery prevented — prior attempt still holds lease. |
| `pre_validation_empty_role` | Empty or `none` role caught at pre-validation | No | `invalid_skill` | Dispatch has no role. Set a valid Role field. |
| `pre_validation_command_failed` | `gather_intelligence validate` command failed | Yes | `tmux_transport_failure` | Intelligence validation command failed. Runtime dependency — retry when resolved. |
| `pre_gather_command_failed` | `gather_intelligence gather` command failed | Yes | `tmux_transport_failure` | Intelligence gathering command failed. Runtime dependency — retry when resolved. |

### 2.3 Post-Lease, Pre-Transport Failures (Phase 1 — Lease Held)

These failures occur after the canonical lease is acquired but before tmux transport begins. Lease + claim must be released (per Contract 90 DFL-1).

| Failure Code | Operation | Retryable | Failure Class | Operator Summary |
|-------------|-----------|-----------|---------------|------------------|
| `post_input_mode_blocked` | Pane in copy/search mode, recovery failed | Yes | `hook_feedback_interruption` | Terminal pane is in non-interactive mode (copy/search). Recovery failed — retry after operator resets pane. |
| `post_process_exit` | Process killed between lease acquire and transport start | Yes | `tmux_transport_failure` | Dispatcher process exited during delivery setup. Lease released by cleanup trap. Safe to retry. |

### 2.4 Transport Failures (Phase 2 — Lease Held, tmux Active)

These failures occur during the tmux transport sequence. Lease + claim must be released. The dispatch returns to `pending/` for automatic retry.

#### Claude Code / Gemini Path

| Failure Code | Operation | tmux Call | Retryable | Failure Class | Operator Summary |
|-------------|-----------|-----------|-----------|---------------|------------------|
| `tx_send_skill` | Type skill command via send-keys | `tmux send-keys -t {pane} -l "{skill}"` | Yes | `tmux_transport_failure` | Failed to type skill command into terminal. Transient tmux issue — retry. |
| `tx_load_buffer` | Load instruction payload into tmux buffer | `tmux_load_buffer_safe "{prompt}"` | Yes | `tmux_transport_failure` | Failed to load instruction into tmux buffer. Transient — retry. |
| `tx_paste_buffer` | Paste buffer content into terminal | `tmux paste-buffer -t {pane}` | Yes | `tmux_transport_failure` | Failed to paste instruction into terminal. Transient — retry. |
| `tx_send_enter` | Submit with Enter key | `tmux send-keys -t {pane} Enter` | Yes | `tmux_transport_failure` | Failed to submit dispatch with Enter. Transient — retry. |

#### Codex Path

| Failure Code | Operation | tmux Call | Retryable | Failure Class | Operator Summary |
|-------------|-----------|-----------|-----------|---------------|------------------|
| `tx_load_buffer_codex` | Load combined skill+instruction into buffer | `tmux_load_buffer_safe "{skill}{prompt}"` | Yes | `tmux_transport_failure` | Failed to load combined content into tmux buffer. Transient — retry. |
| `tx_paste_buffer_codex` | Paste combined buffer into terminal | `tmux paste-buffer -t {pane}` | Yes | `tmux_transport_failure` | Failed to paste combined content into terminal. Transient — retry. |
| `tx_send_enter` | Submit with Enter key (shared) | `tmux send-keys -t {pane} Enter` | Yes | `tmux_transport_failure` | Failed to submit dispatch with Enter. Transient — retry. |

### 2.5 Cleanup Failures (Post-Transport)

These are secondary failures during the cleanup sequence after a primary failure. They do not have their own dispatch disposition — the primary failure code determines disposition.

| Failure Code | Operation | Operator Summary |
|-------------|-----------|------------------|
| `cleanup_lease_release_failed` | `rc_release_lease` failed after delivery failure | Lease release failed — terminal may be stranded until TTL expiry. Check `lease_cleanup_audit.ndjson`. |
| `cleanup_claim_release_failed` | `release_terminal_claim` failed after delivery failure | Legacy claim release failed. Terminal may show busy in shadow state. |
| `cleanup_broker_record_failed` | `rc_delivery_failure` failed (missing attempt_id or broker error) | Broker failure record not written. Dispatch state may be inconsistent — check broker DB. |

---

## 3. Structured Failure Event Schema

### 3.1 Primary Failure Event

Every delivery failure MUST emit a structured event via `log_structured_failure()` with these fields:

```json
{
  "event": "delivery_failure",
  "component": "dispatcher_v8_minimal.sh",
  "failure_code": "<code from Section 2>",
  "failure_class": "<class from failure_classifier.py>",
  "retryable": true,
  "operator_summary": "<from Section 2 tables>",
  "dispatch_id": "<dispatch being delivered>",
  "terminal_id": "<target terminal>",
  "provider": "<claude_code|codex_cli|gemini_cli>",
  "phase": "<pre|post|tx>",
  "details": "<key=value context string>"
}
```

**DFL-LOG-1 (Structured Event Rule)**: The `failure_code` field MUST be one of the codes defined in Section 2. No ad-hoc reason strings. The code is the canonical identifier used across all downstream systems (T0 reasoning, audit queries, operator dashboards).

### 3.2 Release-On-Failure Reason String

When `rc_release_on_failure()` is called, the reason string MUST use the failure code:

```
Current:  "tmux delivery failed: substep=send_skill"
          "tmux Enter failed"
          "input_mode_blocked"
Required: "delivery_failed:tx_send_skill"
          "delivery_failed:tx_send_enter"
          "delivery_failed:post_input_mode_blocked"
```

**Format**: `delivery_failed:{failure_code}`

This preserves the `delivery_failed:` prefix that Contract 90 and `_classify_blocked_dispatch()` use for Phase 2 classification, while embedding the canonical failure code.

### 3.3 Blocked Dispatch Audit Event

The `emit_blocked_dispatch_audit()` NDJSON record MUST include the failure code:

```json
{
  "event_type": "dispatch_blocked",
  "dispatch_id": "20260401-123111-PR-0-C",
  "terminal_id": "T2",
  "block_reason": "delivery_failed:tx_send_skill",
  "block_category": "ambiguous",
  "requeueable": true,
  "failure_code": "tx_send_skill",
  "failure_class": "tmux_transport_failure",
  "timestamp": "2026-04-01T12:31:11Z"
}
```

**DFL-LOG-2 (Audit Enrichment Rule)**: The `failure_code` and `failure_class` fields MUST be added to blocked dispatch audit events. These fields are additive — existing fields are not changed.

### 3.4 Dispatch File Annotation

For transport failures (Phase 2), the annotation format is unchanged from Contract 150:

```
[DELIVERY_SUBSTEP_FAILED: substep=<substep_id>] tmux delivery failed at substep. Retry is automatic.
```

**DFL-LOG-3 (Annotation Mapping Rule)**: The `substep` value in the annotation MUST map to a `failure_code` in Section 2 via the following translation:

| Annotation `substep=` | Failure Code |
|----------------------|--------------|
| `send_skill` | `tx_send_skill` |
| `load_buffer` | `tx_load_buffer` (Claude/Gemini) or `tx_load_buffer_codex` (Codex) |
| `paste_buffer` | `tx_paste_buffer` (Claude/Gemini) or `tx_paste_buffer_codex` (Codex) |
| `enter` | `tx_send_enter` |

PR-1 SHOULD update the annotation to use the canonical failure code directly:

```
[DELIVERY_SUBSTEP_FAILED: code=tx_send_skill] tmux delivery failed at substep. Retry is automatic.
```

### 3.5 Lease Cleanup Audit Event

The `emit_lease_cleanup_audit()` record (Contract 90 DFL-5) MUST include the failure code that triggered cleanup:

```json
{
  "event_type": "lease_released_on_failure",
  "dispatch_id": "20260401-123111-PR-0-C",
  "terminal_id": "T2",
  "lease_released": true,
  "trigger_failure_code": "tx_send_skill",
  "timestamp": "2026-04-01T12:31:11Z"
}
```

---

## 4. Retry Classification

### 4.1 Retry Decision Matrix

Every failure code maps to exactly one retry decision. T0 uses this matrix to determine next action without keyword matching.

| Retry Decision | Meaning | T0 Action |
|---------------|---------|-----------|
| **auto_retry** | Transient failure, dispatcher loop retries automatically | No T0 action needed. Dispatch stays in `pending/`. |
| **defer** | Terminal busy, dispatch deferred until available | No T0 action needed. Dispatch stays in `pending/`. |
| **manual_fix** | Dispatch metadata is invalid, requires operator edit | T0 flags for operator. Dispatch stays in `pending/` with marker. |

### 4.2 Code-to-Decision Mapping

| Failure Code | Retry Decision | Rationale |
|-------------|----------------|-----------|
| `pre_executor_resolution` | auto_retry | Terminal may become available next cycle |
| `pre_mode_configuration` | auto_retry | Terminal mode may self-recover or operator resets |
| `pre_skill_empty` | manual_fix | Role→skill mapping is missing in code |
| `pre_skill_registry` | manual_fix | Skill not in `skills.yaml` — dispatch must be edited |
| `pre_instruction_empty` | manual_fix | Dispatch body is empty — must be reworked |
| `pre_terminal_resolution` | auto_retry | Transient resolution failure |
| `pre_canonical_lease_busy` | defer | Terminal occupied, try later |
| `pre_canonical_lease_expired` | auto_retry | Reconciler will recover |
| `pre_canonical_check_error` | auto_retry | Transient I/O |
| `pre_canonical_acquire_failed` | auto_retry | Contention, next attempt may succeed |
| `pre_legacy_lock_busy` | defer | Lock held, try later |
| `pre_claim_failed` | auto_retry | Transient |
| `pre_duplicate_delivery` | defer | Prior attempt still active |
| `pre_validation_empty_role` | manual_fix | No role specified |
| `pre_validation_command_failed` | auto_retry | Runtime dependency may recover |
| `pre_gather_command_failed` | auto_retry | Runtime dependency may recover |
| `post_input_mode_blocked` | auto_retry | Pane mode may be reset |
| `post_process_exit` | auto_retry | Process restart clears condition |
| `tx_send_skill` | auto_retry | Transient tmux |
| `tx_load_buffer` | auto_retry | Transient tmux |
| `tx_paste_buffer` | auto_retry | Transient tmux |
| `tx_send_enter` | auto_retry | Transient tmux |
| `tx_load_buffer_codex` | auto_retry | Transient tmux |
| `tx_paste_buffer_codex` | auto_retry | Transient tmux |

### 4.3 Relationship To Existing Classification

The failure code registry does not replace `_classify_blocked_dispatch()` or `failure_classifier.py`. It connects them:

```
failure_code  →  failure_class  →  retryable (bool)
              →  retry_decision →  T0 action
              →  block_category →  file disposition
```

**DFL-LOG-4 (Classification Bridge Rule)**: `failure_classifier.py` MUST accept failure codes (not just reason strings) as input. The classifier SHOULD have a direct code→class lookup table in addition to keyword matching, so that codes from this registry are classified without ambiguity.

### 4.4 New Classification Case

A new case in `_classify_blocked_dispatch()` is required for the `delivery_failed:` prefix with failure codes:

```bash
delivery_failed:tx_*|delivery_failed:post_*)
    echo "ambiguous true" ;;
delivery_failed:pre_skill_*|delivery_failed:pre_instruction_*|delivery_failed:pre_validation_empty_role)
    echo "invalid false" ;;
delivery_failed:pre_*)
    echo "ambiguous true" ;;
```

This replaces the single `delivery_failed:*) echo "ambiguous true"` case with per-code precision.

---

## 5. T0 Reasoning Interface

### 5.1 Failure Summary For T0

When T0 receives a dispatch receipt or reviews blocked dispatch audit, it needs a structured summary. The failure event (Section 3.1) provides all fields T0 needs:

| T0 Question | Field |
|------------|-------|
| What failed? | `failure_code` |
| What kind of failure? | `failure_class` |
| Can it be retried? | `retryable` |
| What should the operator do? | `operator_summary` |
| Where did it fail? | `phase` + `terminal_id` |
| What dispatch? | `dispatch_id` |

### 5.2 T0 Decision Rules

**DFL-LOG-5 (T0 Reasoning Rule)**: T0 MUST use `failure_code` (not reason strings or log messages) to make routing decisions. The decision is:

```
if retry_decision == "manual_fix":
    flag dispatch for operator, do not redispatch
elif retry_decision == "defer":
    wait for terminal availability, do not intervene
elif retry_decision == "auto_retry":
    let dispatcher loop handle it; escalate only after 3+ consecutive failures of same code
```

### 5.3 Escalation Threshold

**DFL-LOG-6 (Escalation Rule)**: When the same dispatch fails with the same `failure_code` three or more consecutive times (across dispatcher loop iterations), T0 SHOULD escalate to the operator. The blocked dispatch audit trail provides the consecutive-failure evidence.

---

## 6. Non-Goals

| # | Non-Goal | Rationale |
|---|----------|-----------|
| NG-1 | Modifying lease state machine or cleanup sequence | Contract 90 governs cleanup. This contract defines logging, not behavior. |
| NG-2 | Changing dispatch file movement logic | Contract 140 governs disposition. This contract enriches the data used for decisions. |
| NG-3 | Adding per-substep retry within the transport sequence | Contract 150 NG-1 already scopes this out. |
| NG-4 | Worker-side failure codes | DFL-6 in Contract 90 scopes worker failures to TTL/reconciler. |
| NG-5 | Dashboard or alerting integration | Structured data enables dashboards; building them is a separate concern. |
| NG-6 | Replacing `failure_classifier.py` keyword matching | Adding a direct code→class lookup alongside keyword matching is in scope. Removing keyword matching is not. |

---

## 7. Implementation Constraints For PR-1

1. **Failure codes from Section 2 are canonical.** PR-1 must use these exact codes in `log_structured_failure()` calls.
2. **`rc_release_on_failure()` reason strings** must use `delivery_failed:{failure_code}` format (Section 3.2).
3. **Blocked dispatch audit events** must include `failure_code` and `failure_class` fields (Section 3.3).
4. **`_classify_blocked_dispatch()`** must add per-code classification cases (Section 4.4).
5. **`failure_classifier.py`** must add a code→class lookup table that maps every code in Section 2 to its failure class (Section 4.3, DFL-LOG-4).
6. **Existing log messages** may retain their current format alongside the structured event. The structured event is the canonical record; log messages are for human readability.
7. **Successful delivery paths must not be affected** — no changes to the success flow.

---

## 8. Contract Verification

### 8.1 How To Verify This Contract Is Satisfied

| # | Check | Method |
|---|-------|--------|
| V-1 | Every delivery failure emits a structured event with `failure_code` | Grep dispatcher for `log_structured_failure` calls; verify every failure path uses a code from Section 2 |
| V-2 | `rc_release_on_failure` uses `delivery_failed:{code}` format | Grep for `rc_release_on_failure` calls; verify reason format |
| V-3 | Blocked dispatch audit includes `failure_code` field | Inspect `emit_blocked_dispatch_audit` output format |
| V-4 | `failure_classifier.py` maps every code in Section 2 to a class | Unit test: pass each code, verify class matches Section 2 table |
| V-5 | `_classify_blocked_dispatch` handles `delivery_failed:{code}` patterns | Unit test: pass each code pattern, verify category matches Section 4.4 |
| V-6 | T0 can determine retry decision from `failure_code` alone | Integration test: simulate failures, verify T0 receives structured events with all fields from Section 5.1 |

### 8.2 Quality Gate Checklist

`gate_pr0_delivery_failure_logging_contract`:
- [x] Contract defines unique failure codes for all delivery substeps (Section 2: 22 codes across 3 phases + 3 cleanup codes)
- [x] Contract defines structured fields required in logs and audit artifacts (Section 3: event schema, reason format, audit enrichment, annotation mapping, cleanup audit)
- [x] Contract defines retryable vs non-retryable semantics by failure type (Section 4: per-code retry decision matrix with 3 decisions)
- [ ] Gemini review receipt and normalized report exist with no unresolved blocking findings
- [ ] Codex final gate receipt and normalized report exist with no unresolved blocking findings

---

## Appendix A: Failure Code Quick Reference

| Code | Phase | Retryable | Decision | Class |
|------|-------|-----------|----------|-------|
| `pre_executor_resolution` | pre | Yes | auto_retry | tmux_transport_failure |
| `pre_mode_configuration` | pre | Yes | auto_retry | hook_feedback_interruption |
| `pre_skill_empty` | pre | No | manual_fix | invalid_skill |
| `pre_skill_registry` | pre | No | manual_fix | invalid_skill |
| `pre_instruction_empty` | pre | No | manual_fix | invalid_skill |
| `pre_terminal_resolution` | pre | Yes | auto_retry | tmux_transport_failure |
| `pre_canonical_lease_busy` | pre | Yes | defer | N/A (busy) |
| `pre_canonical_lease_expired` | pre | Yes | auto_retry | stale_lease |
| `pre_canonical_check_error` | pre | Yes | auto_retry | stale_lease |
| `pre_canonical_acquire_failed` | pre | Yes | auto_retry | stale_lease |
| `pre_legacy_lock_busy` | pre | Yes | defer | N/A (busy) |
| `pre_claim_failed` | pre | Yes | auto_retry | tmux_transport_failure |
| `pre_duplicate_delivery` | pre | Yes | defer | N/A (busy) |
| `pre_validation_empty_role` | pre | No | manual_fix | invalid_skill |
| `pre_validation_command_failed` | pre | Yes | auto_retry | tmux_transport_failure |
| `pre_gather_command_failed` | pre | Yes | auto_retry | tmux_transport_failure |
| `post_input_mode_blocked` | post | Yes | auto_retry | hook_feedback_interruption |
| `post_process_exit` | post | Yes | auto_retry | tmux_transport_failure |
| `tx_send_skill` | tx | Yes | auto_retry | tmux_transport_failure |
| `tx_load_buffer` | tx | Yes | auto_retry | tmux_transport_failure |
| `tx_paste_buffer` | tx | Yes | auto_retry | tmux_transport_failure |
| `tx_send_enter` | tx | Yes | auto_retry | tmux_transport_failure |
| `tx_load_buffer_codex` | tx | Yes | auto_retry | tmux_transport_failure |
| `tx_paste_buffer_codex` | tx | Yes | auto_retry | tmux_transport_failure |

## Appendix B: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| Delivery Substep Observability (150) | This contract extends the substep taxonomy to cover all phases, not just tmux transport. Substep IDs from 150 are preserved as the `{operation}` suffix in transport-phase codes. |
| Requeue And Classification Accuracy (140) | This contract refines the `delivery_failed:*` classification case into per-code patterns (Section 4.4). Classification categories (busy/ambiguous/invalid) are unchanged. |
| Delivery Failure Lease Ownership (90) | This contract adds `trigger_failure_code` to the lease cleanup audit record. Cleanup behavior (phases, sequence, trap) is unchanged. |
| Failure Classifier (`failure_classifier.py`) | This contract adds a direct code→class lookup table alongside existing keyword matching. Classification classes are unchanged. |
| Input-Ready Terminal (110) | Pre-delivery input mode failures use `post_input_mode_blocked`. The input mode guard mechanism is unchanged. |
| Queue Truth (70) | File disposition (pending/rejected) is determined by retry decision, not by failure code directly. Disposition rules from Contract 140 remain authoritative. |

## Appendix C: DFL-LOG Rule Summary

| Rule | Obligation |
|------|-----------|
| DFL-LOG-1 | Every failure event uses a canonical `failure_code` from Section 2 |
| DFL-LOG-2 | Blocked dispatch audit includes `failure_code` and `failure_class` fields |
| DFL-LOG-3 | Dispatch annotations map to failure codes via translation table |
| DFL-LOG-4 | Classifier accepts failure codes via direct lookup (not only keywords) |
| DFL-LOG-5 | T0 uses `failure_code` (not reason strings) for routing decisions |
| DFL-LOG-6 | 3+ consecutive same-code failures trigger operator escalation |
