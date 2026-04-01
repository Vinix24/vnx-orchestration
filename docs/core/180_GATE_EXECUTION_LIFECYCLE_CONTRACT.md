# Gate Execution Lifecycle Contract

**Status**: Canonical
**Feature**: Deterministic Headless Gate Execution
**PR**: PR-0
**Gate**: `gate_pr0_gate_execution_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document defines the contract for deterministic gate execution: the state machine governing gate lifecycle, the `not_executable` terminal state with required reason, bounded execution timeouts with stall detection, skip-rationale audit records for unavailable providers, and artifact stability requirements. All downstream PRs (PR-1 and PR-2) implement against this contract.

---

## 1. Why This Exists

### 1.1 The Problem

Three gate execution defects cause repeated manual operator intervention:

**Defect 1: Queued Without Runner**

Gate requests transition to `queued` but no automatic runner executes them. The request record proves T0 asked, but nothing proves the gate started. T0 must passively poll or manually trigger execution. The `queued` state is ambiguous — it could mean "about to run", "no runner configured", or "runner crashed before starting". Doc 45 Section 3.4 identifies this ambiguity but does not define the state machine that resolves it.

**Defect 2: Unavailable Provider Without Skip Record**

When Codex is unavailable (`VNX_CODEX_HEADLESS_ENABLED=0` or `codex` not in PATH), the gate request records `status: blocked` with `reason: codex_headless_not_available`. But this is a request-time classification, not an execution outcome. There is no structured skip-rationale audit record that T0 can use to distinguish "provider unavailable at request time" from "provider available but execution failed." The Gemini `blocked` status has the same ambiguity.

**Defect 3: Stall Without Timeout**

Gemini CLI has known stdout flush and stall issues (OI-048). A stalled gate produces no output, no error, and no timeout — it hangs indefinitely. The `headless_adapter.py` defines `HeadlessTimeoutError` and `headless_timeout()` (default 600s), but these apply to the general headless adapter, not to review gate execution specifically. No gate-type-specific timeout or stall detection exists.

### 1.2 The Fix

1. **State machine**: Define a deterministic state machine with explicit transitions. `queued` is not a terminal state — it must transition to `executing`, `not_executable`, or `failed` within a bounded time.
2. **Not_executable state**: Define `not_executable` as a terminal state with a required `reason` field. This replaces the ambiguous `blocked` status for known-unavailable providers.
3. **Bounded timeout**: Define per-gate-type execution timeout with stall detection. A stalled gate is killed and transitions to `failed` with structured evidence.
4. **Skip-rationale audit**: Define a structured audit record for unavailable providers that T0 can reason on deterministically.
5. **Artifact stability**: Define all-or-nothing materialization — report, result, and contract_hash must all exist or all be absent.

---

## 2. Gate Execution State Machine

### 2.1 States

| State | Terminal | Meaning |
|-------|----------|---------|
| `requested` | No | T0 created the gate request. Execution has not started. |
| `executing` | No | Gate runner has started subprocess. Execution is in progress. |
| `completed` | Yes | Gate execution finished successfully. Result and report exist. |
| `failed` | Yes | Gate execution finished with error or timeout. Structured failure recorded. |
| `not_executable` | Yes | Gate cannot be executed. Provider unavailable, runner missing, or configuration prevents execution. Required `reason` field. |

### 2.2 Valid Transitions

```
requested → executing        (runner starts subprocess)
requested → not_executable   (provider unavailable or runner missing)
requested → failed           (pre-execution failure: config error, binary not found)

executing → completed        (subprocess exit 0, artifacts valid)
executing → failed           (subprocess exit != 0, timeout, stall, or artifact failure)

completed → (terminal)
failed → (terminal)
not_executable → (terminal)
```

**GATE-1 (State Machine Rule)**: Every gate request MUST transition from `requested` to a terminal state (`completed`, `failed`, or `not_executable`) within the bounded execution window defined in Section 4. The `requested` state MUST NOT persist indefinitely.

**GATE-2 (No Ambiguous Queue)**: The `queued` status currently used in request records (e.g., `"status": "queued"` in `_request_gemini()`) is an alias for `requested`. PR-1 SHOULD normalize to `requested` for consistency. During the transition period, both `queued` and `requested` are treated as equivalent non-terminal states.

**GATE-3 (Executing State Record)**: When the gate runner starts execution, it MUST update the request record with:

```json
{
  "status": "executing",
  "started_at": "2026-04-01T14:45:00Z",
  "runner_pid": 12345
}
```

This enables stall detection (Section 4) and distinguishes "not yet started" from "started but not finished."

### 2.3 State Persistence

Gate state is persisted in the request record file at `$VNX_STATE_DIR/review_gates/requests/`. The result record at `$VNX_STATE_DIR/review_gates/results/` is written only on terminal state. This means:

- `requested`: request file exists, no result file
- `executing`: request file updated with `started_at`, no result file yet
- `completed`: request file exists, result file exists with terminal verdict
- `failed`: request file exists, result file exists with `status: failed`
- `not_executable`: request file exists with `status: not_executable` and `reason`, result file written with same status

---

## 3. Not_Executable State

### 3.1 Definition

A gate is `not_executable` when the system can determine at request time or pre-execution time that the gate cannot be executed. This is a permanent condition for the current execution attempt — it will not resolve by waiting.

### 3.2 Required Reason Codes

| Reason Code | Condition | Provider |
|-------------|-----------|----------|
| `provider_not_installed` | CLI binary not found in PATH | Any |
| `provider_disabled` | Feature flag explicitly disables provider | Any |
| `provider_not_configured` | Required configuration missing (API key, auth) | Any |
| `runner_not_available` | No gate runner script or process is available to execute | Any |
| `unsupported_gate_type` | Gate type is not recognized by the runner | Any |
| `contract_missing` | Review contract could not be generated (no changed files, no FEATURE_PLAN) | Any |

### 3.3 Not_Executable Record

**GATE-4 (Not_Executable Rule)**: When a gate transitions to `not_executable`, the following MUST be recorded:

In the request record:
```json
{
  "status": "not_executable",
  "reason": "provider_not_installed",
  "reason_detail": "codex binary not found in PATH",
  "resolved_at": "2026-04-01T14:45:00Z"
}
```

In the result record:
```json
{
  "gate": "codex_gate",
  "pr_id": "PR-0",
  "status": "not_executable",
  "reason": "provider_not_installed",
  "reason_detail": "codex binary not found in PATH",
  "summary": "Codex gate not executable: CLI binary not installed.",
  "contract_hash": "",
  "report_path": "",
  "blocking_findings": [],
  "advisory_findings": [],
  "required_reruns": [],
  "residual_risk": "Gate evidence not available. Compensating evidence required.",
  "recorded_at": "2026-04-01T14:45:00Z"
}
```

### 3.4 Not_Executable vs Blocked

The current `blocked` status is ambiguous — it could mean "temporarily blocked" (retryable) or "permanently unavailable" (terminal). This contract introduces `not_executable` as the terminal state:

| Status | Terminal | Meaning |
|--------|----------|---------|
| `blocked` | No (deprecated for gates) | Temporarily blocked — may resolve. PR-1 should migrate to `requested` + retry. |
| `not_executable` | Yes | Permanently unavailable for this attempt. Requires reason code. |

**GATE-5 (No Ambiguous Blocked)**: PR-1 MUST NOT use `blocked` as a terminal gate status. Blocked conditions must be resolved to either `not_executable` (permanent) or retried to `executing` (transient).

---

## 4. Bounded Execution Timeout

### 4.1 Timeout Per Gate Type

| Gate Type | Default Timeout | Env Override | Rationale |
|-----------|----------------|--------------|-----------|
| `gemini_review` | 300s (5 min) | `VNX_GEMINI_GATE_TIMEOUT` | Gemini CLI is fast but has stall risk (OI-048) |
| `codex_gate` | 600s (10 min) | `VNX_CODEX_GATE_TIMEOUT` | Codex analysis may be slow on large diffs |
| `claude_github_optional` | 300s (5 min) | `VNX_CLAUDE_GITHUB_GATE_TIMEOUT` | GitHub API-bound, should be fast |

**GATE-6 (Timeout Rule)**: Every gate execution MUST have a bounded timeout. The runner MUST kill the subprocess if it exceeds the timeout. The gate transitions to `failed` with `reason: timeout`.

### 4.2 Stall Detection

A stall is when the subprocess is alive but producing no output. This is distinct from timeout — a stall can occur within the first minute of a 5-minute timeout window.

**GATE-7 (Stall Detection Rule)**: The gate runner MUST monitor subprocess output (stdout + stderr). If no output is produced for a configurable stall threshold, the subprocess MUST be killed. Default stall thresholds:

| Gate Type | Stall Threshold | Env Override |
|-----------|----------------|--------------|
| `gemini_review` | 60s | `VNX_GEMINI_STALL_THRESHOLD` |
| `codex_gate` | 120s | `VNX_CODEX_STALL_THRESHOLD` |
| `claude_github_optional` | 60s | `VNX_CLAUDE_GITHUB_STALL_THRESHOLD` |

### 4.3 Timeout/Stall Failure Record

When a gate is killed due to timeout or stall, the failure MUST be recorded as:

```json
{
  "gate": "gemini_review",
  "pr_id": "PR-0",
  "status": "failed",
  "reason": "timeout",
  "reason_detail": "Subprocess exceeded 300s timeout",
  "duration_seconds": 300,
  "partial_output_lines": 0,
  "runner_pid": 12345,
  "killed_at": "2026-04-01T14:50:00Z",
  "summary": "Gate execution timed out after 300s with no output.",
  "contract_hash": "abc123",
  "report_path": "",
  "blocking_findings": [],
  "advisory_findings": [],
  "required_reruns": ["gemini_review"],
  "residual_risk": "Gate timed out. Re-run required.",
  "recorded_at": "2026-04-01T14:50:01Z"
}
```

For stall detection:
```json
{
  "reason": "stall",
  "reason_detail": "No output for 60s (stall threshold exceeded)",
  "duration_seconds": 85,
  "partial_output_lines": 3
}
```

**GATE-8 (No Silent Hang)**: A gate execution MUST NOT hang indefinitely. Every execution path terminates in a terminal state within the bounded timeout window. Silent hangs are the primary defect this contract prevents.

---

## 5. Skip-Rationale Audit

### 5.1 When Skip Records Are Required

A skip-rationale record is required when a gate transitions to `not_executable`. This record is the evidence that T0 and the operator use to understand why the gate was skipped and what compensating action is needed.

### 5.2 Skip-Rationale Record Schema

```json
{
  "event_type": "gate_skip_rationale",
  "gate": "codex_gate",
  "pr_id": "PR-0",
  "reason": "provider_not_installed",
  "reason_detail": "codex binary not found in PATH. VNX_CODEX_HEADLESS_ENABLED=0.",
  "provider_check": {
    "binary_name": "codex",
    "binary_found": false,
    "env_flag": "VNX_CODEX_HEADLESS_ENABLED",
    "env_value": "0"
  },
  "compensating_action": "Manual Codex review or operator override required.",
  "timestamp": "2026-04-01T14:45:00Z"
}
```

**GATE-9 (Skip Audit Rule)**: Every `not_executable` transition MUST produce a skip-rationale record in the NDJSON audit trail at `$VNX_STATE_DIR/gate_execution_audit.ndjson`. This record is append-only and durable.

### 5.3 Skip-Rationale For T0

T0 uses skip-rationale records to determine compensating action:

| Reason Code | T0 Action |
|-------------|-----------|
| `provider_not_installed` | Request operator to install provider, or accept compensating evidence |
| `provider_disabled` | Request operator to enable feature flag, or accept compensating evidence |
| `provider_not_configured` | Request operator to configure provider (API key, auth) |
| `runner_not_available` | Request operator to start gate runner, or manually execute gate |
| `unsupported_gate_type` | Escalate — gate type may need implementation |
| `contract_missing` | Investigate — review contract generation may have a bug |

### 5.4 Relationship To Closure

**GATE-10 (Skip Blocks Closure)**: A required gate in `not_executable` state BLOCKS closure unless T0 explicitly accepts compensating evidence. The compensating evidence acceptance must be recorded in the gate result with:

```json
{
  "status": "not_executable",
  "compensating_evidence_accepted": true,
  "compensating_evidence_reason": "Codex CLI not available. Manual operator review confirms no security issues."
}
```

Optional gates in `not_executable` do NOT block closure but must still produce the skip-rationale record.

---

## 6. Artifact Stability

### 6.1 The Three Artifacts

Every gate execution that reaches `completed` MUST produce three artifacts:

| Artifact | Location | Content |
|----------|----------|---------|
| **Normalized Report** | `$VNX_DATA_DIR/unified_reports/{ts}-HEADLESS-{gate}-{pr}.md` | Operator-readable markdown with verdict, findings, scope |
| **Result Record** | `$VNX_STATE_DIR/review_gates/results/{pr_slug}-{gate}-contract.json` | Machine-readable JSON with verdict, contract_hash, report_path |
| **Contract Hash** | Inside result record, `contract_hash` field | SHA-256 prefix of the review contract content |

### 6.2 All-Or-Nothing Materialization

**GATE-11 (Artifact Atomicity Rule)**: The three artifacts MUST materialize as an atomic unit. Either all three exist and are consistent, or none of them exist.

Materialization sequence:
```
1. Write normalized report to unified_reports/
2. Verify report file exists and is non-empty
3. Compute contract_hash from review contract
4. Write result record with report_path and contract_hash
5. Verify result record is valid JSON with all required fields
```

If any step fails:
```
- Do NOT write partial result record
- Do NOT leave orphan report without result
- Transition gate to "failed" with reason "artifact_materialization_failed"
- Record which step failed in reason_detail
```

### 6.3 Artifact Consistency Checks

**GATE-12 (Artifact Consistency Rule)**: Before a gate is considered `completed`, the following consistency checks MUST pass:

| Check | Rule | Failure Action |
|-------|------|----------------|
| Report exists at `report_path` | File must exist on disk | Transition to `failed` |
| Report is non-empty | File size > 0 bytes | Transition to `failed` |
| `contract_hash` matches review contract | Recompute hash and compare | Transition to `failed` |
| `report_path` in result matches actual report | Paths must be identical | Transition to `failed` |
| Result JSON has all required fields (per Doc 45 Section 4) | Schema validation | Transition to `failed` |

### 6.4 Stale Artifact Detection

A result record is stale if:
- Its `contract_hash` does not match the current review contract (content changed since review)
- Its `report_path` points to a file that no longer exists (report was deleted)
- Its `recorded_at` timestamp predates a branch force-push (code changed after review)

**GATE-13 (Stale Detection Rule)**: The closure verifier MUST reject stale artifacts. Stale detection is performed by recomputing the contract hash at closure time and comparing to the result's `contract_hash`. A mismatch means the review was conducted against different content.

---

## 7. Non-Goals

| # | Non-Goal | Rationale |
|---|----------|-----------|
| NG-1 | Adding new gate types beyond gemini_review, codex_gate, claude_github_optional | FEATURE_PLAN scopes this out explicitly |
| NG-2 | Changing the review contract or evidence verification rules | Doc 45 and Doc 130 govern evidence rules; this contract governs execution lifecycle |
| NG-3 | Modifying PR completion criteria | Those remain per existing contracts |
| NG-4 | Automatic retry of failed gates | Failed gates require explicit re-request. Automatic retry is a future concern. |
| NG-5 | Dashboard or alerting for gate status | Structured data enables dashboards; building them is out of scope |
| NG-6 | Resolving OI-048 (Gemini stdout flush) | PR-1 addresses the symptom with stall detection; root cause fix is in Gemini CLI |

---

## 8. Implementation Constraints For PR-1

1. **GATE-1**: Implement gate runner that transitions `requested` gates to `executing` or `not_executable` within bounded time.
2. **GATE-3**: Update request record with `started_at` and `runner_pid` when execution begins.
3. **GATE-4**: Write `not_executable` result record with reason code when provider is unavailable.
4. **GATE-6, GATE-7**: Implement per-gate-type timeout and stall detection with subprocess kill.
5. **GATE-8**: Ensure no execution path can hang indefinitely.
6. **GATE-9**: Write skip-rationale NDJSON records for every `not_executable` transition.
7. **GATE-11, GATE-12**: Implement atomic artifact materialization with consistency checks.
8. **GATE-2**: Normalize `queued` to `requested` in request records (backward-compatible).

### 8.1 Files To Modify

| File | Change | Contract Rule |
|------|--------|---------------|
| `scripts/review_gate_manager.py` | Add `execute_gate()` method, update request records with execution state | GATE-1, GATE-3 |
| `scripts/lib/headless_adapter.py` | Add gate-specific timeout/stall config | GATE-6, GATE-7 |
| `scripts/review_gate_manager.py` | Write `not_executable` result records with reason | GATE-4, GATE-5 |
| `scripts/review_gate_manager.py` | Atomic artifact write with consistency checks | GATE-11, GATE-12 |
| New: `scripts/gate_runner.py` or integrated into review_gate_manager | Gate execution entry point with subprocess management | GATE-1, GATE-8 |
| `$VNX_STATE_DIR/gate_execution_audit.ndjson` | New audit trail file for skip-rationale records | GATE-9 |

### 8.2 Test Requirements

| Test | Validates |
|------|-----------|
| Gate request transitions to `executing` when runner starts | GATE-1, GATE-3 |
| Gate request transitions to `not_executable` when provider missing | GATE-4, GATE-5 |
| Gate killed after timeout produces `failed` result with `reason: timeout` | GATE-6, GATE-8 |
| Gate killed after stall produces `failed` result with `reason: stall` | GATE-7, GATE-8 |
| Skip-rationale NDJSON record written for `not_executable` | GATE-9 |
| Artifact write is atomic: partial failure produces no result record | GATE-11 |
| Stale contract_hash is detected and rejected | GATE-13 |
| `requested` does not persist beyond timeout window | GATE-1 |

---

## Appendix A: GATE Rule Summary

| Rule | Obligation |
|------|-----------|
| GATE-1 | Every request transitions to terminal state within bounded time |
| GATE-2 | `queued` is alias for `requested` — normalize in PR-1 |
| GATE-3 | `executing` state records `started_at` and `runner_pid` |
| GATE-4 | `not_executable` requires structured reason code and detail |
| GATE-5 | `blocked` is deprecated as terminal gate status |
| GATE-6 | Per-gate-type execution timeout with subprocess kill |
| GATE-7 | Stall detection: no output for N seconds → kill |
| GATE-8 | No execution path hangs indefinitely |
| GATE-9 | Skip-rationale NDJSON audit for every `not_executable` |
| GATE-10 | Required `not_executable` gate blocks closure unless compensating evidence accepted |
| GATE-11 | Artifact materialization is all-or-nothing |
| GATE-12 | Artifact consistency checks before `completed` |
| GATE-13 | Stale artifact detection via contract_hash recomputation |

## Appendix B: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| Headless Review Evidence (45) | This contract extends Doc 45 with a formal state machine (Section 2), execution lifecycle (Sections 3-4), and artifact stability rules (Section 6). Doc 45 defines evidence surfaces; this contract defines execution behavior. |
| PR-Scoped Gate Evidence (130) | This contract is orthogonal — Doc 130 defines lookup and scoping rules; this contract defines execution lifecycle. Both inform closure decisions. |
| Delivery Failure Lease (90) | Not directly related. Gate execution uses subprocess, not tmux transport. No lease involvement. |
| Fail-Closed Bootstrap (170) | Gate execution requires VNX env vars (runner needs state dirs). Bootstrap preconditions from Doc 170 apply. |

## Appendix C: Current Code Gaps (For PR-1 Implementers)

| File | Line | Gap | Contract Rule |
|------|------|-----|---------------|
| `review_gate_manager.py` | 189 | `status: "queued"` has no automatic transition to `executing` | GATE-1 |
| `review_gate_manager.py` | 189,203 | `blocked` used as terminal status for unavailable providers | GATE-5 |
| `review_gate_manager.py` | N/A | No `execute_gate()` method — gates are not auto-executed | GATE-1 |
| `headless_adapter.py` | 82-87 | Generic timeout (600s), no per-gate-type config | GATE-6 |
| `headless_adapter.py` | N/A | No stall detection (output monitoring) | GATE-7 |
| `review_gate_manager.py` | 500-585 | `record_result()` does not verify artifact atomicity | GATE-11 |
| N/A | N/A | No `gate_execution_audit.ndjson` audit trail | GATE-9 |
| N/A | N/A | No gate runner entry point (subprocess management) | GATE-1, GATE-8 |
