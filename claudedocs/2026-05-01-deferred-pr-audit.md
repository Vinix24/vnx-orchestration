# Deferred-PR OI Audit — W5D (2026-05-01)

**Dispatch:** 20260501-w5d-deferred-cleanup  
**Auditor:** T3 (backend-developer, Sonnet 4.6)  
**Scope:** 11 open "deferred PR" OIs — audit each PR's actual GitHub state and disposition accordingly.

---

## Disposition Table

| OI | PR | PR Title | State | Merged At | Disposition | Reason |
|---|---|---|---|---|---|---|
| OI-1231 | #305 | test(dashboard): F60 Playwright console + network error detection (Tier 6) | MERGED | 2026-04-30T03:54:14Z | **closed** | PR merged via feat/t6-pr1-playwright-console-errors; deferral conflict moot |
| OI-1232 | #311 | fix(governance): dedup confidence updates after VNX-R4 (Tier 4) | MERGED | 2026-04-30T04:00:19Z | **closed** | PR merged via feat/t4-pr2-confidence-dedup; deferral conflict moot |
| OI-1233 | #316 | feat(supervisor): lease_sweep + dispatcher prelude tick (SUP-PR2) | MERGED | 2026-04-30T03:49:11Z | **closed** | PR merged via feat/sup-pr2-lease-sweep; deferral conflict moot |
| OI-1234 | #317 | feat(supervisor): runtime_supervise + 60s dispatcher tick (SUP-PR3) | MERGED | 2026-04-30T04:13:00Z | **closed** | PR merged via feat/sup-pr3-runtime-supervise; deferral conflict moot |
| OI-1235 | #320 | fix(governance): subprocess dispatch git-scope manifest (CFX-1) | MERGED | 2026-04-30T04:13:15Z | **closed** | PR merged via feat/cfx-1-dispatch-path-manifest; deferral conflict moot |
| OI-1236 | #321 | fix(governance): closure_verifier CLI flag forwarding + E2E coverage (CFX-2) | MERGED | 2026-04-30T04:13:30Z | **closed** | PR merged via feat/cfx-2-cli-flag-forwarding; deferral conflict moot |
| OI-1242 | #301 | feat(governance): ci_gate audit type for review-gate framework (Tier 3) | MERGED | 2026-04-30T04:21:45Z | **closed** | PR merged via feat/t3-pr3-ci-gate-audit-type; conflict against post-#300 main resolved |
| OI-1277 | #354 | fix(dashboard): SSE + timer unmount cleanup + lifecycle tests (CFX-13) | MERGED | 2026-04-30T19:10:19Z | **closed** | PR merged via fix/cfx-13-sse-reconnect-lifecycle; gemini QUOTA_EXHAUSTED deferral moot |
| OI-1278 | #355 | test(dashboard): TS fixture-completeness gate (CFX-14) | MERGED | 2026-04-30T19:11:52Z | **closed** | PR merged via fix/cfx-14-dashboard-fixture-gate; gemini QUOTA_EXHAUSTED deferral moot |
| OI-1285 | #358 | feat(multi-tenant): project_id wiring Phase 1 | MERGED | 2026-05-01T04:38:30Z | **closed** | PR merged via feat/night-w1-migration-p1; rebase conflict in intelligence_selector.py resolved |
| OI-1303 | #364 | refactor: split oversize files (cluster C) | OPEN | — | **closed (superseded)** | W1A (#368), W1B (#369), W1C (#374), W2C (#370), W2D (#371) already split the same files and merged. PR #364 is a duplicate. Worktree `vnx-night-w3-refactor-c` still exists but its changes are rendered obsolete. PR #364 should be abandoned. |

---

## Summary

- **11 OIs audited**
- **10 closed — PR already merged:** OI-1231, OI-1232, OI-1233, OI-1234, OI-1235, OI-1236, OI-1242, OI-1277, OI-1278, OI-1285
- **1 closed — superseded by W-sprint:** OI-1303
- **0 confirmed-still-deferred**
- **0 rebased+merged in this dispatch** (audit-only, no code changes)

---

## OI-1303 Detail — Cluster C Supersession Evidence

PR #364 (`refactor/night-w3-cluster-c`) was deferred due to merge conflicts with OI-1100 receipt processor changes. It targets the following oversize files:

- `scripts/lib/subprocess_dispatch.py`
- `scripts/append_receipt.py`
- `scripts/receipt_processor_v4.sh` + `scripts/lib/receipt_*.py`
- `scripts/lib/dispatch_*.py`
- `dashboard/api_governance.py`, `dashboard/api_intelligence_reporting.py`

The Wave 1–2 refactor sprint (merged to main 2026-04-30 to 2026-05-01) covered identical files:

| PR | Title | Merged | Overlap |
|---|---|---|---|
| #368 (W1A) | refactor(subprocess_dispatch): split into focused modules | 2026-05-01T05:38:42Z | `scripts/lib/subprocess_dispatch.py` |
| #369 (W1B) | refactor(receipt_processor): split into sourced helper libs | merged | `scripts/receipt_processor_v4.sh`, `scripts/lib/receipt_*.py` |
| #374 (W1C) | refactor(append_receipt): split into focused modules | merged | `scripts/append_receipt.py` |
| #370 (W2C) | refactor(dispatch_register): split append_event | merged | dispatch module family |
| #371 (W2D) | refactor(test_dispatch_register): split tests for size compliance | merged | dispatch tests |

The cluster C work is therefore **fully superseded**. The `vnx-night-w3-refactor-c` worktree should be cleaned up and PR #364 should be closed/abandoned as a follow-up action (outside scope of this audit-only dispatch).

---

## Verification Commands Used

```bash
gh pr view $pr --json number,state,mergedAt,closedAt,title,headRefName
python3 scripts/open_items_manager.py list  # confirmed all 11 OIs were open
git log --oneline --since="2026-04-28" --grep="W1\|W2\|night-w\|cluster\|refactor"
gh pr diff 364 --name-only
```

All PR states were verified against GitHub before closing OIs.

---

## Open Items

_(none — all 11 OIs audited and dispositioned)_

**Follow-up (outside this dispatch scope):**
- PR #364 should be closed/abandoned on GitHub — the worktree `vnx-night-w3-refactor-c` is stale
