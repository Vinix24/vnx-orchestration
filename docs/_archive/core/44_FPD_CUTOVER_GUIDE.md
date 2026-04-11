# FP-D Safe Autonomy Cutover Guide

**Feature**: FP-D — Safe Autonomy, Governance Envelopes, And End-To-End Provenance
**PR**: PR-5
**Status**: Canonical
**Purpose**: Operator guide for cutover, rollback, and transition management

---

## 1. Overview

FP-D introduces explicit autonomy envelopes and provenance enforcement on top of
the hardened runtime (FP-A through FP-C). The cutover is controlled by two feature
flags and is fully reversible.

### Feature Flags

| Flag | Default | Effect |
|------|---------|--------|
| `VNX_AUTONOMY_EVALUATION` | `0` | `1` = policy evaluation outcomes are binding |
| `VNX_PROVENANCE_ENFORCEMENT` | `0` | `1` = commits without trace tokens are blocked |

### Cutover Phases

| Phase | Flags | Behavior |
|-------|-------|----------|
| Shadow | Both `0` | Evaluation runs, emits events, no blocking |
| Provenance Only | `VNX_PROVENANCE_ENFORCEMENT=1` | Provenance active; autonomy advisory |
| Full Enforcement | Both `1` | Full policy evaluation and provenance enforcement |
| Rollback | Both `0` | Returns to pre-FP-D behavior |

---

## 2. Pre-Cutover Checklist

Run the prerequisite check before cutover:

```bash
python scripts/safe_autonomy_cli.py prerequisites
```

Prerequisites that must pass:
1. **Policy matrix complete** — All 10 policy classes covered with decision types
2. **Governance schema ready** — `escalation_state` and `governance_overrides` tables exist
3. **Provenance registry ready** — `provenance_registry` table exists
4. **Verification table ready** — `provenance_verifications` table exists
5. **Git hooks present** — `prepare-commit-msg` and `commit-msg` hooks installed
6. **No blocking escalations** — No unresolved hold/escalate states
7. **Authority preserved** — Merge forbidden, completion gated (G-R4)
8. **Policy classes covered** — All 10 classes have at least one decision type

---

## 3. Cutover Procedure

### Step 1: Run Pre-Cutover Validation

```bash
python scripts/safe_autonomy_cli.py prepare
```

This validates prerequisites and emits a `cutover_prepared` event without modifying state.

### Step 2: Run FP-D Certification

```bash
python scripts/safe_autonomy_cli.py certify
```

All certification matrix rows must pass. Review any skipped rows.

### Step 3: Record Cutover Decision

```bash
python scripts/safe_autonomy_cli.py cutover \
  --actor t0 \
  --justification "FP-D certification passed; prerequisites met"
```

This records a `cutover_executed` governance event and prints the required environment variable settings.

### Step 4: Set Environment Variables

Apply the flag instructions from Step 3:

```bash
export VNX_AUTONOMY_EVALUATION=1
export VNX_PROVENANCE_ENFORCEMENT=1
```

For persistent configuration, add these to the VNX runtime environment
(e.g., `.vnx-data/env` or the session profile).

### Step 5: Verify Cutover

```bash
python scripts/safe_autonomy_cli.py status
python scripts/safe_autonomy_cli.py verify-envelope
```

Confirm the phase shows `full_enforcement` and the envelope verification passes.

---

## 4. Gradual Rollout (Optional)

For cautious rollout, enable provenance enforcement first:

```bash
python scripts/safe_autonomy_cli.py cutover \
  --actor t0 \
  --justification "Gradual rollout: provenance first" \
  --provenance-only

export VNX_PROVENANCE_ENFORCEMENT=1
# VNX_AUTONOMY_EVALUATION remains 0
```

After confirming provenance enforcement is stable, enable full enforcement:

```bash
python scripts/safe_autonomy_cli.py cutover \
  --actor t0 \
  --justification "Full enforcement after provenance validation period"

export VNX_AUTONOMY_EVALUATION=1
```

---

## 5. Rollback Procedure

### Immediate Rollback

```bash
python scripts/safe_autonomy_cli.py rollback \
  --actor t0 \
  --justification "Describe the reason for rollback"

export VNX_AUTONOMY_EVALUATION=0
export VNX_PROVENANCE_ENFORCEMENT=0
```

### What Rollback Preserves

- Enriched receipts remain valid (backward-compatible)
- Policy evaluation events continue in advisory mode
- Escalation state is preserved (can be reviewed later)
- Provenance registry entries remain valid
- Git hooks remain installed (but operate in shadow mode)

### What Rollback Changes

- Policy evaluation outcomes become advisory-only (no blocking)
- Trace token validation warnings only (no commit blocking)
- Forbidden actions for runtime actors are not enforced
- Pre-FP-D behavior resumes for all action classes

---

## 6. Operator Commands

| Command | Purpose |
|---------|---------|
| `safe_autonomy_cli.py status` | Current cutover phase and health |
| `safe_autonomy_cli.py prerequisites` | Validate cutover prerequisites |
| `safe_autonomy_cli.py prepare` | Pre-cutover readiness report |
| `safe_autonomy_cli.py cutover` | Record cutover transition |
| `safe_autonomy_cli.py rollback` | Record rollback transition |
| `safe_autonomy_cli.py verify-envelope` | Check autonomy constraints |
| `safe_autonomy_cli.py certify` | Run FP-D certification matrix |
| `safe_autonomy_cli.py review` | T0 integrated review summary |

All commands accept `--json` for machine-readable output.

---

## 7. What FP-D Does NOT Do

Per the governance rules (G-R4, Section 3.3 of the policy matrix):

1. **No autonomous merge** — `branch_merge` and `force_push` remain forbidden for runtime actors
2. **No autonomous completion** — `dispatch_complete`, `pr_close`, and `feature_certify` remain gated
3. **No silent policy mutation** — Recommendation logic cannot rewrite policy (A-R9)
4. **No CLI lock-in** — Provenance enforcement works across Claude CLI, Codex CLI, and future tools

---

## 8. Residual Risks

| Risk | Mitigation | Owner |
|------|-----------|-------|
| Policy classification may need refinement | Monitor evaluation distribution and escalation frequency | T0 |
| Legacy trace tokens allow weaker provenance | Track legacy usage; sunset via `VNX_PROVENANCE_LEGACY_ACCEPTED=0` | T0 |
| Feature flag rollback leaves partial state | Rollback is behavioral; enriched data stays valid | PR-5 |
| Override mechanism abuse | Override frequency in audit views; T0 reviews patterns | T0 |
| Enforcement blocks legitimate actions | Rollback to shadow via `VNX_AUTONOMY_EVALUATION=0` | Operator |
| CI enforcement depends on repo CI config | Document CI setup; include in governance audit | PR-3 |

---

## 9. Monitoring After Cutover

### Key Metrics to Watch

1. **Policy evaluation distribution** — ratio of automatic:gated:forbidden outcomes
2. **Escalation frequency** — new holds and escalations per day
3. **Override frequency** — granted overrides per week
4. **Provenance completeness** — percentage of dispatches with complete chains
5. **Trace token format** — preferred vs. legacy format ratio

### Health Checks

```bash
# Cutover status and escalation health
python scripts/safe_autonomy_cli.py status --json

# Review governance and provenance audit
python scripts/safe_autonomy_cli.py review --json

# Run periodic certification
python scripts/safe_autonomy_cli.py certify --json
```

---

## 10. Governance Event Types (FP-D)

| Event Type | Emitted By | Purpose |
|-----------|-----------|---------|
| `policy_evaluation` | governance_evaluator | Every action evaluation |
| `escalation_transition` | governance_evaluator | Escalation level changes |
| `governance_override` | governance_evaluator | Override grants/denials |
| `provenance_registered` | receipt_provenance | Provenance link created |
| `provenance_gap` | receipt_provenance | Missing provenance detected |
| `provenance_verified` | provenance_verification | Verification run completed |
| `cutover_prepared` | safe_autonomy_cutover | Pre-cutover validation |
| `cutover_executed` | safe_autonomy_cutover | Cutover transition recorded |
| `cutover_rollback` | safe_autonomy_cutover | Rollback transition recorded |
