# VNX Project Status

**Status**: Active  
**Last Updated**: 2026-04-01  
**Owner**: VNX Maintainer  
**Purpose**: Commit-backed status after the completed 5-feature autonomous hardening chain.

---

## Current Summary

The latest hardening chain is complete and merged through `557864d`.

This baseline now includes:

1. **Terminal input readiness protection**
   - slash-prefixed dispatches are no longer sent blindly into tmux copy/search mode
2. **Queue/runtime projection consistency**
   - queue truth drift is detected and repairable from canonical runtime evidence
3. **Gate evidence accuracy**
   - gate lookup is PR-scoped and `report_path` enforcement is deterministic
4. **Dispatch requeue and classification accuracy**
   - retryable dispatch failures are deferred instead of being permanently rejected
5. **Delivery substep observability**
   - delivery rejection evidence identifies the failing substep instead of collapsing into a generic reject

The system is now best described as:

- governance-first
- mixed interactive + headless capable
- feature-worktree based
- receipt-led and closure-verifier enforced
- materially hardened for tmux/runtime/queue failure modes discovered during autonomous trials

---

## Representative Recent Chain Merges

- `e4cdf3c` — Feature 5: Terminal Input-Ready Mode Guard
- `942bd53` — Feature 6: Queue and Runtime Projection Consistency Hardening
- `1df2964` — Feature 7: Gate Evidence Accuracy and PR-Scoped Lookup
- `da60f53` — Feature 8: Dispatch Requeue and Classification Accuracy
- `557864d` — Feature 9: Delivery Substep Observability

Governance note:

- Features 1-2 in this lane were still local-only merges
- Features 3-5 in this lane were merged through real GitHub PRs with CI visibility
- compensating-evidence use for unstable headless gate execution is now explicit, not implicit

---

## Current Proven Capabilities

- Input-readiness checks before slash-prefixed dispatch delivery
- Queue/runtime projection reconciliation against canonical state
- PR-scoped gate-result lookup with report existence enforcement
- Deferred requeue classification for retryable dispatch failures
- Substep-level delivery failure annotation and certification evidence

---

## Carried-Forward Open Items

These items remain known and non-blocking, but are not resolved in code yet:

- `OI-022` — `rc_register` remains non-fatal while `acquire_lease` depends on its FK side effect
- `OI-024` — `configure_terminal_mode` probe placement remains a known deviation
- `OI-048` — headless Gemini/Codex gate execution reliability remains weak
- `OI-078` — `Profile C` CI path configuration remains pre-existing and needs cleanup

---

## Recommended Next Bridge Lane

The next short bridge lane should focus on:

1. residual governance bugfix sweep
2. headless review reliability hardening
3. operator UX improvements such as latest-first conversation resume

These should close the remaining governance friction before broader major-feature work.

---

## Maintenance Rule

Update this document when one of these changes:

- a merged feature materially changes what VNX can do today
- a remediation closes a previously recurring governance/runtime failure class
- the recommended next bridge lane changes in a meaningful way
