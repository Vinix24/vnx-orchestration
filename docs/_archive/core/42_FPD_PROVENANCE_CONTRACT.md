# FP-D Provenance Contract — Trace Tokens, Bidirectional Linkage, And Enforcement

**Feature**: FP-D — Safe Autonomy, Governance Envelopes, And End-To-End Provenance
**PR**: PR-0
**Status**: Canonical
**Purpose**: Defines the end-to-end provenance chain across dispatch, receipt, commit, and PR/featureplan. Specifies the trace token format, bidirectional linkage rules, CLI-agnostic enforcement paths, and legacy ref acceptance policy.

---

## 1. Provenance Chain Overview

VNX provenance connects four entities into a bidirectional chain:

```
Dispatch ←──→ Receipt ←──→ Commit ←──→ PR / Feature Plan
    │              │            │              │
    └── dispatch_id ──────────────────────────┘
                   └── git_ref ──┘
                                 └── pr_number ─┘
```

**Forward direction** (work -> evidence): Dispatch creates work -> receipt records completion -> commit captures code -> PR aggregates commits.

**Reverse direction** (audit -> origin): PR references commits -> commits carry trace tokens -> receipts link to dispatches -> dispatches reference feature plan PRs.

### 1.1 Provenance Invariants

1. **Every committed change traces to a dispatch**: Commits must carry a trace token linking to the originating dispatch. (G-R5)
2. **Every receipt traces to a dispatch**: Receipts carry `dispatch_id` or equivalent linkage. (G-R6)
3. **Every PR traces to feature plan PRs**: PR descriptions or metadata reference the feature plan PR numbers. (G-R7)
4. **Bidirectional**: Given any one entity, the full chain can be reconstructed in both directions. (G-R7)
5. **CLI-agnostic**: Provenance enforcement does not depend on a specific AI CLI tool. (G-R8)
6. **Receipts are primary evidence**: Git metadata is a pointer; receipts carry the full context. (G-R6)

---

## 2. Trace Token Specification

### 2.1 Preferred Format

```
Dispatch-ID: <dispatch_id>
```

Where `<dispatch_id>` follows the existing VNX dispatch ID format: `YYYYMMDD-HHMMSS-<slug>-<track>`

**Example**:
```
Dispatch-ID: 20260329-180606-autonomy-policy-matrix--escala-C
```

### 2.2 Placement Rules

The trace token appears in the **commit message body** (not the subject line), on its own line:

```
feat(governance): define autonomy policy matrix

Implement canonical policy classes and action classification
for FP-D governance envelopes.

Dispatch-ID: 20260329-180606-autonomy-policy-matrix--escala-C
```

### 2.3 Accepted Legacy Formats

During the transition period, these legacy formats are accepted as valid trace references. New commits should use the preferred format.

| Format | Example | Status |
|---|---|---|
| `Dispatch-ID: <id>` | `Dispatch-ID: 20260329-180606-...` | **Preferred** |
| `dispatch:<id>` (inline) | `dispatch:20260329-180606-...` | Accepted legacy |
| `PR-N` reference in subject | `feat(scope): PR-3 description` | Accepted legacy (links to feature plan PR, not dispatch) |
| `FP-X` reference in subject | `fix(scope): close FP-A gaps` | Accepted legacy (links to feature plan) |

### 2.4 Trace Token Regex

For validation, the canonical trace token matches:

```regex
^Dispatch-ID:\s+(\S+)$
```

Legacy formats match:

```regex
dispatch:(\S+)
\bPR-(\d+)\b
\bFP-([A-Z])\b
```

### 2.5 Trace Token Rules

1. **One preferred token per commit**: A commit should carry at most one `Dispatch-ID:` line. Multiple dispatch references in one commit indicate scope creep.
2. **Legacy accepted, not encouraged**: Validation passes on legacy formats but tooling should suggest the preferred format.
3. **Absence is detectable**: Missing trace tokens produce a validation warning (shadow mode) or error (enforcement mode).
4. **Token must resolve**: The dispatch ID in the trace token should correspond to a real dispatch. Unresolvable tokens produce a validation warning.

---

## 3. Bidirectional Linkage Contract

### 3.1 Dispatch -> Receipt Linkage

| Source Field | Target Field | Mechanism |
|---|---|---|
| `dispatch.dispatch_id` | `receipt.cmd_id` or `receipt.dispatch_id` | Receipt processor copies dispatch ID into receipt at creation time |
| `dispatch.dispatch_id` | `coordination_events.entity_id` | Events reference the dispatch ID as entity_id |

**Receipt schema extension** (PR-2):

```json
{
  "dispatch_id": "<dispatch_id>",
  "trace_token": "Dispatch-ID: <dispatch_id>"
}
```

The `dispatch_id` field is added to receipts where it is not already present via `cmd_id`. Both fields are accepted during the transition; `dispatch_id` is preferred for new receipts.

### 3.2 Receipt -> Commit Linkage

| Source Field | Target Field | Mechanism |
|---|---|---|
| `receipt.provenance.git_ref` | Commit SHA | Receipt captures HEAD SHA at completion time |
| `receipt.provenance.branch` | Branch name | Receipt captures active branch |
| Commit message `Dispatch-ID:` | `receipt.dispatch_id` | Shared dispatch ID links commit to receipt |

**Reconstruction**: Given a receipt, find its commit via `git_ref`. Given a commit, find its receipt by matching `Dispatch-ID` in the commit message against receipt `dispatch_id`.

### 3.3 Commit -> PR Linkage

| Source Field | Target Field | Mechanism |
|---|---|---|
| Commit SHA | PR commits list | Git/GitHub native: PR contains commits |
| Commit message `Dispatch-ID:` | PR body or metadata | PR description aggregates dispatch references |
| PR number | Feature plan PR queue | `PR_QUEUE.md` maps PR numbers to feature plan PRs |

### 3.4 PR -> Feature Plan Linkage

| Source Field | Target Field | Mechanism |
|---|---|---|
| PR title/body `PR-N` reference | `FEATURE_PLAN.md` PR section | Feature plan defines PR scope |
| PR branch name | Feature plan branch field | Feature plan specifies the branch |
| `PR_QUEUE.md` entries | Feature plan PR list | Queue tracks PR progress against plan |

### 3.5 Full Chain Reconstruction

Given any entity, the full chain can be reconstructed:

| Starting From | Forward Path | Reverse Path |
|---|---|---|
| **Dispatch** | dispatch_id -> receipts (filter by dispatch_id) -> git_ref -> commits -> PR | N/A (dispatch is origin) |
| **Receipt** | receipt.dispatch_id -> dispatch; receipt.git_ref -> commit -> PR | receipt.dispatch_id -> dispatch |
| **Commit** | commit SHA -> PR (git native) | commit message Dispatch-ID -> receipt -> dispatch |
| **PR** | N/A (PR is terminal) | PR commits -> commit messages -> Dispatch-IDs -> receipts -> dispatches |

---

## 4. Enforcement Paths

### 4.1 Local Git Hooks (Assistance Layer)

Local hooks assist developers and agents in maintaining provenance. They are **not** the primary enforcement mechanism (G-R8, A-R6).

#### prepare-commit-msg Hook

- **Purpose**: Auto-inject `Dispatch-ID:` line when a dispatch context is available
- **Behavior**: If `VNX_CURRENT_DISPATCH_ID` environment variable is set, append `Dispatch-ID: $VNX_CURRENT_DISPATCH_ID` to the commit message template
- **Override**: Developer can edit or remove the line before committing
- **CLI-agnostic**: Works with any Git client; reads environment variable, not CLI-specific state

#### commit-msg Hook

- **Purpose**: Validate trace token presence in commit message
- **Shadow mode** (`VNX_PROVENANCE_ENFORCEMENT=0`): Log warning if no trace token found; allow commit
- **Enforcement mode** (`VNX_PROVENANCE_ENFORCEMENT=1`): Block commit if no trace token found (preferred or legacy format)
- **Bypass**: `--no-verify` bypasses the hook. This is logged as a governance event if VNX is running. Bypass is an explicit override, not a silent skip.

### 4.2 CI / Server-Side Validation (Durable Backstop)

CI validation is the durable enforcement layer that survives local hook bypasses (A-R4, A-R6).

#### CI Trace Token Check

- **Trigger**: Runs on every PR and push to protected branches
- **Behavior**: Scan commit messages in the PR for trace tokens (preferred + legacy formats)
- **Output**: Report listing commits with and without valid trace tokens
- **Enforcement**: Configurable — warning-only or blocking, controlled by repository settings
- **Implementation**: Shell script or CI step that runs `git log --format=%B` and applies trace token regex

#### CI Provenance Completeness Check

- **Trigger**: Runs on PR ready for review
- **Behavior**: For each `Dispatch-ID` found in commits, verify a matching receipt exists in the receipt log
- **Output**: Report listing dispatch IDs with and without receipt evidence
- **Enforcement**: Warning-only initially; blocking after FP-D cutover

### 4.3 Receipt-Side Validation (Runtime Layer)

Receipt processing validates provenance at creation time.

- **Receipt creation**: Receipt processor verifies `dispatch_id` is set and matches a known dispatch
- **Git state capture**: Receipt captures `git_ref`, `branch`, `is_dirty` at completion time
- **Missing linkage detection**: Receipt with no `dispatch_id` or no `git_ref` emits a `provenance_gap` coordination event

---

## 5. Provenance Gap Handling

### 5.1 Gap Types

| Gap Type | Description | Severity | Detection Point |
|---|---|---|---|
| `missing_trace_token` | Commit has no trace token in message | Warning (shadow) / Error (enforced) | commit-msg hook, CI check |
| `unresolvable_token` | Trace token references non-existent dispatch | Warning | CI check, receipt validation |
| `missing_receipt` | Dispatch has no corresponding receipt | Warning | Provenance verification (PR-4) |
| `missing_git_ref` | Receipt has no git_ref in provenance | Warning | Receipt validation |
| `orphan_commit` | Commit on feature branch with no dispatch linkage | Info | CI check |
| `broken_chain` | Forward or reverse reconstruction fails at any link | Error | Provenance audit (PR-4) |

### 5.2 Gap Events

Every detected provenance gap emits a coordination event:

```json
{
  "event_type": "provenance_gap",
  "entity_type": "commit | receipt | dispatch | pr",
  "entity_id": "<sha | receipt_id | dispatch_id | pr_number>",
  "actor": "hook | ci | receipt_processor | verifier",
  "reason": "<gap description>",
  "metadata_json": {
    "gap_type": "<gap type from 5.1>",
    "severity": "info | warning | error",
    "enforcement_mode": "shadow | enforced",
    "trace_token_found": "<token or null>",
    "expected_dispatch_id": "<id or null>"
  }
}
```

### 5.3 Legacy Transition Rules

During the transition from pre-FP-D to FP-D provenance:

1. **Existing commits without trace tokens**: Not retroactively flagged. Gap detection starts from the FP-D cutover point.
2. **Legacy format acceptance**: `PR-N` and `FP-X` references in existing commits satisfy provenance for pre-FP-D work.
3. **Mixed branches**: A branch may contain both pre-FP-D commits (legacy refs) and FP-D commits (Dispatch-ID). Both are valid.
4. **Cutover boundary**: The first commit after `VNX_PROVENANCE_ENFORCEMENT=1` marks the enforcement boundary. Commits before this point are exempt from trace token enforcement.

---

## 6. Receipt Schema Extensions

### 6.1 New Fields (PR-2)

| Field | Type | Required | Description |
|---|---|---|---|
| `dispatch_id` | TEXT | Recommended | Explicit dispatch ID (preferred over cmd_id for provenance) |
| `trace_token` | TEXT | Optional | Full trace token string as it appears in the commit message |
| `pr_number` | INTEGER | Optional | GitHub PR number if known at receipt time |
| `feature_plan_pr` | TEXT | Optional | Feature plan PR reference (e.g., "PR-0") |

### 6.2 Backward Compatibility

- `cmd_id` remains accepted as a dispatch identifier for existing receipts
- Receipt readers should check `dispatch_id` first, then fall back to `cmd_id`
- New receipts should populate both `dispatch_id` and `cmd_id` during transition

### 6.3 Enhanced Receipt Example

```json
{
  "event": "task_complete",
  "run_id": "20260329-180606-C-policy",
  "track": "C",
  "phase": "1.0",
  "gate": "review",
  "task_id": "C-autonomy-policy-matrix",
  "cmd_id": "20260329-180606-autonomy-policy-matrix--escala-C",
  "dispatch_id": "20260329-180606-autonomy-policy-matrix--escala-C",
  "status": "success",
  "summary": "FP-D autonomy policy matrix and provenance contract defined",
  "report_path": ".vnx-data/unified_reports/20260329-180632-C-policy-matrix.md",
  "trace_token": "Dispatch-ID: 20260329-180606-autonomy-policy-matrix--escala-C",
  "pr_number": null,
  "feature_plan_pr": "PR-0",
  "provenance": {
    "git_ref": "abc123...",
    "branch": "feature/safe-autonomy-governance",
    "is_dirty": false,
    "dirty_files": 0,
    "diff_summary": null,
    "captured_at": "2026-03-29T18:30:00Z",
    "captured_by": "receipt_processor"
  },
  "session": {
    "session_id": "session-xyz",
    "terminal": "T3",
    "model": "claude-opus-4-6",
    "provider": "claude_code",
    "captured_at": "2026-03-29T18:30:00Z"
  }
}
```

---

## 7. Provenance Registry (PR-4)

PR-4 builds a queryable provenance registry that enables audit views. The registry schema:

```sql
CREATE TABLE IF NOT EXISTS provenance_registry (
    dispatch_id TEXT NOT NULL,
    receipt_id TEXT,                      -- run_id from receipt
    commit_sha TEXT,                      -- 40-char hex
    pr_number INTEGER,                   -- GitHub PR number
    feature_plan_pr TEXT,                -- Feature plan PR reference
    trace_token TEXT,                    -- Full trace token string
    chain_status TEXT NOT NULL DEFAULT 'incomplete',  -- complete | incomplete | broken
    gaps_json TEXT,                       -- JSON array of gap types found
    verified_at TEXT,                     -- ISO-8601
    verified_by TEXT,                     -- verifier | ci | operator
    PRIMARY KEY (dispatch_id)
);
```

### 7.1 Chain Status Values

| Status | Meaning |
|---|---|
| `complete` | All four links (dispatch -> receipt -> commit -> PR) are present and valid |
| `incomplete` | One or more links are missing but no contradictions found |
| `broken` | A link contradicts another (e.g., receipt points to a different dispatch than the commit trace token) |

---

## Appendix A: Trace Token Validation Pseudocode

```python
PREFERRED_RE = re.compile(r'^Dispatch-ID:\s+(\S+)$', re.MULTILINE)
LEGACY_DISPATCH_RE = re.compile(r'dispatch:(\S+)')
LEGACY_PR_RE = re.compile(r'\bPR-(\d+)\b')
LEGACY_FP_RE = re.compile(r'\bFP-([A-Z])\b')

def extract_trace_tokens(commit_message: str) -> dict:
    """Extract all trace tokens from a commit message."""
    result = {
        "preferred": None,
        "legacy_dispatch": None,
        "legacy_pr": [],
        "legacy_fp": [],
    }

    m = PREFERRED_RE.search(commit_message)
    if m:
        result["preferred"] = m.group(1)

    m = LEGACY_DISPATCH_RE.search(commit_message)
    if m:
        result["legacy_dispatch"] = m.group(1)

    result["legacy_pr"] = LEGACY_PR_RE.findall(commit_message)
    result["legacy_fp"] = LEGACY_FP_RE.findall(commit_message)

    return result

def validate_trace_token(commit_message: str, enforcement_mode: str) -> dict:
    """Validate a commit message for trace token presence."""
    tokens = extract_trace_tokens(commit_message)

    has_preferred = tokens["preferred"] is not None
    has_any_legacy = (
        tokens["legacy_dispatch"] is not None
        or len(tokens["legacy_pr"]) > 0
        or len(tokens["legacy_fp"]) > 0
    )

    if has_preferred:
        return {"valid": True, "format": "preferred", "dispatch_id": tokens["preferred"]}
    elif has_any_legacy:
        return {"valid": True, "format": "legacy", "tokens": tokens}
    else:
        severity = "error" if enforcement_mode == "enforced" else "warning"
        return {"valid": False, "format": None, "severity": severity}
```

## Appendix B: Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `VNX_CURRENT_DISPATCH_ID` | Current dispatch context for prepare-commit-msg hook | unset |
| `VNX_PROVENANCE_ENFORCEMENT` | `0` = shadow (warn), `1` = enforced (block) | `0` |
| `VNX_PROVENANCE_LEGACY_ACCEPTED` | `1` = accept legacy formats, `0` = preferred only | `1` |
