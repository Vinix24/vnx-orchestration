# T0 Example Orchestration Workflows

## Example 1: Successful PR Completion

```
📥 Receipt: PR-3 completion report received
    ↓
🔬 Read QUALITY line: [approve|risk:0.12] → Standard review
    ↓
📋 Check: `open_items_manager.py digest`
    → 7 open items for PR-3 (3 blockers, 3 warns, 1 info)
    → Evidence attached by receipt processor
    ↓
📖 Read report: tests pass, E2E passes, stress test 93%, memory stable
    ↓
✅ Close: OI-PR3-001 --reason "405 tests pass per report"
✅ Close: OI-PR3-002 --reason "E2E suite passes"
✅ Close: OI-PR3-003 --reason "3-concurrent 100% per stress test section"
✅ Close: OI-PR3-004 --reason "5-concurrent 93.3% (>=60% threshold met)"
✅ Close: OI-PR3-005 --reason "memory stable at 140MB, no regression"
✅ Close: OI-PR3-006 --reason "zero zombies confirmed in cleanup section"
✅ Close: OI-PR3-007 --reason "dashboard data validated in report"
    ↓
🎯 All blockers/warns closed → `pr_queue_manager.py complete PR-3`
    ↓
📊 Result: PR-4 dependencies now met, ready to dispatch
```

## Example 2: Partial Completion with Follow-Up

```
📥 Receipt: PR-5 completion report received
    ↓
🔬 Read QUALITY line: [hold|risk:0.55] → Critical review
    ↓
📋 Check: `open_items_manager.py digest`
    → 5 open items for PR-5 (2 blockers, 2 warns, 1 info)
    → 2 new OIs auto-created by quality advisory
    ↓
📖 Read report: tests pass but dead_code(3) flagged
    ↓
✅ Close: OI-PR5-001 --reason "unit tests 95% coverage per report"
✅ Close: OI-PR5-002 --reason "API contract validated"
❌ Open: OI-PR5-003 (blocker) — dead code not cleaned up
⏸️ Defer: OI-PR5-004 --reason "cosmetic, not blocking"
    ↓
⚠️ Blocker OI-PR5-003 still open → Dispatch follow-up

[[TARGET:A]]
Manager Block

Role: backend-developer
Terminal: T1
PR-ID: PR-5
Priority: P1
Cognition: normal
Dispatch-ID: 20260210-143000-pr5-dead-code
Parent-Dispatch: none
Reason: Remove dead code flagged by quality advisory (OI-PR5-003)

Context: [[@quality-advisory-report]]

Instruction:
- Remove dead code identified in quality advisory
- Run vulture to verify no more dead code
- Success: OI-PR5-003 blocker resolved

[[DONE]]
```

## Example 3: WAIT Decision

```
=== ORCHESTRATION DECISION ===
Status: WAIT
Reason: T2 currently working on storage optimization (12m elapsed)

Terminal States: T1=idle T2=working(12m) T3=idle
Queue: pending=1 active=1 conflicts=0
Open Items: PR-4 has 2 open blockers

Next Action: Monitor T2, review when receipt arrives
================================
```

## Example 4: Feature Initialization

```
=== ORCHESTRATION DECISION ===
Status: DISPATCH
Reason: New feature plan ready, initializing batch dispatch

Action taken:
1. python pr_queue_manager.py init-feature FEATURE_PLAN.md
   → Generated 8 PR dispatches to staging/
   → Created 24 open items from quality gates
2. python pr_queue_manager.py staging-list
   → PR-5: ✅ deps met (ready to promote)
   → PR-6: ⏳ depends on PR-5
   → PR-7: ⏳ depends on PR-5
   → PR-8: ⏳ depends on PR-6, PR-7
3. python pr_queue_manager.py promote PR-5-dispatch-id

Terminal States: T1=idle T2=idle T3=idle
Queue: pending=0 active=0 → promoting PR-5
================================
```

## Example 5: ESCALATE Decision

```
=== ORCHESTRATION DECISION ===
Status: ESCALATE
Reason: T3 blocked for >30min on MCP connection failure, manual intervention needed

Terminal States: T1=idle T2=idle T3=blocked(35m)
Queue: pending=5 active=0 conflicts=2
Recommendation: Check T3 MCP server connectivity, restart if needed
================================
```
