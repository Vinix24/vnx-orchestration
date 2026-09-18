# glm_gate — Headless Gate Report

**PR**: 1862
**Branch**: dispatch/20260917-oi1750-weigering-audit
**Gate**: glm_gate
**Generated**: 2026-09-17T09:18:31Z

---

## Gate Output

---
schema_version: 1
dispatch_id: glm-gate-pr1862-1789636624
provider: glm-harness
sub_provider: zai
model: glm-5.2
terminal_id: plan-gate
pool_id: headless
role: review-gate
task_class: research_structured
pr_id: none
duration_seconds: 81.74
exit_code: 0
token_usage:
  input: 40720
  output: 983
  cache_read: 0
cost_usd: 0.02992226
route_decision:
  strategy: default
  selected_provider: glm-harness
  selected_model: glm-5.2
---

# Dispatch glm-gate-pr1862-1789636624

**Dispatch-ID**: glm-gate-pr1862-1789636624
**Model**: glm-5.2
**Provider**: glm-harness
**Terminal**: plan-gate
**Duration**: 81.7s

## Response

## Review: PR 1862 — durable audit trail for overwrite-guard refusals (OI-1750)

The diff adds a durable `gate_execution_audit.ndjson` line when the overwrite guard refuses a write, and threads `commit_sha` through every skip-rationale writer so a refused write's head survives in the ledger. I traced each of the four "guarded write + unconditional own skip-rationale" paths plus the two default-audit paths (record_terminal_result, record_failure) and confirmed the double-log prevention is consistent: each refused write lands exactly one line.

### Correctness

- `record_terminal_result` now wraps the guard in try/except, audits on refusal, then re-raises. The re-raise preserves prior behavior (no `_write_result_atomic` after refusal). The audit happens inside `slot_lock(result_path)`, but `_audit_write_refusal` writes to a different file (`gate_execution_audit.ndjson`), so the per-path `slot_lock` does not deadlock.
- `_state_dir_from_result_path` returns `parent.parent` of the `review_gates` segment. For `<state_dir>/review_gates/results/pr.json`, `parent` = `.../review_gates`, `parent.parent` = `state_dir`. Correct. The flat-path case returns None and the caller degrades to "no audit line" rather than guessing a path; this is explicitly tested (`test_flat_result_path_with_no_review_gates_ancestor_is_not_a_crash`).
- The `audit_refusal=False` opt-out is applied to exactly the four callers that pair a guarded write with their own unconditional skip-rationale; the `commit_sha` is moved to the skip-rationale call in each so no head is lost. The default stays `True` for future callers. Logic is sound.

### Governance/contract

- Adding `commit_sha` to every `gate_skip_rationale` record is additive. NDJSON ledgers conventionally tolerate extra keys; I could not confirm a strict allowlist validator that would reject unknown fields from the diff alone. Flagged as residual risk, not blocking.
- The double-log invariant is the core contract this PR enforces, and the count-pinning tests (`test_oi1750_tweede_plek_audit_line_count.py`) assert `len(lines) == 1` plus the surviving reason, not just "a line exists". Good test design.

### Security

No prompt-injection or untrusted-input handling introduced. The deterministic pre-scan found no instruction-like text. The diff is audit/log code only; no new code-execution or external-input surface.

### Notes (non-blocking)

- `_state_dir_from_result_path` is a structural heuristic: a future result-path builder that does not route through a `review_gates` segment will silently get no audit line for a refused write. The behavior is safe (no crash, no wrong path) and documented, but the silent-drop is a failure mode a later refactor could trip without a test catching it. Info only.
- `_audit_write_refusal`'s `reason_detail` is a fixed string ("an existing decided, evidenced verdict for this head would have been downgraded"). Accurate for the downgrade-refusal case the guard handles; if the guard ever refuses for a different reason, the message would mislabel it. Info only.

No blocking correctness, security, or governance issue introduced by this diff.

```json
{
  "verdict": "pass",
  "findings": [
    {
      "severity": "info",
      "message": "_state_dir_from_result_path silently returns None (no audit line) for any result path that does not pass through a 'review_gates' segment; a future result-path builder that breaks the convention would leave a refused write unaudited with no error. Safe and documented, but a silent failure mode.",
      "file_path": "scripts/lib/gate_recorder.py",
      "line": 0
    },
    {
      "severity": "info",
      "message": "_audit_write_refusal uses a fixed reason_detail describing a downgrade; accurate for the guard's current refusal case but would mislabel a refusal that fires for a different reason if the guard's scope ever widens.",
      "file_path": "scripts/lib/gate_recorder.py",
      "line": 0
    }
  ],
  "residual_risk": "The double-log prevention depends on the grep sweep having found ALL four production callers that pair write_result_guarded with an unconditional own skip-rationale write; a missed caller would still double-log. The additive commit_sha field on gate_skip_rationale records is safe unless a strict unknown-field-rejecting schema validator exists downstream (not confirmable from the diff)."
}
```

