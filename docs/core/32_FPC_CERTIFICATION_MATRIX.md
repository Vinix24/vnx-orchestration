# FP-C Certification Matrix

**Feature**: FP-C — Execution Modes, Headless Routing, And Intelligence Quality
**PR**: PR-0
**Status**: Canonical
**Purpose**: Maps every in-scope scenario to its expected behavior, implementing PR, and verification evidence. FP-C is certified when every row passes.

---

## How To Use This Matrix

1. Each row is one scenario that FP-C must handle correctly.
2. The "Expected Outcome" column defines the correct behavior.
3. The "Implementing PR" column identifies which PR delivers the implementation.
4. The "Verification" column specifies what test or evidence proves correctness.
5. FP-C is certified when every row shows `pass` status.

---

## 1. Task Class Routing

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 1.1 | `coding_interactive` dispatch created | Routes to `interactive_tmux_*` target; headless targets excluded | PR-1, PR-5 | Routing event shows target_type=interactive_tmux_*; no headless candidate selected |
| 1.2 | `research_structured` dispatch with headless available | Routes to `headless_*_cli` target when `VNX_HEADLESS_ROUTING=1` | PR-1, PR-5 | Routing event shows target_type=headless_*_cli; task completes with receipt |
| 1.3 | `research_structured` dispatch without headless available | Falls back to `interactive_tmux_*` target | PR-1, PR-5 | Routing event shows fallback_used=true; target_type=interactive_tmux_* |
| 1.4 | `docs_synthesis` dispatch with headless available | Routes to `headless_*_cli` target when eligible | PR-1, PR-5 | Routing event confirms headless routing; receipt produced |
| 1.5 | `channel_response` dispatch without inbox registration | Routing rejected; dispatch not created | PR-2, PR-5 | Error event with reason "channel dispatch must enter via inbox" |
| 1.6 | `ops_watchdog` dispatch | Prefers interactive tmux target | PR-5 | Routing event shows preference for interactive; ops task completes |
| 1.7 | Unknown skill maps to `coding_interactive` | Default task class applied; routes interactive | PR-1, PR-5 | Dispatch metadata shows task_class=coding_interactive |
| 1.8 | T0 overrides routing with explicit target_id | Override respected if target is healthy and capable | PR-5 | Routing event shows override=true; target matches T0 specification |
| 1.9 | T0 override to unhealthy target | Override rejected; dispatch remains queued | PR-5 | Routing event shows override_rejected=true with health reason |

## 2. Execution Target Registry

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 2.1 | Register interactive tmux target | Target appears in registry with correct capabilities and health | PR-1 | Registry query returns target with declared capabilities |
| 2.2 | Register headless CLI target | Target appears in registry; not bound to tmux pane | PR-1 | Registry entry has no pane_id; target_type=headless_*_cli |
| 2.3 | Register channel adapter | Adapter registered separately from T1/T2/T3 | PR-2 | Registry entry has terminal_id=null; target_type=channel_adapter |
| 2.4 | Health check on healthy target | Returns health=healthy; target remains routing-eligible | PR-1 | Health check event logged; target.health=healthy |
| 2.5 | Health check on unresponsive target | Health transitions to unhealthy; target excluded from routing | PR-1 | Health transition event; no dispatches routed to this target |
| 2.6 | Deregister target | Target health set to offline; existing dispatch unaffected | PR-1 | Target.health=offline; in-flight dispatch continues |
| 2.7 | Duplicate target registration | Idempotent; existing entry unchanged | PR-1 | No error; registry entry matches original |

## 3. Headless Execution

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 3.1 | Headless dispatch executes successfully | Dispatch completes; receipt produced; attempt recorded as succeeded | PR-1 | Receipt in NDJSON; attempt.state=succeeded; coordination event logged |
| 3.2 | Headless dispatch fails | Attempt recorded as failed; failure_reason captured; fallback to interactive available | PR-1 | Attempt.state=failed; failure_reason non-null; fallback event if retried interactively |
| 3.3 | Headless dispatch times out | ACK timeout incident created; retry with interactive fallback | PR-1 | Incident with class=ack_timeout; re-delivery to interactive target |
| 3.4 | Headless target health degrades during queue | Next dispatch skips degraded target; prefers healthy alternatives | PR-1 | Routing event shows degraded target deprioritized |
| 3.5 | `VNX_HEADLESS_ROUTING=0` disables headless | All dispatches route interactive regardless of task class | PR-5 | No headless routing events; all targets selected are interactive_tmux_* |

## 4. Inbound Event Inbox

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 4.1 | Inbound event arrives in inbox | Event persisted durably before any routing | PR-2 | Inbox file/record exists with event payload and timestamp |
| 4.2 | Inbox item translated to dispatch | Canonical dispatch created via broker; channel_origin preserved | PR-2 | Dispatch.channel_origin matches inbox event; dispatch registered in DB |
| 4.3 | Duplicate inbound event (same dedupe key) | Second event rejected; no duplicate dispatch | PR-2 | Only one dispatch for the dedupe key; rejection event logged |
| 4.4 | Inbox processing retry on transient failure | Retry with bounded attempts; dead-letter after budget exhausted | PR-2 | Retry attempts recorded; dead-letter event if exhausted |
| 4.5 | Channel adapter offline when event arrives | Event persisted in inbox; dead-lettered after timeout; T0 escalation | PR-2 | Inbox record persists; escalation event emitted |
| 4.6 | Inbound event with routing hints | Task class and target preference extracted from event metadata | PR-2 | Dispatch.task_class matches hint; routing event shows hint applied |

## 5. Bounded Intelligence Injection

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 5.1 | Dispatch created with matching intelligence | Up to 3 items injected; one per class maximum | PR-3 | intelligence_payload contains <= 3 items; each unique item_class |
| 5.2 | No intelligence meets threshold | Zero items injected; suppression event emitted | PR-3 | intelligence_payload.items is empty; intelligence_suppression event logged |
| 5.3 | Intelligence payload exceeds 2000 chars | Lower-priority items dropped (recent_comparable first) | PR-3 | Payload size <= 2000 chars; suppressed array shows dropped items |
| 5.4 | Intelligence injected at resume (recovered dispatch) | Fresh selection at resume time; items may differ from original injection | PR-3 | New injection_decision event with injection_point=dispatch_resume |
| 5.5 | Task class filtering changes selected items | Different task classes get different intelligence slices | PR-3 | Two dispatches with different task_class get different item sets |
| 5.6 | Item with zero evidence count | Item suppressed regardless of confidence | PR-3 | Item not in payload; suppression reason = "evidence_count=0" |
| 5.7 | Injection decision event emitted | Every injection (including empty) produces a coordination event | PR-3 | coordination_events contains intelligence_injection or intelligence_suppression |

## 6. Recommendation Usefulness Metrics

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 6.1 | Recommendation proposed | Recommendation recorded with acceptance_state=proposed | PR-4 | recommendations table row with state=proposed |
| 6.2 | Recommendation accepted | Outcome measurement window opens (7 days) | PR-4 | accepted_at set; outcome_window_start/end populated |
| 6.3 | Recommendation rejected with reason | Rejection recorded; no outcome window | PR-4 | rejected_at set; rejection_reason non-null; no window |
| 6.4 | Recommendation expires unreviewed | State transitions to expired after 14 days | PR-4 | acceptance_state=expired; no outcome data collected |
| 6.5 | Accepted recommendation outcome measured | Before/after metrics collected and compared | PR-4 | recommendation_outcomes row with baseline and outcome values |
| 6.6 | Insufficient data for comparison | Comparison marked insufficient_data; no conclusion drawn | PR-4 | outcome.comparison_status=insufficient_data |
| 6.7 | Recommendation superseded by newer one | Old recommendation state=superseded; new one is active | PR-4 | Older row.acceptance_state=superseded; newer row.acceptance_state=proposed |
| 6.8 | Metrics work across headless dispatches | Headless dispatch outcomes included in recommendation metrics | PR-4 | Metric aggregation includes dispatches with target_type=headless_*_cli |
| 6.9 | Metrics work across channel-originated dispatches | Channel-originated dispatch outcomes included | PR-4 | Metric aggregation includes dispatches with channel_origin != null |
| 6.10 | Advisory-only enforcement | No recommendation automatically mutates runtime config | PR-4 | No config changes without explicit operator action; acceptance is recording only |

## 7. Mixed Execution Cutover (PR-5)

| # | Scenario | Expected Outcome | Implementing PR | Verification |
|---|----------|-----------------|-----------------|--------------|
| 7.1 | Cutover enabled | Mixed routing active; coding stays interactive; non-coding eligible for headless | PR-5 | Routing decisions follow task class rules; coding never headless |
| 7.2 | Rollback via `VNX_HEADLESS_ROUTING=0` | All routing returns to interactive-only | PR-5 | No headless routing after flag set to 0 |
| 7.3 | Live dispatch shows intelligence payload | Intelligence items visible in dispatch bundle and routing event | PR-5 | bundle.json contains intelligence_payload; dashboard can render it |
| 7.4 | End-to-end: channel event → inbox → dispatch → headless execution → receipt | Full lifecycle completes with audit trail | PR-5 | All intermediate events exist in coordination_events |
| 7.5 | FP-C certification evidence complete | All matrix rows pass; residual risks documented | PR-5 | Certification report JSON with all rows pass/fail |

---

## Certification Procedure

### Pre-Certification (Per-PR)

Each PR runs its quality gate tests covering the rows assigned to it. The gate must pass before the PR merges.

### Final Certification (PR-5)

After PR-5 merges, run full certification:

1. **Contract tests**: Task class and execution target schemas validate against all fixtures.
2. **Routing tests**: Every routing invariant (R-1 through R-8) verified with positive and negative cases.
3. **Intelligence tests**: Injection bounded to 3 items, evidence thresholds enforced, suppression events emitted.
4. **Recommendation tests**: Acceptance lifecycle, outcome windows, and metric collection verified.
5. **Integration tests**: End-to-end flows for interactive, headless, and channel-originated dispatches.
6. **Rollback tests**: `VNX_HEADLESS_ROUTING=0` returns to pre-FPC behavior cleanly.

### Certification Evidence

The certification run produces a JSON report mapping each row number to:
- `status`: pass | fail | skip
- `evidence`: test name, event IDs, or log excerpts
- `notes`: any caveats or residual risks

FP-C is certified when all rows show `pass` status.

---

## Residual Risk Register

| Risk | Mitigation | Owner |
|------|-----------|-------|
| Task class boundaries may need refinement after real-world headless routing | Monitor routing distribution and first-pass success by task class; adjust skill mapping | T0 |
| Headless CLI execution may have different failure modes than tmux delivery | PR-1 implements headless-specific health checks and timeout adjustments | PR-1 |
| Intelligence confidence thresholds may be too strict or too lenient | PR-3 makes thresholds configurable; PR-4 measures effectiveness | PR-3, PR-4 |
| Recommendation measurement windows may be too short for low-volume dispatches | PR-4 reports insufficient_data explicitly; window length is configurable | PR-4 |
| Channel adapter reliability is untested in production | PR-2 includes bounded retry and dead-letter semantics; monitor in first week | PR-2 |
| Cutover rollback may leave orphaned headless processes | PR-5 includes graceful shutdown for headless targets on rollback | PR-5 |
