# Headless Run Contract and Failure Taxonomy

**Status**: Accepted
**PR**: PR-0
**Gate**: gate_pr0_headless_contract
**Date**: 2026-03-30
**Author**: T3 (Track C Architecture)

This document defines what a headless run is in VNX terms, which state it must emit, how failures are classified, what operators can expect to inspect, and what constitutes proof that headless execution is operationally ready.

All subsequent implementation PRs (PR-1 through PR-4) share this contract as their single source of truth.

For headless review-gate specific evidence requirements, also see:
- [45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/core/45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md)

---

## 1. Headless Run Identity Contract

### 1.1 Definition

A **headless run** is a VNX dispatch execution that:

1. Runs as a CLI subprocess (not inside a tmux pane)
2. Has no interactive operator present during execution
3. Produces durable output artifacts and receipts identical in structure to interactive runs
4. Is governed by the same dispatch state machine as interactive runs

A headless run is **not** an autonomous agent. It is a bounded subprocess with a prompt, a timeout, and a classified exit.

### 1.2 Identity Fields (Required)

Every headless run MUST be uniquely identifiable by these fields, persisted before the subprocess starts:

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `run_id` | `str` | Generated (UUID4) | Unique identifier for this execution instance |
| `dispatch_id` | `str` | Dispatch system | The dispatch this run fulfills |
| `attempt_id` | `str` | `dispatch_attempts` table | Attempt record within the dispatch |
| `target_id` | `str` | `execution_targets` table | Which execution target was selected |
| `target_type` | `str` | Registry | One of `headless_claude_cli`, `headless_codex_cli` |
| `task_class` | `str` | Dispatch bundle | Must be in `{research_structured, docs_synthesis}` |
| `terminal_id` | `str?` | Router | Logical terminal assignment (may be null for pooled targets) |

### 1.3 Identity Invariants

- **I-1**: A `run_id` is assigned exactly once and never reused.
- **I-2**: A run is always linked to exactly one `dispatch_id` and one `attempt_id`.
- **I-3**: A retry of a failed dispatch creates a new `run_id` and a new `attempt_id` under the same `dispatch_id`.
- **I-4**: The `run_id` appears in all log artifacts, coordination events, and receipt entries produced by this run.

---

## 2. Run Lifecycle and State Model

### 2.1 Lifecycle Phases

A headless run progresses through five phases. Each transition is recorded as a `coordination_event`.

```
INITIALIZING -> RUNNING -> COMPLETING -> TERMINAL
                  |
                  v
               FAILING -> TERMINAL
```

Detailed states:

| Phase | State | Description |
|-------|-------|-------------|
| INITIALIZING | `init` | Run identity created, bundle loaded, subprocess not yet spawned |
| RUNNING | `running` | Subprocess is alive; heartbeat and output timestamps updating |
| COMPLETING | `completing` | Subprocess exited successfully; output being persisted |
| FAILING | `failing` | Subprocess exited abnormally or was killed; failure being classified |
| TERMINAL | `succeeded` | Output persisted, receipt emitted, dispatch marked completed |
| TERMINAL | `failed:<class>` | Failure classified, receipt emitted, dispatch marked failed |

### 2.2 State Transitions

```
init ---------> running          (subprocess spawned, PID captured)
running ------> completing       (exit code 0)
running ------> failing          (exit code != 0, timeout, signal, no-output)
completing ---> succeeded        (output persisted, receipt emitted)
failing ------> failed:<class>   (failure classified, receipt emitted)
```

No backward transitions. No implicit retries. Recovery is a new run.

### 2.3 Transition Events

Each transition emits a `coordination_event` with:

```python
{
    "event_type": "headless_run_transition",
    "entity_type": "headless_run",
    "entity_id": run_id,
    "from_state": "<previous>",
    "to_state": "<next>",
    "actor": "headless_adapter",
    "reason": "<human-readable>",
    "metadata_json": {
        "dispatch_id": "...",
        "attempt_id": "...",
        "pid": 12345,            # when available
        "exit_code": null,       # when available
        "failure_class": null,   # when classified
        "duration_seconds": 0.0  # when terminal
    }
}
```

---

## 3. Required Runtime Fields

### 3.1 Headless Run State Record

The run registry must persist these fields for every active or recently-completed run:

| Field | Type | When Set | Description |
|-------|------|----------|-------------|
| `run_id` | `str` | init | Unique run identifier |
| `dispatch_id` | `str` | init | Parent dispatch |
| `attempt_id` | `str` | init | Attempt record |
| `target_id` | `str` | init | Execution target |
| `target_type` | `str` | init | Target type key |
| `task_class` | `str` | init | Dispatch task class |
| `pid` | `int?` | running | OS process ID of the subprocess |
| `pgid` | `int?` | running | Process group ID (if `os.setpgrp` used) |
| `state` | `str` | init | Current lifecycle state |
| `failure_class` | `str?` | failing | Classified failure (see Section 4) |
| `exit_code` | `int?` | completing/failing | Subprocess exit code |
| `started_at` | `ISO8601` | init | When the run record was created |
| `subprocess_started_at` | `ISO8601?` | running | When the subprocess was spawned |
| `heartbeat_at` | `ISO8601?` | running | Last heartbeat timestamp |
| `last_output_at` | `ISO8601?` | running | Last time stdout/stderr produced output |
| `completed_at` | `ISO8601?` | terminal | When the run reached terminal state |
| `duration_seconds` | `float?` | terminal | Wall-clock duration from subprocess start to exit |
| `log_artifact_path` | `str?` | completing/failing | Path to persisted stdout/stderr log |
| `output_artifact_path` | `str?` | completing | Path to structured output file |
| `receipt_id` | `str?` | terminal | Receipt emitted for this run |

For review-gate runs, `output_artifact_path` is not sufficient by itself. A normalized operator-readable markdown report must also exist under `$VNX_DATA_DIR/unified_reports/` (root level, not a subdirectory), and the corresponding `review_gate_result` must reference that report path. This ensures the receipt processor fires a receipt to T0.

### 3.2 Heartbeat Contract

- Heartbeat updates `heartbeat_at` at a configurable interval (default: 30 seconds).
- Heartbeat is driven by the adapter polling subprocess liveness, not by the subprocess itself.
- A run with `heartbeat_at` older than `2 * heartbeat_interval` and no `completed_at` is **stale**.

### 3.3 Output Timestamp Contract

- `last_output_at` updates whenever the subprocess writes to stdout or stderr.
- A run that is `running` with `last_output_at` older than a configurable threshold (default: 120 seconds) is a **no-output hang candidate**.
- This field is the primary signal for detecting hung processes that are alive but unproductive.

### 3.4 CLI Binary and Flags Resolution

This section documents how the headless adapter resolves which CLI binary and flags to use for a given run. Understanding this precedence is critical for operators configuring custom CLI binaries.

#### 3.4.1 Binary Resolution Precedence (3-Tier)

The CLI binary is resolved in strict priority order. The **first match wins**; lower tiers are never consulted once a higher tier provides a value.

| Priority | Source | Scope | Description |
|----------|--------|-------|-------------|
| **1 (highest)** | `HEADLESS_CLI_DEFAULTS[target_type]["binary"]` | Per target type | Hardcoded in `headless_adapter.py`. If the `target_type` key exists in the defaults dict, its `binary` value is used unconditionally. |
| **2** | `VNX_HEADLESS_CLI` environment variable | Global | Read via `headless_cli_binary()`. Only consulted when `target_type` is **not** a key in `HEADLESS_CLI_DEFAULTS`. |
| **3 (lowest)** | Hardcoded default `"claude"` | Global | Returned by `headless_cli_binary()` when `VNX_HEADLESS_CLI` is unset or empty. |

Current `HEADLESS_CLI_DEFAULTS` keys:

| `target_type` | Binary | Flags |
|----------------|--------|-------|
| `headless_claude_cli` | `claude` | `--print --output-format text` |
| `headless_codex_cli` | `codex` | `--quiet` |

#### 3.4.2 Flags Resolution

Flags follow the same lookup as the binary. When `target_type` is found in `HEADLESS_CLI_DEFAULTS`, the associated `args` list is used. When the fallback path is taken (tier 2 or 3 binary), default flags `["--print"]` are applied.

There is **no merging** of flags between tiers. The entire `{binary, args}` tuple comes from one source:
- Either the `HEADLESS_CLI_DEFAULTS` entry (tier 1), or
- The fallback tuple `{headless_cli_binary(), ["--print"]}` (tier 2/3).

#### 3.4.3 Conflict Behavior

**Scenario**: An operator sets `VNX_HEADLESS_CLI=custom_binary` but the dispatch uses a `target_type` that exists in `HEADLESS_CLI_DEFAULTS` (e.g., `headless_claude_cli`).

**Result**: `VNX_HEADLESS_CLI` is **silently ignored**. The tier-1 lookup succeeds, so `headless_cli_binary()` is never called. The run uses `claude` (not `custom_binary`) with `["--print", "--output-format", "text"]`.

```
# Example: operator expects custom_binary, but gets claude
export VNX_HEADLESS_CLI=custom_binary

# Dispatch with target_type = "headless_claude_cli"
# -> HEADLESS_CLI_DEFAULTS["headless_claude_cli"] exists
# -> binary = "claude", args = ["--print", "--output-format", "text"]
# -> VNX_HEADLESS_CLI is never consulted

# Dispatch with target_type = "experimental_runner"
# -> HEADLESS_CLI_DEFAULTS["experimental_runner"] does NOT exist
# -> Fallback: binary = headless_cli_binary() = "custom_binary"
# -> args = ["--print"]
```

#### 3.4.4 Operator Configuration Guide

| Goal | Action |
|------|--------|
| Override CLI binary for a **known** target type (`headless_claude_cli`, `headless_codex_cli`) | Modify `HEADLESS_CLI_DEFAULTS` in `headless_adapter.py` (code change required) |
| Set CLI binary for **unknown/custom** target types | Set `VNX_HEADLESS_CLI` env var — this is the correct and intended use |
| Change the global default when nothing else is configured | Set `VNX_HEADLESS_CLI` env var |
| Verify which binary a run actually used | Check the run's log artifact header or coordination events |

**Key takeaway**: `VNX_HEADLESS_CLI` is a **fallback**, not an override. It has no effect when the dispatch's `target_type` is listed in `HEADLESS_CLI_DEFAULTS`.

---

## 4. Failure Taxonomy

### 4.1 Failure Classes

Every non-successful run MUST be classified into exactly one failure class. These classes drive operator recovery decisions.

| Class | Code | Trigger | Retryable | Operator Action |
|-------|------|---------|-----------|-----------------|
| `success` | `SUCCESS` | Exit code 0, output persisted | N/A | None |
| `tool_failure` | `TOOL_FAIL` | Exit code != 0, stderr indicates tool/API error | Yes (with backoff) | Check tool availability, retry |
| `infra_failure` | `INFRA_FAIL` | Binary not found, permission denied, disk full, OOM | Yes (after fix) | Fix infrastructure, retry |
| `timeout` | `TIMEOUT` | Subprocess exceeded `VNX_HEADLESS_TIMEOUT` | Yes (with longer timeout) | Review timeout config, check prompt complexity |
| `no_output_hang` | `NO_OUTPUT` | Process alive but no output for > threshold | Yes (after investigation) | Inspect prompt, check upstream dependencies |
| `interrupted` | `INTERRUPTED` | SIGINT, SIGTERM, SIGHUP received | Yes | Check what sent the signal, retry |
| `prompt_error` | `PROMPT_ERR` | Exit code != 0, stderr indicates prompt/input issue | No (needs prompt fix) | Fix prompt or dispatch bundle |
| `unknown` | `UNKNOWN` | None of the above patterns match | Manual review | Inspect logs, classify manually |

### 4.2 Classification Rules

Classification is applied in order (first match wins):

1. **Exit code 0** -> `success`
2. **Subprocess killed by timeout** -> `timeout`
3. **No output detected for > threshold while running** -> `no_output_hang`
4. **Signal-terminated** (SIGINT=2, SIGTERM=15, SIGHUP=1) -> `interrupted`
5. **Binary not found / permission denied** -> `infra_failure`
6. **Stderr contains tool/API error patterns** -> `tool_failure`
7. **Stderr contains prompt/input error patterns** -> `prompt_error`
8. **Exit code != 0 with no matching pattern** -> `unknown`

### 4.3 Classification Evidence

Each failure classification MUST record:

```python
{
    "failure_class": "TIMEOUT",
    "exit_code": -9,
    "signal": 9,
    "stderr_tail": "<last 500 chars of stderr>",
    "classification_reason": "subprocess.TimeoutExpired after 600s",
    "retryable": True,
    "operator_hint": "Consider increasing VNX_HEADLESS_TIMEOUT or simplifying the prompt"
}
```

This evidence is stored in the run state record and included in the receipt.

---

## 5. Minimum Observability Contract

### 5.1 What Operators MUST Be Able to Do

These are non-negotiable capabilities. A headless run implementation that cannot satisfy all of these is not ready for use.

| # | Capability | Mechanism |
|---|-----------|-----------|
| O-1 | **List active headless runs** | Query run registry for `state = running` |
| O-2 | **See how long a run has been executing** | `now - subprocess_started_at` |
| O-3 | **Detect a hung run** | `now - last_output_at > threshold` while `state = running` |
| O-4 | **Detect a stale run** | `now - heartbeat_at > 2 * interval` while `state = running` |
| O-5 | **See why a run failed** | `failure_class` + `classification_reason` in run state |
| O-6 | **Read the full output of a completed run** | `log_artifact_path` on disk |
| O-7 | **Read the stderr of a failed run** | `log_artifact_path` includes both stdout and stderr |
| O-8 | **Trace a run back to its dispatch** | `dispatch_id` + `attempt_id` in run state |
| O-9 | **Trace a run forward to its receipt** | `receipt_id` in run state |
| O-10 | **Kill a stuck run** | PID/PGID in run state, operator can send signal |

### 5.2 What Operators MUST NOT Need to Do

| # | Anti-pattern | Why |
|---|-------------|-----|
| A-1 | Grep through raw log files to find a run | Run registry provides structured query |
| A-2 | Guess whether a process is still alive | Heartbeat + PID provide definitive answer |
| A-3 | Manually classify why a run failed | Failure taxonomy classifies automatically |
| A-4 | Correlate PIDs to dispatches by timestamp guessing | `run_id` links them directly |
| A-5 | Check multiple directories for output | Single `log_artifact_path` pointer |

### 5.3 Log Artifact Requirements

Every headless run MUST produce a log artifact at `log_artifact_path` containing:

1. **Header**: run_id, dispatch_id, target_type, started_at
2. **Stdout**: Complete captured stdout
3. **Stderr**: Complete captured stderr (clearly delimited)
4. **Footer**: exit_code, failure_class (if applicable), duration_seconds, completed_at

Format: plain text with clear section delimiters. Not JSON (must be human-readable without tooling).

---

## 6. Receipt Integration

### 6.1 Receipt Events

A headless run emits the following receipt events through the standard `append_receipt` pipeline:

| Event | When | Required Fields |
|-------|------|----------------|
| `task_started` | Subprocess spawned | `run_id`, `dispatch_id`, `target_type`, `task_class` |
| `task_complete` | Run succeeded | `run_id`, `dispatch_id`, `duration_seconds`, `output_path` |
| `task_failed` | Run failed | `run_id`, `dispatch_id`, `failure_class`, `duration_seconds` |
| `task_timeout` | Run timed out | `run_id`, `dispatch_id`, `timeout_seconds`, `duration_seconds` |

### 6.2 Provenance Chain

The receipt provenance chain for a headless run:

```
dispatch_created -> dispatch_claimed -> task_started -> task_complete/task_failed
```

Every link in this chain MUST reference the same `dispatch_id` and `trace_token`.

---

## 7. Burn-In Proof Criteria

### 7.1 Definition

Burn-in proof is evidence that headless execution works reliably under realistic conditions, not just in unit tests. The burn-in is not passed until all criteria below are satisfied with real execution evidence.

### 7.2 Measurable Criteria

| # | Criterion | Evidence Required | Pass Threshold |
|---|----------|-------------------|----------------|
| B-1 | **Success path works end-to-end** | At least 3 successful headless runs with different prompts | 3 runs, all succeeded, output correct |
| B-2 | **Timeout is detected and classified** | At least 1 run that exceeds timeout, classified as `TIMEOUT` | Classification correct, receipt emitted |
| B-3 | **No-output hang is detected** | At least 1 run that hangs, detected within 2x threshold | Detection within window, not false positive |
| B-4 | **Interrupted run is classified** | At least 1 run killed by signal, classified as `INTERRUPTED` | Classification correct, no orphaned artifacts |
| B-5 | **Failure classification is accurate** | Review all failure-class assignments across burn-in runs | Zero misclassifications |
| B-6 | **Operator inspection works** | Operator lists active runs, reads logs, traces to dispatch | All O-1 through O-10 demonstrated |
| B-7 | **Receipts are provenance-complete** | Every burn-in run has a complete provenance chain | No broken links in chain |
| B-8 | **No regression to interactive flows** | Interactive tmux dispatches still work after headless enablement | At least 2 interactive runs succeed |
| B-9 | **Recovery from failure is actionable** | Operator can determine next action from failure class alone | Operator survey or review confirms |
| B-10 | **Heartbeat detects stale processes** | At least 1 case where heartbeat correctly identifies stale run | Stale detection within 2x interval |

### 7.3 Burn-In Evidence Format

Each criterion must be documented with:

```markdown
### B-N: <Criterion name>
- **Run ID(s)**: <run_id values>
- **Dispatch ID(s)**: <dispatch_id values>
- **Evidence**: <what was observed>
- **Pass/Fail**: <result>
- **Notes**: <any caveats>
```

### 7.4 Certification Decision

- All 10 criteria at Pass -> **Certified for operator use**
- Any criterion at Fail -> **Not certified**; must document residual risk and remediation plan
- Certification is documented in the PR-4 burn-in report

---

## 8. Compatibility Constraints

| # | Constraint | Rationale |
|---|-----------|-----------|
| C-1 | Headless runs use the same `dispatch_attempts` table | No parallel state systems |
| C-2 | Headless runs use the same `coordination_events` table | Unified audit trail |
| C-3 | Headless runs use the same receipt pipeline | Provenance stays consistent |
| C-4 | Interactive tmux flows must not be modified by this feature | G-R5 from FEATURE_PLAN |
| C-5 | All new fields are additive (no schema migrations that break existing data) | Safe rollback |
| C-6 | Headless can be disabled by setting `VNX_HEADLESS_ENABLED=0` | Instant rollback |

---

## 9. What This Contract Does NOT Define

These are explicitly deferred to implementation PRs:

- **PR-1**: Run registry storage format (SQLite table vs JSON files), heartbeat implementation mechanics, specific SQL schema additions
- **PR-2**: Log artifact format details, stderr pattern matching for classification, tee implementation
- **PR-3**: Operator CLI commands, inspection view layout, recovery hook integration
- **PR-4**: Burn-in test execution, certification report template

This contract defines the **what** and **why**. Implementation PRs define the **how**.

---

## Appendix A: Governance Traceability

| FEATURE_PLAN Rule | Contract Section |
|-------------------|-----------------|
| G-R1 (receipt-producing) | Section 6 |
| G-R2 (operator inspection) | Section 5 |
| G-R3 (real burn-in evidence) | Section 7 |
| G-R4 (no hidden retry) | Section 2.2 (no backward transitions) |
| G-R5 (no tmux regression) | Section 8, C-4 |
| A-R1 (durable identity) | Section 1 |
| A-R2 (heartbeat + output timestamps) | Section 3 |
| A-R3 (persisted log artifacts) | Section 5.3 |
| A-R4 (classified exits) | Section 4 |
| A-R5 (process group aware) | Section 3.1 (`pgid` field) |
| A-R6 (simple but not opaque) | Section 5 (observability contract) |
| A-R7 (interactive compatibility) | Section 8, C-4 |

## Appendix B: Failure Class Decision Tree

```
Exit code 0?
  YES -> SUCCESS
  NO  -> Was subprocess killed by timeout?
           YES -> TIMEOUT
           NO  -> Was output silent for > threshold?
                    YES -> NO_OUTPUT
                    NO  -> Was process signal-terminated?
                             YES -> INTERRUPTED
                             NO  -> Is binary missing / permission denied?
                                      YES -> INFRA_FAIL
                                      NO  -> Does stderr match tool/API error?
                                               YES -> TOOL_FAIL
                                               NO  -> Does stderr match prompt error?
                                                        YES -> PROMPT_ERR
                                                        NO  -> UNKNOWN
```
