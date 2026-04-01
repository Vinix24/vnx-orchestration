# Delivery Substep Observability Contract

**Status**: Canonical
**Feature**: Delivery Substep Observability
**PR**: PR-0
**Gate**: `gate_pr0_delivery_substep_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document defines the canonical taxonomy for delivery substeps, the required log fields per substep failure, and the integration with existing rejection annotation. All downstream PRs (PR-1 and PR-2) implement against this contract.

---

## 1. Why This Exists

### 1.1 The Problem

When a dispatch delivery fails during the tmux transport phase, the current error messages are generic:

```
V8 ERROR: Failed to send skill command to terminal T2:0.1
V8 ERROR: Failed to load prompt to tmux buffer (3 attempts)
V8 ERROR: Failed to paste prompt to terminal T2:0.1
V8 ERROR: Failed to send Enter to terminal T2:0.1
```

These messages identify the failing operation but lack a **canonical substep identifier**. The `rc_release_on_failure()` call uses a single reason: `"tmux delivery failed"` or `"tmux Enter failed"` — collapsing four distinct failure modes into two buckets. When reviewing blocked dispatch audit trails or investigating stranded terminals, the operator cannot determine which specific transport step failed without reading the full log.

### 1.2 The Fix

Give every delivery substep a named identifier. Use that identifier in:
1. The `rc_release_on_failure()` reason string.
2. The `log_structured_failure()` audit event.
3. The rejection annotation on the dispatch file (when applicable).

This is a pure observability improvement. It does not change delivery behavior, retry logic, or classification semantics.

---

## 2. Delivery Substep Taxonomy

### 2.1 Substep Identifiers

The V8 hybrid delivery pipeline has two provider-specific paths. Each path decomposes into named substeps:

#### Claude Code / Gemini Path (send-keys + paste-buffer)

| Substep ID | Operation | tmux Call | Lines (approx) |
|-----------|-----------|-----------|-----------------|
| `send_skill` | Type skill command via send-keys | `tmux send-keys -t {pane} -l "{skill_command}"` | 1653 |
| `load_buffer` | Load instruction payload into tmux buffer | `tmux_load_buffer_safe "{prompt}"` | 1663 |
| `paste_buffer` | Paste buffer content into terminal | `tmux paste-buffer -t {pane}` | 1670 |
| `send_enter` | Submit the complete input with Enter | `tmux send-keys -t {pane} Enter` | 1690 |

#### Codex Path (combined paste-buffer)

| Substep ID | Operation | tmux Call | Lines (approx) |
|-----------|-----------|-----------|-----------------|
| `load_buffer_codex` | Load combined skill+instruction into buffer | `tmux_load_buffer_safe "{skill_command}{prompt}"` | 1638 |
| `paste_buffer_codex` | Paste combined buffer into terminal | `tmux paste-buffer -t {pane}` | 1644 |
| `send_enter` | Submit the complete input with Enter | `tmux send-keys -t {pane} Enter` | 1690 |

### 2.2 Pre-Delivery Substeps

These substeps execute before the tmux transport and have their own existing identifiers:

| Substep ID | Operation | Existing Audit Code |
|-----------|-----------|-------------------|
| `input_mode_guard` | Check pane_in_mode and recover | `input_mode_blocked` (doc 110) |
| `mode_control` | Force normal mode, clear context, model switch | `mode_configuration_failed` |
| `worktree_cd` | Change terminal directory to worktree | `worktree_cd_failed` (non-fatal) |
| `input_clear` | Clear readline buffer (C-u) | N/A (best-effort, no audit) |

Pre-delivery substeps are already audited via their existing mechanisms. This contract adds substep identifiers only for the tmux transport phase (Section 2.1).

---

## 3. Required Log Fields Per Substep Failure

### 3.1 Structured Failure Event

When a delivery substep fails, a `log_structured_failure()` event MUST be emitted with these fields:

| Field | Source | Example |
|-------|--------|---------|
| `code` | Substep ID from Section 2.1 | `"send_skill"` |
| `message` | Human-readable description | `"Failed to send skill command to terminal"` |
| `details` | Key-value context | `"pane=T2:0.1 dispatch=d-001 provider=claude_code skill=/architect attempts=3"` |

### 3.2 Release-On-Failure Reason

When `rc_release_on_failure()` is called after a substep failure, the reason string MUST include the substep ID:

```
Current:  "tmux delivery failed"
Required: "delivery_failed:send_skill"
          "delivery_failed:load_buffer"
          "delivery_failed:paste_buffer"
          "delivery_failed:send_enter"
```

Format: `delivery_failed:{substep_id}`

This preserves the existing `delivery_failed` prefix (which the Delivery Failure Lease Contract, doc 90, uses for Phase 2 classification) while adding the substep identifier.

### 3.3 Dispatcher Log Message

The existing `log "V8 ERROR: ..."` messages MUST include the substep ID:

```
Current:  "V8 ERROR: Failed to send skill command to terminal T2:0.1"
Required: "V8 ERROR: [send_skill] Failed to send skill command to terminal T2:0.1"
```

Format: `V8 ERROR: [{substep_id}] {description}`

---

## 4. Rejection Annotation Integration

### 4.1 Current Format

When a delivery failure occurs, the dispatch file currently receives no delivery-specific annotation. The generic `[REJECTED: Dispatch failed during execution]` marker (from `process_dispatches()`) does not indicate which substep failed.

### 4.2 Required Format

**DS-1 (Delivery Substep Rule 1)**: When a delivery substep fails, the dispatch file MUST NOT receive a generic rejection annotation. Instead, the failure is recorded in the audit trail (Section 3) and the dispatch returns to `pending/` (per RC-3 in the Requeue And Classification Accuracy Contract, doc 140).

Delivery substep failures are **requeueable** — the tmux transport may succeed on the next attempt. The dispatch file should not be annotated with `[REJECTED]` for a transient tmux failure.

### 4.3 Annotation Exception

If the dispatcher explicitly determines that a delivery failure is permanent (e.g., pane is dead and will not recover), it MAY annotate with:

```
[REJECTED: delivery_failed:{substep_id} — {reason}]
```

This is the only path from a substep failure to a `[REJECTED]` annotation. The substep ID MUST be included.

---

## 5. Audit Compatibility

### 5.1 Existing Audit Format

The dispatcher uses two audit mechanisms:
1. `log_structured_failure(code, message, details)` — emits JSON to stdout with `event: "failure"`.
2. `emit_blocked_dispatch_audit(dispatch_id, terminal_id, reason, event_type)` — emits NDJSON to `blocked_dispatch_audit.ndjson`.

### 5.2 Compatibility Requirements

**DS-2 (Delivery Substep Rule 2)**: Substep failure events MUST use `log_structured_failure()` with the substep ID as the `code` field. This preserves compatibility with existing log parsing that matches on `"event": "failure"` and extracts the `code` field.

**DS-3 (Delivery Substep Rule 3)**: The `emit_blocked_dispatch_audit()` event for delivery failures MUST include the substep ID in the `block_reason` field. Format: `delivery_failed:{substep_id}`. This preserves compatibility with `_classify_blocked_dispatch()` which currently does not match `delivery_failed:*` — a new case must be added (classified as `ambiguous true` since delivery transport failures are transient).

### 5.3 Classification Update

A new case in `_classify_blocked_dispatch()` is required:

```bash
delivery_failed:*)
    echo "ambiguous true" ;;
```

This classifies all delivery substep failures as requeueable, consistent with the existing behavior where delivery failures release the lease but do not permanently reject the dispatch.

---

## 6. Non-Goals

| # | Non-Goal | Rationale |
|---|----------|-----------|
| NG-1 | Retry per-substep (e.g., retry paste without retrying send_skill) | The existing `tmux_retry 3` mechanism handles retries at the tmux call level. Per-substep retry orchestration adds complexity without clear benefit. |
| NG-2 | Substep timing/latency measurement | Performance instrumentation is a separate concern. This contract adds failure identification only. |
| NG-3 | Provider-specific substep variants beyond Codex | The two provider paths (Claude/Gemini vs Codex) are the only paths. No new providers are in scope. |
| NG-4 | Dashboard visualization of substep failures | Dashboard changes are out of scope. The audit trail is sufficient for operator investigation. |
| NG-5 | Modifying tmux_retry or tmux_send_best_effort | These are transport-level utilities. Substep identification wraps them, not replaces them. |

---

## 7. Implementation Constraints For PR-1

1. **Substep IDs from Section 2.1 are canonical**. PR-1 must use these exact identifiers.
2. **`log_structured_failure()`** must be called with substep ID as `code` for every delivery failure.
3. **`rc_release_on_failure()`** reason must use `delivery_failed:{substep_id}` format.
4. **`_classify_blocked_dispatch()`** must add `delivery_failed:*) echo "ambiguous true"` case.
5. **No `[REJECTED]` annotation** for delivery substep failures (they are requeueable per RC-3).
6. **Existing log messages** must include `[{substep_id}]` prefix in the error description.
7. **Successful delivery paths must not be affected** — no changes to the success flow.

---

## Appendix A: Substep Quick Reference

| Substep ID | Provider | Operation | Requeueable |
|-----------|----------|-----------|-------------|
| `send_skill` | Claude/Gemini | send-keys -l skill command | Yes |
| `load_buffer` | Claude/Gemini | Load instruction to tmux buffer | Yes |
| `paste_buffer` | Claude/Gemini | Paste buffer to terminal | Yes |
| `load_buffer_codex` | Codex | Load combined skill+instruction to buffer | Yes |
| `paste_buffer_codex` | Codex | Paste combined buffer to terminal | Yes |
| `send_enter` | All | send-keys Enter (submission) | Yes |

## Appendix B: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| Delivery Failure Lease (90) | Substep failures occur in Phase 2 (during delivery). Lease cleanup follows doc 90. The reason string now includes the substep ID. |
| Requeue And Classification (140) | Delivery substep failures are `ambiguous true` (requeueable). The `delivery_failed:*` classification case must be added per DS-3. |
| Input-Ready Terminal (110) | `input_mode_guard` is a pre-delivery substep with its own audit mechanism. Not changed by this contract. |
