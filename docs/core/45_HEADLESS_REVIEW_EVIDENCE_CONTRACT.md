# Headless Review Evidence Contract

**Status**: Canonical
**Purpose**: Define the required request, artifact, report, and receipt surfaces for headless review jobs so T0 can enforce review-gate evidence deterministically.

This contract applies to headless review providers used for governance evidence, including:
- `gemini_review`
- `codex_gate`
- `claude_github_optional`

It does not replace the generic headless execution contract in [HEADLESS_RUN_CONTRACT.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/HEADLESS_RUN_CONTRACT.md). It narrows that contract for review-gate runs that T0 depends on for closure decisions.

## 1. Why This Exists

Headless review jobs are only useful to VNX if they produce evidence T0 can reason about.

The minimum acceptable outcome is not:
- a prose comment
- a transient CLI output blob
- a gate result with no durable report path

The minimum acceptable outcome is:
1. a review-gate request linked to a PR and review contract
2. a durable operator-readable report
3. a structured gate result receipt
4. deterministic linkage between request, report, receipt, and closure decision

If any of these are missing, closure must remain blocked.

## 2. Required Inputs For Every Headless Review Job

Each headless review job MUST be bound to all of the following:

| Field | Required | Description |
|---|---|---|
| `gate` | Yes | One of `gemini_review`, `codex_gate`, `claude_github_optional` |
| `pr_id` | Yes | Canonical PR id such as `PR-3` |
| `branch` | Yes | Branch under review |
| `review_contract_path` | Yes | Path to the canonical review contract input |
| `contract_hash` | Yes | Hash of the contract content used to render the request |
| `review_mode` | Yes | `per_pr` or `final` |
| `risk_class` | Yes | `low`, `medium`, or `high` |
| `changed_files` | Yes | Changed files considered during review |
| `requested_by` | Yes | Normally `T0` |
| `report_path` | Yes | Expected normalized markdown report path |

### Input Invariants

1. T0 MUST NOT request a headless review job without a review contract.
2. T0 MUST carry the `contract_hash` through request, result, and closure review.
3. The `report_path` MUST be known before the review is considered closure-relevant.

## 3. Required Output Surfaces

Every headless review job MUST produce all three output surfaces below.

### 3.1 Request Record

The request record lives under:

`$VNX_STATE_DIR/review_gates/requests/`

It is the durable proof that T0 asked for the gate.

### 3.2 Normalized Markdown Report

Every headless review job MUST produce an operator-readable markdown report under:

`$VNX_DATA_DIR/unified_reports/`

This is the same directory that interactive terminal workers write to. The receipt processor scans this directory (root level, non-recursive) to fire receipts to T0. Headless reports MUST be written here — not in a subdirectory — so they are visible to the receipt pipeline.

Recommended filename pattern:

`YYYYMMDD-HHMMSS-HEADLESS-<gate>-<pr-id>.md`

Minimum report sections:
- gate identity
- PR identity
- contract hash
- review scope
- summary verdict
- blocking findings
- advisory findings
- required reruns
- residual risk
- artifact linkage

### 3.3 Structured Gate Result

The structured result lives under:

`$VNX_STATE_DIR/review_gates/results/`

It is the machine-readable closure input that T0 and the closure verifier use.

## 4. Required Gate Result Fields

Every `review_gate_result` relevant to closure MUST include:

| Field | Required | Description |
|---|---|---|
| `gate` | Yes | Gate name |
| `pr_id` | Yes | Canonical PR id |
| `branch` | Yes | Branch under review |
| `status` | Yes | `pass`, `fail`, `blocked`, `not_configured`, or another explicit provider state |
| `summary` | Yes | Short verdict summary |
| `contract_hash` | Yes | Must match the request contract hash |
| `report_path` | Yes | Path to normalized markdown report |
| `blocking_findings` | Yes | Array, may be empty |
| `advisory_findings` | Yes | Array, may be empty |
| `blocking_count` | Yes | Integer |
| `advisory_count` | Yes | Integer |
| `required_reruns` | Yes | Array, may be empty |
| `residual_risk` | Yes | String or empty string |
| `recorded_at` | Yes | ISO timestamp |

### Explicit-State Rule

Optional gates are allowed to be intentionally absent, but they may never be silently absent.

So:
- `claude_github_optional` may be `not_configured` or `configured_dry_run`
- but it may not simply have no state at all

## 5. T0 Enforcement Rules

T0 MUST enforce all of the following before completing a PR or declaring closure-ready state:

1. If the review stack requires a gate, a gate request record must exist.
2. If the review stack requires a gate, a gate result must exist.
3. The gate result must carry the same `contract_hash` as the review contract.
4. The gate result must contain a valid `report_path`.
5. The normalized markdown report must exist at `report_path`.
6. All blocking findings must be resolved or explicitly re-reviewed before closure.
7. Missing, contradictory, or ambiguous review evidence blocks closure.

### Closure Blocking Examples

Closure MUST fail if any of the following is true:
- receipt exists but `report_path` is missing
- report exists but no `review_gate_result` exists
- request/result `contract_hash` does not match the review contract
- gate says `pass` but unresolved blocking findings remain
- optional gate has no explicit state

## 6. Relationship To Unified Reports

Headless review jobs are not exempt from the normal VNX evidence flow.

They may keep raw subprocess logs elsewhere, but they still MUST project a normalized markdown report into:

`$VNX_DATA_DIR/unified_reports/`

This keeps headless review evidence inside the same operator-visible report surface as interactive worker reports and ensures the receipt processor fires receipts to T0.

## 7. Trial And Feature Plan Requirements

Any feature plan that depends on headless review evidence MUST explicitly define:
- which PRs require which gates
- which gates are policy-required versus optional
- the expected `report_path` convention
- how T0 should behave on missing or contradictory evidence

If a feature plan omits this, T0 must treat the plan as under-specified and refuse closure claims based on incomplete review evidence.

## 8. Operator Reading Order

When validating a headless review outcome, read in this order:

1. review contract
2. gate request record
3. normalized markdown report
4. structured gate result
5. closure verifier result

This preserves the distinction between:
- what was requested
- what the reviewer concluded
- what the governance system accepted
