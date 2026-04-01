# PR-Scoped Gate Evidence Contract

**Status**: Canonical
**Feature**: Gate Evidence Accuracy And PR-Scoped Lookup
**PR**: PR-0
**Gate**: `gate_pr0_gate_evidence_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document defines how gate evidence is scoped to PRs, how dispatch provenance is ordered, how gate results are validated, and what GitHub evidence is required before merge closure. All downstream PRs (PR-1 and PR-2) implement against this contract.

---

## 1. Why This Exists

### 1.1 The Problem

The remediation merge (commit `15a97d1`) fixed the most dangerous false-green closure paths: OR-based gate lookup, missing report_path validation, and Codex verdict field blindness. But residual accuracy gaps remain:

**Nondeterministic provenance**: When multiple dispatches exist for one PR (e.g., a rejected dispatch followed by a re-dispatch), the filesystem iteration order of `iterdir()` is not guaranteed. Different runs may produce different dispatch orderings, causing provenance to be nondeterministic.

**Incomplete PR scoping**: The contract-based lookup path (`{pr_slug}-{gate}-contract.json`) does not verify `pr_id` inside the JSON — it trusts the filename. If a file is manually renamed or copied, it could satisfy a gate for the wrong PR.

**Verdict-only results**: A gate result with `status: pass` but no `report_path` can exist if the report was never written (e.g., headless process crash after verdict but before report). The remediation fix catches this at `record_result()` time, but stale results written before the fix may still exist on disk.

**No GitHub linkage requirement**: A VNX PR can currently be closed locally without ever creating a real GitHub PR or having green CI checks. This decouples VNX governance from the actual code integration path.

### 1.2 The Fix

Make every gate-evidence operation:
1. **PR-scoped**: Gate results are always matched by both PR identifier AND gate name (AND logic).
2. **Deterministic**: Dispatch provenance ordering is sorted, not iteration-order-dependent.
3. **Report-enforced**: Every gate result with a terminal verdict must carry a valid `report_path` to an existing file.
4. **GitHub-linked**: Merge closure requires a real GitHub PR with green required CI checks.

### 1.3 Relationship To Existing Contracts

| Contract | Relationship |
|----------|-------------|
| Headless Review Evidence (45) | This contract extends doc 45 with PR-scoping rules, provenance ordering, and GitHub CI linkage. Doc 45 defines the evidence surfaces; this contract defines accuracy invariants across those surfaces. |
| Queue Truth (70) | Dispatch provenance ordering interacts with queue reconciliation (Priority 2 source). This contract defines how to order multiple dispatches for the same PR. |
| Projection Consistency (120) | Orthogonal — doc 120 covers state surface consistency; this contract covers evidence attribution accuracy. |

---

## 2. Dispatch Provenance Ordering

### 2.1 The Problem

When multiple dispatches exist for the same PR (common after rejection and re-dispatch), the provenance chain must be deterministic. The filesystem does not guarantee `iterdir()` ordering.

### 2.2 Ordering Rule

**GE-1 (Gate Evidence Rule 1)**: When multiple dispatches exist for the same PR, they MUST be ordered by the dispatch timestamp embedded in the dispatch ID, ascending.

The dispatch ID format is: `YYYYMMDD-HHMMSS-{descriptor}-{track}`

The sort key is the `YYYYMMDD-HHMMSS` prefix, lexicographic ascending. This produces chronological order because the timestamp format is ISO-sortable.

### 2.3 Implementation Constraint

All code paths that enumerate dispatches for provenance derivation MUST use `sorted()` on the dispatch file list, not raw `iterdir()` or `glob()` output. The sort key MUST be the filename (which starts with the timestamp).

```python
# CORRECT: deterministic ordering
for f in sorted(active_dir.iterdir()):
    ...

# INCORRECT: nondeterministic ordering
for f in active_dir.iterdir():
    ...
```

### 2.4 Multi-Dispatch Resolution

When a PR has dispatches in multiple states (e.g., one in `rejected/`, one in `active/`), the state priority from Queue Truth Contract (70) EC-3 applies:

```
active > completed > pending/staging > rejected
```

Within each state, dispatches are ordered by timestamp (GE-1). The most recent dispatch in the highest-priority state is the **effective dispatch** for provenance purposes.

---

## 3. PR-Scoped Gate Lookup

### 3.1 The Problem

Gate results are stored as JSON files in `.vnx-data/state/review_gates/results/`. Multiple PRs may share the same gate names (e.g., every PR requires `gemini_review`). Without PR scoping, a gate result for PR-0 could satisfy PR-1's closure check.

### 3.2 Lookup Rules

**GE-2 (Gate Evidence Rule 2)**: Gate result lookup MUST match on BOTH `pr_id` AND `gate` name (AND logic). A match on either field alone is insufficient.

**GE-3 (Gate Evidence Rule 3)**: When `branch` is available, gate result lookup MUST also reject results from a different branch. This prevents stale results from prior features from satisfying current-feature closure.

### 3.3 Lookup Sequence

The gate result lookup proceeds in this order:

```
1. Try contract-based path: {results_dir}/{pr_slug}-{gate}-contract.json
   - Read JSON
   - Verify pr_id field matches (not just filename)
   - Verify branch matches (if branch parameter provided)
   - If valid, return result

2. Fall back to legacy scan: {results_dir}/*-{gate}*.json
   - For each matching file:
     - Read JSON
     - Verify data["pr_id"] == pr_id AND data["gate"] == gate
     - Verify branch matches (if provided)
     - If valid, return result

3. If no result found, return None (gate not satisfied)
```

### 3.4 Forbidden Lookup Behaviors

| # | Behavior | Why Forbidden |
|---|----------|--------------|
| **FL-1** | Match on `gate` name alone (OR logic) | Cross-PR evidence attribution |
| **FL-2** | Match on `pr_id` alone | Wrong gate could satisfy a different required gate |
| **FL-3** | Trust filename without verifying JSON content | Renamed/copied files could satisfy wrong PR |
| **FL-4** | Accept result from different branch when branch is known | Stale prior-feature results |
| **FL-5** | Accept result with `status: queued` or `status: requested` as evidence | Non-terminal state is not completion evidence |

### 3.5 Contract-Path `pr_id` Verification

**GE-4 (Gate Evidence Rule 4)**: Even when using the contract-based lookup path (`{pr_slug}-{gate}-contract.json`), the `pr_id` field inside the JSON MUST be verified against the requested PR. Filename trust alone is insufficient.

This addresses the gap where a file could be manually renamed or generated with the wrong filename.

---

## 4. Report-Path Enforcement

### 4.1 The Problem

A gate result JSON can carry a `status: pass` verdict without a `report_path` if:
- The headless process crashed after producing a verdict but before writing the report.
- The result was written before the remediation fix that enforces report_path at write time.
- The report file was deleted after the result was written.

### 4.2 Enforcement Rules

**GE-5 (Gate Evidence Rule 5)**: Every gate result with a terminal verdict (`pass`, `fail`, `approve`, `reject`) MUST carry a non-empty `report_path` field. A result with a terminal verdict and empty or missing `report_path` MUST be rejected as incomplete evidence.

**GE-6 (Gate Evidence Rule 6)**: The file at `report_path` MUST exist on disk at closure verification time. A result with a `report_path` that points to a non-existent file MUST be rejected as stale evidence.

### 4.3 Verdict Field Resolution

Gate results use two different fields for their verdict depending on the provider:

| Provider | Verdict Field | Values |
|----------|--------------|--------|
| Gemini | `status` | `pass`, `fail`, `blocked`, `not_configured`, `configured_dry_run` |
| Codex | `verdict` | `approve`, `reject`, `pass`, `fail` |
| Claude GitHub | `status` | `pass`, `fail`, `not_configured` |

**GE-7 (Gate Evidence Rule 7)**: Verdict resolution MUST check both `status` and `verdict` fields. The effective verdict is: `result.get("status") or result.get("verdict")`. This ensures Codex results (which use `verdict`) are subject to the same report_path enforcement as Gemini results (which use `status`).

### 4.4 Terminal Verdicts

The following verdicts are **terminal** (evidence-bearing) and require report_path:

| Verdict | Meaning | Report Required |
|---------|---------|----------------|
| `pass` | Gate passed | **Yes** |
| `fail` | Gate failed | **Yes** |
| `approve` | Codex approved | **Yes** |
| `reject` | Codex rejected | **Yes** |

The following verdicts are **non-terminal** and do not require report_path:

| Verdict | Meaning | Report Required |
|---------|---------|----------------|
| `not_configured` | Gate not in review stack | No |
| `configured_dry_run` | Dry run mode | No |
| `blocked` | Blocked by dependency | No |
| `queued` | Requested but not started | No |

---

## 5. Cross-PR Evidence Attribution Prevention

### 5.1 The Problem

Without strict PR scoping, evidence from one PR can silently satisfy another PR's gate requirements. This is the most dangerous evidence integrity failure because it creates a false-green closure path.

### 5.2 Attribution Rules

**GE-8 (Gate Evidence Rule 8)**: A gate result MUST NOT satisfy a gate requirement for a PR other than the one identified in the result's `pr_id` field. This is enforced by the AND-logic lookup (GE-2).

**GE-9 (Gate Evidence Rule 9)**: The `contract_hash` field in a gate result MUST match the `content_hash` of the review contract for the PR being verified. A hash mismatch means the review was conducted against different content than the current PR state.

### 5.3 Cross-PR Attribution Detection

The closure verifier MUST check for and flag the following scenarios:

| Scenario | Detection | Action |
|----------|-----------|--------|
| Result `pr_id` does not match requested PR | AND-logic lookup returns None | Gate unsatisfied — block closure |
| Result `contract_hash` does not match current contract | Hash comparison after lookup | Gate evidence stale — block closure |
| Result `branch` does not match current branch | Branch rejection in lookup | Gate evidence from wrong feature — block closure |
| Multiple results exist for same PR+gate | First valid match wins | Log advisory — not an error if results agree |

---

## 6. GitHub PR And CI Linkage

### 6.1 The Problem

VNX governance tracks PRs locally (PR-0, PR-1, etc.) but the actual code integration happens via GitHub PRs. Without linking these, a VNX PR can be "closed" locally while the code is never merged or CI is failing.

### 6.2 GitHub PR Requirement

**GE-10 (Gate Evidence Rule 10)**: Before merge closure, a real GitHub PR MUST exist for the feature branch. The PR is looked up by branch name via `gh pr list --head {branch}`.

### 6.3 GitHub PR State Requirements

| Closure Mode | Required PR State | Required Merge State |
|-------------|-------------------|---------------------|
| `pre_merge` | `OPEN` | `CLEAN` |
| `post_merge` | `MERGED` | N/A (merge already happened) |

### 6.4 CI Check Requirements

**GE-11 (Gate Evidence Rule 11)**: Before merge closure (`pre_merge` mode), all required GitHub CI checks MUST be green. A check is green when:
- `status` = `COMPLETED`
- `conclusion` = `SUCCESS`
- `__typename` = `CheckRun`

If no `statusCheckRollup` exists (no CI configured), the check MUST fail — absence of CI is not a pass.

### 6.5 GitHub PR Number Derivation

The GitHub PR number is derived from the branch lookup, not from gate result files. Gate result files may carry a `pr_number` field, but it is informational — the authoritative GitHub PR number comes from `gh pr list`.

### 6.6 Local-Only Closure Blocking

**GE-12 (Gate Evidence Rule 12)**: If no GitHub PR exists for the feature branch, merge closure MUST fail with an explicit diagnostic. Local-only closure (VNX gates pass but no GitHub PR exists) is a governance gap that MUST be blocked.

---

## 7. Closure Verification Integration

### 7.1 Gate Evidence Check Sequence

During closure verification, gate evidence is checked in this order for each required gate in the review stack:

```
1. Look up gate result using PR-scoped AND-logic (GE-2, GE-3)
2. IF no result found:
     Gate unsatisfied — FAIL
3. Resolve verdict (GE-7): status || verdict
4. IF verdict is non-terminal (queued, not_configured, etc.):
     Gate not completed — FAIL (for required gates)
5. Verify report_path is non-empty (GE-5)
6. Verify report_path file exists on disk (GE-6)
7. Verify contract_hash matches current review contract (GE-9)
8. IF all checks pass:
     Gate satisfied — PASS
```

### 7.2 Merge Readiness Check Sequence

After all gate evidence is verified:

```
1. Look up GitHub PR for feature branch (GE-10)
2. IF no PR found:
     Merge blocked — FAIL (GE-12)
3. Check PR state matches closure mode (Section 6.3)
4. Check all CI checks are green (GE-11)
5. IF all checks pass:
     Merge ready — PASS
```

---

## 8. Audit Evidence

### 8.1 Closure Verification Output

Every closure verification run produces a list of `CheckResult` entries with:

| Field | Content |
|-------|---------|
| `check_name` | What was checked (e.g., `gate_gemini_review`, `merge_state`, `github_checks`) |
| `status` | `PASS`, `FAIL`, or `SKIP` |
| `detail` | Human-readable explanation of the result |

### 8.2 Evidence Trail

The gate evidence trail for a PR consists of:

1. **Review contract**: `review_contract.md` with `content_hash` — defines what was promised.
2. **Gate request records**: `.vnx-data/state/review_gates/requests/` — proves T0 asked for the gate.
3. **Gate result records**: `.vnx-data/state/review_gates/results/` — proves the gate ran and produced a verdict.
4. **Normalized reports**: `.vnx-data/unified_reports/` — proves the review produced readable output.
5. **Closure verification output**: Proves all evidence was checked and passed.

---

## 9. Implementation Constraints For PR-1

PR-1 implements against this contract. The following constraints are binding:

1. **GE-1 (deterministic ordering)**: All `iterdir()` calls in provenance derivation must be replaced with `sorted()`.
2. **GE-2 (AND logic)**: Gate lookup must require both `pr_id` and `gate` match. The remediation fix already does this for the legacy path — PR-1 must also verify `pr_id` inside contract-path JSON (GE-4).
3. **GE-5, GE-6 (report_path enforcement)**: Closure verifier must reject results with empty `report_path` or missing report file for terminal verdicts.
4. **GE-7 (verdict resolution)**: Both `status` and `verdict` fields must be checked.
5. **GE-10, GE-11, GE-12 (GitHub linkage)**: Merge readiness must require GitHub PR existence and green CI checks. Local-only closure must be explicitly blocked.
6. **All fixes must be testable**: Each GE-* rule must have at least one test that exercises the success and failure path.

---

## 10. Verification Criteria For PR-2

PR-2 certifies this contract. The following must be demonstrated:

1. Multi-dispatch PR scenario produces identical provenance across repeated runs (GE-1).
2. Cross-PR gate evidence is never attributed to the wrong PR (GE-2, GE-8).
3. Contract-path lookup verifies `pr_id` inside JSON, not just filename (GE-4).
4. Verdict-only results without `report_path` fail validation (GE-5).
5. Results with non-existent `report_path` file fail validation (GE-6).
6. Codex `verdict` field is resolved correctly (GE-7).
7. GitHub PR absence blocks merge closure (GE-12).
8. CI check failures block merge closure (GE-11).

---

## Appendix A: Gate Evidence Rule Quick Reference

| Rule | Description | Enforcement Point |
|------|-------------|-------------------|
| GE-1 | Deterministic dispatch ordering by timestamp | Provenance derivation |
| GE-2 | PR-scoped gate lookup (AND logic) | `_find_gate_result()` |
| GE-3 | Branch rejection for stale results | `_find_gate_result()` |
| GE-4 | Verify `pr_id` inside contract-path JSON | `_find_gate_result()` |
| GE-5 | Non-empty `report_path` for terminal verdicts | Closure verification |
| GE-6 | `report_path` file must exist on disk | Closure verification |
| GE-7 | Resolve verdict from `status` OR `verdict` field | Closure verification |
| GE-8 | No cross-PR evidence attribution | AND-logic lookup (GE-2) |
| GE-9 | `contract_hash` must match review contract | Closure verification |
| GE-10 | GitHub PR must exist for feature branch | Merge readiness check |
| GE-11 | All required CI checks must be green | Merge readiness check |
| GE-12 | Local-only closure is blocked | Merge readiness check |

## Appendix B: Relationship To Other Contracts

| Contract | Relationship |
|----------|-------------|
| Headless Review Evidence (45) | This contract extends doc 45 with PR-scoping, provenance ordering, and GitHub CI rules. Doc 45 defines surfaces; this contract defines accuracy invariants. |
| Queue Truth (70) | Dispatch ordering (GE-1) uses the same state-priority rule as EC-3 in doc 70. |
| Projection Consistency (120) | Orthogonal — doc 120 covers state surface consistency; this contract covers evidence attribution accuracy. |
| Terminal Exclusivity (80) | Not directly related — terminal availability vs evidence accuracy. |
| Input-Ready Terminal (110) | Not directly related — pane input mode vs evidence accuracy. |
