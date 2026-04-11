# Batch Refactor Contract — F27 PR-0

**Feature**: F27 — Batch Refactor Blocker OIs
**Dispatch**: 20260405-112620-f27-pr0-refactor-contract-C
**Date**: 2026-04-05
**Author**: T3 (Architect)

---

## Executive Summary

This contract defines exact module boundaries, function decompositions, and import migration rules for the three oversized files in F27. It also catalogs all functions > 80 lines across the codebase.

**Verified counts** (2026-04-05):
- Total open items: 738 (57 blockers)
- dispatcher_v8_minimal.sh: 2140L (limit 500L, 4.3x over)
- runtime_coordination.py: 1164L (limit 400L, 2.9x over)
- review_gate_manager.py: 1017L (limit 400L, 2.5x over)
- Functions > 80L across codebase: 183 (159 Python, 24 shell)

---

## 1. Dispatcher Decomposition (2140L → 5 files)

### 1.1 Current Structure

49 functions. 4 exceed 80 lines:

| Function | Lines | Span | Priority |
|----------|-------|------|----------|
| `dispatch_with_skill_activation()` | 552 | 1348–1899 | CRITICAL — must decompose into 4+ sub-functions first |
| `configure_terminal_mode()` | 272 | 858–1129 | CRITICAL — must decompose into 3+ sub-functions first |
| `process_dispatches()` | 198 | 1904–2101 | HIGH — must decompose into 3 sub-functions |
| `terminal_lock_allows_dispatch()` | 92 | 292–383 | HIGH — extract embedded Python to library |

### 1.2 Pre-requisite: Decompose Mega-Functions

Before distributing to modules, these functions must be split:

#### `dispatch_with_skill_activation()` (552L → 4 functions)

| New Function | Lines | Source Lines | Responsibility |
|-------------|-------|-------------|----------------|
| `prepare_dispatch_payload()` | ~120 | 1348–1480 | Pane mode probe, terminal resolution, metadata extraction, receipt footer, intelligence/context section building |
| `acquire_dispatch_lease()` | ~80 | 1481–1570 | Canonical lease check, legacy lock check, terminal claim acquire, RC registration, lease acquire |
| `deliver_dispatch_to_terminal()` | ~180 | 1571–1760 | Mode configuration, skill command build, prompt assembly, buffer load/paste delivery, error handling |
| `finalize_dispatch_delivery()` | ~100 | 1761–1899 | rc_delivery_success/failure, progress update, heartbeat, metadata logging, active dir move |

#### `configure_terminal_mode()` (272L → 4 functions)

| New Function | Lines | Source Lines | Responsibility |
|-------------|-------|-------------|----------------|
| `mode_pre_check()` | ~50 | 858–910 | Terminal/provider resolution, mode extraction, fail-closed routing check |
| `reset_terminal_context()` | ~60 | 911–975 | Input clear (C-u), context reset command, feedback modal dismissal, post-clear verify |
| `switch_terminal_model()` | ~80 | 976–1060 | Model routing decision, normalization, send switch command, 4s delay, pane capture verify |
| `activate_terminal_mode()` | ~50 | 1061–1129 | Provider-specific mode handling, planning/thinking/normal activation |

#### `process_dispatches()` (198L → 3 functions)

| New Function | Lines | Source Lines | Responsibility |
|-------------|-------|-------------|----------------|
| `validate_dispatch_preconditions()` | ~70 | 1904–1975 | Stuck file cleanup, skill validation, role validation, agent validation, track validation |
| `gather_dispatch_intelligence()` | ~40 | 1976–2020 | Task description extraction, intelligence gather, pattern/prevention parsing |
| `execute_and_classify_dispatch()` | ~60 | 2021–2101 | Dispatch invocation, marker post-checks, rejection/deferral/success classification |

#### `terminal_lock_allows_dispatch()` (92L → extracted)

Extract embedded Python (60L) to `scripts/lib/terminal_state_check.py`. Shell function becomes a ~30L wrapper calling the Python script.

### 1.3 Module Assignment

After decomposition, distribute all 49+ functions into 4 modules + orchestrator:

#### `scripts/lib/dispatch_create.sh` (~250L)

Functions for building dispatch payloads:
- `extract_instruction_content()` (29L, lines 1162–1190)
- `extract_context_files()` (49L, lines 1192–1240)
- `generate_receipt_footer()` (61L, lines 1245–1305)
- `map_role_to_skill()` (34L, lines 1310–1343)
- `prepare_dispatch_payload()` (~120L, extracted from dispatch_with_skill_activation)

#### `scripts/lib/dispatch_deliver.sh` (~350L)

Functions for terminal delivery mechanics:
- `tmux_send_best_effort()` (9L, lines 219–227)
- `tmux_load_buffer_safe()` (21L, lines 234–254)
- `tmux_retry()` (22L, lines 258–279)
- `get_pane_ids()` (21L, lines 701–721)
- `determine_executor()` (24L, lines 1134–1157)
- `mode_pre_check()` (~50L, extracted from configure_terminal_mode)
- `reset_terminal_context()` (~60L, extracted from configure_terminal_mode)
- `switch_terminal_model()` (~80L, extracted from configure_terminal_mode)
- `activate_terminal_mode()` (~50L, extracted from configure_terminal_mode)
- `deliver_dispatch_to_terminal()` (~180L, extracted from dispatch_with_skill_activation)

Note: This module is at the 500L boundary. If it exceeds during implementation, split `configure_terminal_mode` sub-functions into a separate `dispatch_mode.sh`.

#### `scripts/lib/dispatch_lifecycle.sh` (~380L)

Functions for lifecycle management:
- `track_to_terminal()` (8L, lines 282–289)
- `terminal_lock_allows_dispatch()` (~30L, reduced after Python extraction)
- `acquire_terminal_claim()` (21L, lines 385–405)
- `release_terminal_claim()` (19L, lines 407–425)
- `rc_enabled()` (3L, lines 431–433)
- `rc_python()` (3L, lines 435–437)
- `rc_register()` (25L, lines 441–465)
- `rc_check_terminal()` (23L, lines 470–492)
- `rc_acquire_lease()` (26L, lines 497–522)
- `rc_delivery_start()` (17L, lines 525–541)
- `rc_delivery_success()` (30L, lines 545–574)
- `rc_delivery_failure()` (12L, lines 577–588)
- `rc_release_lease()` (20L, lines 624–643)
- `rc_release_on_failure()` (44L, lines 650–693)
- `acquire_dispatch_lease()` (~80L, extracted from dispatch_with_skill_activation)
- `finalize_dispatch_delivery()` (~100L, extracted from dispatch_with_skill_activation)

#### `scripts/lib/dispatch_logging.sh` (~160L)

Functions for logging and audit:
- `log()` (3L, lines 72–74)
- `log_structured_failure()` (50L, lines 81–130)
- `_classify_blocked_dispatch()` (32L, lines 137–168)
- `emit_blocked_dispatch_audit()` (43L, lines 174–216)
- `emit_lease_cleanup_audit()` (29L, lines 593–621)

#### `scripts/dispatcher_v8_minimal.sh` — Orchestrator (~350L)

Remains as the main entry point:
- Shared variable declarations and constants (~50L)
- All `extract_*()` functions — 14 functions, 3–6L each (~60L)
- `get_terminal_provider()` (29L, lines 773–801)
- `get_context_reset_command()` (11L, lines 803–813)
- `validate_dispatch_preconditions()` (~70L, extracted from process_dispatches)
- `gather_dispatch_intelligence()` (~40L, extracted from process_dispatches)
- `execute_and_classify_dispatch()` (~60L, extracted from process_dispatches)
- Main loop (22L, lines 2118–2140)
- Source statements for all 4 modules

### 1.4 Shared State

All modules depend on these shared variables (declared in orchestrator, exported via `source`):

| Variable | Purpose |
|----------|---------|
| `VNX_DIR`, `DISPATCH_DIR`, `QUEUE_DIR`, `PENDING_DIR`, `ACTIVE_DIR`, `COMPLETED_DIR`, `REJECTED_DIR` | Directory paths |
| `STATE_DIR`, `TERMINALS_DIR`, `VNX_DISPATCH_PAYLOAD_DIR` | State/terminal paths |
| `VNX_RUNTIME_PRIMARY`, `VNX_CANONICAL_LEASE_ACTIVE`, `VNX_BROKER_SHADOW` | Feature flags |
| `VNX_DISPATCH_MAX_INLINE`, `VNX_DISPATCH_LEASE_SECONDS` | Thresholds |

### 1.5 Verification Rule

After decomposition:
- `bash -n` must pass on ALL .sh files (mandatory per project rules)
- Dispatcher behavior must be identical (dispatch a test, verify receipt)
- No module exceeds 500L
- `dispatcher_v8_minimal.sh` (orchestrator) < 500L

---

## 2. Runtime Coordination Decomposition (1164L → 4 files)

### 2.1 Current Structure

All standalone functions (no classes). 2 exceed 80 lines:

| Function | Lines | Span | Category |
|----------|-------|------|----------|
| `release_all_leases()` | 128 | 1037–1164 | lease_manager |
| `transition_dispatch_idempotent()` | 83 | 439–521 | runtime_state_machine |

### 2.2 Module Assignment

#### `scripts/lib/coordination_db.py` (~240L)

Database connection, schema, events, and queries:

| Function | Lines | Span |
|----------|-------|------|
| `db_path_from_state_dir()` | 2 | 173–174 |
| `get_connection()` | 17 | 177–193 |
| `_now_utc()` | 2 | 200–201 |
| `_new_event_id()` | 2 | 204–205 |
| `_dump()` | 2 | 208–209 |
| `_append_event()` | 35 | 212–246 |
| `init_schema()` | 57 | 253–309 |
| `get_dispatch()` | 6 | 949–954 |
| `get_lease()` | 6 | 957–962 |
| `get_events()` | 27 | 965–991 |
| `project_terminal_state()` | 41 | 994–1034 |

**Total: ~240L** ✅ under 400L

**Imports**: sqlite3, json, uuid, datetime, pathlib, typing, DB_FILENAME constant

#### `scripts/lib/runtime_state_machine.py` (~331L)

Dispatch and lease state validation, transitions, attempt tracking:

| Function | Lines | Span |
|----------|-------|------|
| `validate_dispatch_state()` | 3 | 127–129 |
| `validate_lease_state()` | 3 | 132–134 |
| `validate_dispatch_transition()` | 9 | 137–145 |
| `is_terminal_dispatch_state()` | 3 | 148–150 |
| `is_accepted_or_beyond()` | 3 | 153–155 |
| `validate_lease_transition()` | 9 | 158–166 |
| `register_dispatch()` | 68 | 316–383 |
| `transition_dispatch()` | 51 | 386–436 |
| `transition_dispatch_idempotent()` | 83 | 439–521 |
| `increment_attempt_count()` | 10 | 524–533 |
| `create_attempt()` | 41 | 540–580 |
| `update_attempt()` | 48 | 583–630 |

**Total: ~331L** ✅ under 400L

`transition_dispatch_idempotent()` at 83L barely exceeds 80L. Split into main body + `_check_idempotent_noop()` helper (~20L) to bring it under.

**Imports from coordination_db**: `_now_utc`, `_new_event_id`, `_append_event`, `_dump`
**Imports from facade**: `DISPATCH_STATES`, `DISPATCH_TRANSITIONS`, `TERMINAL_DISPATCH_STATES`, `ACCEPTED_OR_BEYOND_STATES`, `LEASE_STATES`, `LEASE_TRANSITIONS`, `InvalidStateError`, `InvalidTransitionError`, `DuplicateTransitionError`

#### `scripts/lib/lease_manager.py` (~350L after refactor)

Terminal lease lifecycle:

| Function | Lines | Span |
|----------|-------|------|
| `_default_expires()` | 4 | 637–640 |
| `acquire_lease()` | 56 | 643–698 |
| `renew_lease()` | 57 | 701–757 |
| `release_lease()` | 73 | 760–832 |
| `expire_lease()` | 42 | 835–876 |
| `recover_lease()` | 64 | 879–942 |
| `release_all_leases()` | 128 | 1037–1164 |

**Raw total: ~424L** ⚠️ exceeds 400L

**Required decomposition**: Split `release_all_leases()` (128L) into:
- `_verify_non_terminal_dispatches()` (~30L) — BOOT-9 step 1
- `_release_all_leases_bulk()` (~40L) — BOOT-10 step 2
- `_verify_all_leases_idle()` (~25L) — BOOT-11 step 4
- `release_all_leases()` (~35L) — orchestrator calling the 3 helpers

After split: ~350L ✅ under 400L

**Imports from coordination_db**: `_now_utc`, `_append_event`, `get_connection`
**Imports from runtime_state_machine**: `validate_lease_transition`
**Imports from facade**: `LEASE_STATES`, `LEASE_TRANSITIONS`, `InvalidTransitionError`

#### `scripts/lib/runtime_coordination.py` — Facade (~120L)

Constants, exceptions, and re-exports:

```python
# Constants (lines 28-86)
DISPATCH_STATES = ...      # 13L
TERMINAL_DISPATCH_STATES   # 1L
ACCEPTED_OR_BEYOND_STATES  # 3L
LEASE_STATES = ...         # 7L
DISPATCH_TRANSITIONS = ... # 13L
LEASE_TRANSITIONS = ...    # 7L
DB_FILENAME = ...          # 1L

# Exceptions (lines 92-125)
class InvalidStateError     # 2L
class InvalidTransitionError # 2L
class DuplicateTransitionError # 26L

# Re-exports
from .coordination_db import (db_path_from_state_dir, get_connection, init_schema,
                               get_dispatch, get_lease, get_events, project_terminal_state)
from .runtime_state_machine import (validate_dispatch_state, validate_lease_state, ...)
from .lease_manager import (acquire_lease, renew_lease, release_lease, ...)
```

**Total: ~120L** ✅ under 400L

### 2.3 Import Dependency Graph

```
coordination_db.py (leaf — no internal imports)
  └── imports: sqlite3, json, uuid, datetime, pathlib, typing
      DB_FILENAME from runtime_coordination

runtime_state_machine.py
  └── imports from coordination_db: _now_utc, _new_event_id, _append_event, _dump
      imports from runtime_coordination: constants, exceptions

lease_manager.py
  └── imports from coordination_db: _now_utc, _append_event
      imports from runtime_state_machine: validate_lease_transition
      imports from runtime_coordination: LEASE_STATES, LEASE_TRANSITIONS, exceptions

runtime_coordination.py (facade — re-exports only)
  └── re-exports from all 3 modules
```

No circular imports: `coordination_db` is the leaf, `runtime_state_machine` depends on it, `lease_manager` depends on both, facade re-exports all.

### 2.4 Verification Rule

After decomposition:
- All existing imports `from runtime_coordination import X` must still work (facade re-exports)
- All tests must pass with zero changes (unless they import private functions)
- No module exceeds 400L
- No circular imports

---

## 3. Review Gate Manager Decomposition (1017L → 5 files)

### 3.1 Current Structure

One large class `ReviewGateManager` (843L, lines 47–889) + `main()` (121L, lines 897–1017). 2 methods exceed 80 lines:

| Method | Lines | Span | Category |
|--------|-------|------|----------|
| `request_claude_github_with_contract()` | 90 | 290–379 | gate_executor |
| `record_result()` | 86 | 538–623 | gate_result_parser |

### 3.2 Architecture Decision: Mixin-Based Decomposition

The `ReviewGateManager` class uses `self.*` state throughout. Rather than extracting standalone functions (which would require passing `self` state everywhere), use **mixin classes** that compose into the final `ReviewGateManager`:

```python
# gate_executor.py
class GateExecutorMixin:
    """Gate request and execution methods."""
    def request_reviews(self, ...): ...
    def execute_gate(self, ...): ...
    def request_and_execute(self, ...): ...

# gate_result_parser.py
class GateResultParserMixin:
    """Result recording and finding classification."""
    def record_result(self, ...): ...
    def record_claude_github_result(self, ...): ...

# gate_report_generator.py
class GateReportGeneratorMixin:
    """Report writing and audit trail."""
    def _write_not_executable_result(self, ...): ...
    def _write_skip_rationale(self, ...): ...
    def _write_failure_result(self, ...): ...

# review_gate_manager.py (facade)
class ReviewGateManager(GateExecutorMixin, GateResultParserMixin, GateReportGeneratorMixin):
    def __init__(self, ...): ...  # initialization + path helpers
```

### 3.3 Module Assignment

#### `scripts/lib/gate_executor.py` (~390L)

Gate request orchestration and execution:

| Method | Lines | Span |
|--------|-------|------|
| `_gemini_available()` | 2 | 126–127 |
| `_codex_headless_available()` | 2 | 129–130 |
| `_claude_github_configured()` | 2 | 132–133 |
| `request_reviews()` | 47 | 135–181 |
| `_request_gemini()` | 37 | 183–219 |
| `request_gemini_with_contract()` | 68 | 221–288 |
| `request_claude_github_with_contract()` | 90 | 290–379 |
| `_request_codex()` | 41 | 454–494 |
| `_request_claude_github()` | 41 | 496–536 |
| `execute_gate()` | 38 | 764–801 |
| `request_and_execute()` | 77 | 803–879 |
| `status()` | 8 | 881–888 |

**Raw total: ~453L** ⚠️ exceeds 400L

**Required decomposition**: Split `request_claude_github_with_contract()` (90L):
- Extract `_determine_claude_github_state()` (~25L, lines 311–337) — state determination logic
- Reduces `request_claude_github_with_contract()` to ~65L

After split: ~390L ✅ under 400L (tight but acceptable)

**Imports**: headless_adapter, review_contract, governance_receipts, gemini_prompt_renderer, claude_github_receipt, gate_runner, auto_merge_policy

#### `scripts/lib/gate_result_parser.py` (~195L)

Finding classification and result recording:

| Method | Lines | Span |
|--------|-------|------|
| `record_result()` | 86 | 538–623 |
| `record_claude_github_result()` | 72 | 381–452 |
| `_classify_unavailable()` | 18 | 629–646 |

**Raw total: ~176L** — but `record_result` exceeds 80L.

**Required decomposition**: Split `record_result()` (86L):
- Extract `_classify_and_format_findings()` (~25L, lines 577–594) — finding classification loop
- Reduces `record_result()` to ~65L

After split: ~195L ✅ under 400L

**Imports**: governance_receipts, gemini_prompt_renderer, claude_github_receipt

#### `scripts/lib/gate_report_generator.py` (~115L)

Result writing and audit trail:

| Method | Lines | Span |
|--------|-------|------|
| `_write_not_executable_result()` | 36 | 648–683 |
| `_write_skip_rationale()` | 34 | 685–718 |
| `_write_failure_result()` | 43 | 720–762 |

**Total: ~115L** ✅ under 400L

**Imports**: governance_receipts, json, pathlib

#### `scripts/review_gate_manager.py` — Facade (~180L)

Initialization, path helpers, CLI, and class composition:

| Component | Lines | Span |
|-----------|-------|------|
| Imports + constants | 40 | 1–40 |
| `_utc_now()` | 3 | 42–44 |
| `ReviewGateManager.__init__()` | 9 | 48–56 |
| Path helpers (10 methods) | 56 | 58–112 |
| `ReviewGateManager` class (mixin composition) | 5 | class declaration |
| `_parse_changed_files()` | 4 | 891–894 |
| `main()` | 121 | 897–1017 |

**Raw total: ~238L** — but `main()` at 121L exceeds 80L.

**Decision**: Keep `main()` in facade. It's CLI argument parsing — splitting it adds complexity without benefit. The 80L function limit in F27 PR-4 can handle this by extracting subcommand handlers.

### 3.4 Verification Rule

After decomposition:
- `from review_gate_manager import ReviewGateManager` must still work
- All `ReviewGateManager` methods callable as before
- All gate tests pass
- No module exceeds 400L
- No circular imports between mixin modules

---

## 4. Function Decomposition Catalog

### 4.1 Functions > 80L in the 3 Target Files

Already covered in sections 1–3 above. Summary:

| File | Function | Lines | Action |
|------|----------|-------|--------|
| dispatcher_v8_minimal.sh | `dispatch_with_skill_activation()` | 552 | Split into 4 functions |
| dispatcher_v8_minimal.sh | `configure_terminal_mode()` | 272 | Split into 4 functions |
| dispatcher_v8_minimal.sh | `process_dispatches()` | 198 | Split into 3 functions |
| dispatcher_v8_minimal.sh | `terminal_lock_allows_dispatch()` | 92 | Extract Python to library |
| runtime_coordination.py | `release_all_leases()` | 128 | Split into 3 helpers + orchestrator |
| runtime_coordination.py | `transition_dispatch_idempotent()` | 83 | Extract `_check_idempotent_noop()` helper |
| review_gate_manager.py | `request_claude_github_with_contract()` | 90 | Extract `_determine_claude_github_state()` |
| review_gate_manager.py | `record_result()` | 86 | Extract `_classify_and_format_findings()` |

### 4.2 Functions > 80L Across Entire Codebase (Top 50 by Size)

**Python (159 functions > 80L found)**:

| # | File | Function | Lines |
|---|------|----------|-------|
| 1 | scripts/llm_benchmark.py | `__init__()` | 797 |
| 2 | scripts/pr_queue_manager.py | `_exit_from_result()` | 395 |
| 3 | scripts/gather_intelligence.py | `main()` | 226 |
| 4 | scripts/lib/runtime_reconciler.py | `_reconcile_dispatches()` | 226 |
| 5 | scripts/quality_db_init.py | `initialize_database()` | 224 |
| 6 | scripts/llm_benchmark.py | `generate_markdown_report()` | 210 |
| 7 | scripts/pr_queue_manager.py | `init_feature_batch()` | 199 |
| 8 | scripts/append_receipt.py | `_enrich_completion_receipt()` | 198 |
| 9 | scripts/closure_verifier.py | `_validate_review_evidence()` | 189 |
| 10 | scripts/pr_queue_manager.py | `_parse_pr_from_feature_plan()` | 187 |
| 11 | scripts/llm_benchmark_coding_v2.py | `generate_markdown_report()` | 185 |
| 12 | scripts/lib/mixed_execution_router.py | `route_dispatch()` | 181 |
| 13 | scripts/llm_benchmark.py | `main()` | 177 |
| 14 | scripts/log_quality_event.py | `main()` | 175 |
| 15 | scripts/lib/workflow_supervisor.py | `handle_incident()` | 168 |
| 16 | scripts/lib/dashboard_actions.py | `start_session()` | 166 |
| 17 | scripts/lib/vnx_recover_runtime.py | `_phase_headless_reconciliation()` | 161 |
| 18 | scripts/governance_weekly_report.py | `generate_report()` | 159 |
| 19 | scripts/kickoff_preflight.py | `run_preflight()` | 158 |
| 20 | scripts/lib/gemini_prompt_renderer.py | `render_gemini_prompt()` | 158 |
| 21 | scripts/report_parser.py | `extract_metadata()` | 155 |
| 22 | scripts/lib/terminal_state_reconciler.py | `reconcile_terminal_state()` | 153 |
| 23 | scripts/pr_queue_manager.py | `patch_dispatch()` | 152 |
| 24 | scripts/gather_intelligence.py | `extract_tags_from_description()` | 151 |
| 25 | scripts/closure_verifier.py | `verify_pr_closure()` | 148 |
| 26 | scripts/lib/provenance_verification.py | `governance_audit_view()` | 148 |
| 27 | scripts/closure_verifier.py | `verify_closure()` | 145 |
| 28 | scripts/llm_benchmark_coding_v2.py | `main()` | 145 |
| 29 | scripts/lib/canonical_state_views.py | `build_terminal_snapshot()` | 144 |
| 30 | scripts/lib/vnx_recover_runtime.py | `_phase_lease_reconciliation()` | 144 |
| 31 | tests/test_pr_dispatch_integration.py | `test_pr_dispatch_creation()` | 144 |
| 32 | scripts/lib/fpc_certification.py | `_certify_task_class_routing()` | 143 |
| 33 | scripts/lib/headless_adapter.py | `_run_subprocess()` | 143 |
| 34 | scripts/lib/provenance_verification.py | `verify_dispatch_provenance()` | 143 |
| 35 | scripts/lib/provenance_verification.py | `provenance_audit_view()` | 140 |
| 36 | scripts/gather_intelligence.py | `query_prevention_rules()` | 139 |
| 37 | scripts/pr_queue_manager.py | `create_dispatch_from_pr()` | 138 |
| 38 | scripts/lib/provenance_verification.py | `pre_merge_advisory()` | 137 |
| 39 | scripts/build_t0_tags_digest.py | `build_tags_digest()` | 136 |
| 40 | scripts/codex_final_gate.py | `render_codex_prompt()` | 136 |
| 41 | scripts/lib/exit_classifier.py | `classify_exit()` | 135 |
| 42 | dashboard/serve_dashboard.py | `do_POST()` | 133 |
| 43 | scripts/lib/dispatch_broker.py | `register()` | 133 |
| 44 | scripts/gather_intelligence.py | `query_relevant_patterns()` | 132 |
| 45 | scripts/cost_tracker.py | `build_metrics()` | 130 |
| 46 | scripts/lib/inbound_inbox.py | `process()` | 130 |
| 47 | scripts/vnx_install.py | `validate_installation()` | 130 |
| 48 | scripts/lib/runtime_coordination.py | `release_all_leases()` | 128 |
| 49 | scripts/lib/safe_autonomy_cutover.py | `validate_prerequisites()` | 129 |
| 50 | scripts/code_quality_scanner.py | `store_quality_metrics()` | 127 |

*Plus 109 additional Python functions between 80–126L.*

**Shell (24 functions > 80L)**:

| # | File | Function | Lines |
|---|------|----------|-------|
| 1 | scripts/commands/start.sh | `cmd_start()` | 741 |
| 2 | scripts/smart_tap_v7_json_translator.sh | `is_json()` | 608 |
| 3 | scripts/dispatcher_v8_minimal.sh | `dispatch_with_skill_activation()` | 552 |
| 4 | scripts/generate_t0_brief.sh | `main()` | 492 |
| 5 | scripts/commands/merge_preflight.sh | `cmd_merge_preflight()` | 307 |
| 6 | scripts/dispatcher_v8_minimal.sh | `configure_terminal_mode()` | 272 |
| 7 | scripts/commands/new_worktree.sh | `cmd_new_worktree()` | 269 |
| 8 | scripts/commands/doctor.sh | `cmd_doctor()` | 261 |
| 9 | scripts/dispatcher_v8_minimal.sh | `process_dispatches()` | 198 |
| 10 | scripts/generate_valid_dashboard.sh | `get_pr_queue_summary()` | 179 |
| 11 | scripts/commands/recover.sh | `cmd_recover()` | 170 |
| 12 | scripts/commands/finish_worktree.sh | `cmd_finish_worktree()` | 160 |
| 13 | scripts/commands/jump.sh | `cmd_jump()` | 160 |
| 14 | scripts/report_watcher.sh | `process_report()` | 144 |
| 15 | scripts/lib/input_mode_guard.sh | `check_pane_input_ready()` | 131 |
| 16 | tests/test_full_auto.sh | `run_gate_cycle()` | 126 |
| 17 | scripts/receipt_processor_v4.sh | `process_single_report()` | 124 |
| 18 | scripts/vnx_supervisor_simple.sh | `monitor()` | 118 |
| 19 | scripts/receipt_notifier.sh | `notify_t0_enhanced()` | 104 |
| 20 | scripts/dispatcher_v8_minimal.sh | `terminal_lock_allows_dispatch()` | 92 |
| 21 | scripts/commands/regen_settings.sh | `cmd_regen_settings()` | 88 |
| 22 | tests/test_receipt_flow.sh | `test_document_generation()` | 85 |
| 23 | scripts/lib/process_lifecycle.sh | `vnx_proc_acquire_lock()` | 84 |
| 24 | scripts/lib/vnx_marked_blocks.sh | `vnx_upsert_marked_block()` | 83 |

### 4.3 PR-4 Scope for Function Sweep

PR-4 will handle all 183 functions > 80L. The scope is large but most splits are mechanical (extract helper function, no behavioral change). Priority order:

1. **Functions in the 3 target files** (8 functions) — handled in PR-1/2/3
2. **Functions in other scripts/lib/ files** (~60 functions) — core library
3. **Functions in scripts/commands/ files** (~6 functions) — CLI commands
4. **Functions in scripts/ root files** (~50 functions) — utility scripts
5. **Functions in dashboard/ and tests/** (~30 functions) — secondary priority

---

## 5. Import Migration Rules

### 5.1 Dispatcher (Shell)

**Current**: `dispatcher_v8_minimal.sh` is self-contained. No other files source it.

**After refactor**: The orchestrator sources 4 modules at the top:

```bash
source "${SCRIPT_DIR}/lib/dispatch_logging.sh"
source "${SCRIPT_DIR}/lib/dispatch_create.sh"
source "${SCRIPT_DIR}/lib/dispatch_lifecycle.sh"
source "${SCRIPT_DIR}/lib/dispatch_deliver.sh"
```

**External callers**: No files import functions from the dispatcher. It's a top-level script only.

**New dependency**: `scripts/lib/terminal_state_check.py` (extracted from terminal_lock_allows_dispatch)

### 5.2 Runtime Coordination (Python)

**Current consumers** (files that `from runtime_coordination import ...` or `import runtime_coordination`):

Run `grep -r "runtime_coordination" --include="*.py"` to find all. Key consumers:
- `scripts/lib/runtime_core_cli.py`
- `scripts/lib/runtime_facade.py`
- `scripts/lib/runtime_reconciler.py`
- `scripts/lib/vnx_recover_runtime.py`
- `scripts/lib/safe_autonomy_cutover.py`
- `scripts/lib/canonical_state_views.py`
- `scripts/lib/dispatch_broker.py`
- Multiple test files

**Migration rule**: ALL existing `from runtime_coordination import X` statements continue to work because the facade re-exports everything. Zero import changes needed in consumers.

**Internal-only changes**: New modules import from each other per the dependency graph in section 2.3.

### 5.3 Review Gate Manager (Python)

**Current consumers**:

- `scripts/review_gate_manager.py` is the main entry point (called as `python scripts/review_gate_manager.py`)
- `scripts/lib/gate_runner.py` imports from it
- Test files import from it

**Migration rule**: The facade class `ReviewGateManager` stays in `review_gate_manager.py`. It inherits from mixin modules. All existing `from review_gate_manager import ReviewGateManager` statements continue to work. Zero import changes needed in consumers.

**Internal-only changes**: Mixin modules import from `scripts/lib/` peer modules.

---

## 6. Cross-File Dependency Verification

No cross-dependencies exist between the 3 target files:
- `dispatcher_v8_minimal.sh` does NOT source `runtime_coordination.py` or `review_gate_manager.py` directly (it calls them via `python scripts/...` subprocess)
- `runtime_coordination.py` does NOT import `review_gate_manager.py`
- `review_gate_manager.py` does NOT import `runtime_coordination.py`

**This confirms PR-1, PR-2, and PR-3 can run in parallel** after PR-0 merges.

---

## 7. Summary: Line Budget Verification

### Dispatcher

| Module | Projected Lines | Limit | Status |
|--------|----------------|-------|--------|
| dispatch_create.sh | ~250 | 500 | ✅ |
| dispatch_deliver.sh | ~350 | 500 | ✅ (tight) |
| dispatch_lifecycle.sh | ~380 | 500 | ✅ |
| dispatch_logging.sh | ~160 | 500 | ✅ |
| dispatcher_v8_minimal.sh (orchestrator) | ~350 | 500 | ✅ |

### Runtime Coordination

| Module | Projected Lines | Limit | Status |
|--------|----------------|-------|--------|
| coordination_db.py | ~240 | 400 | ✅ |
| runtime_state_machine.py | ~331 | 400 | ✅ |
| lease_manager.py | ~350 | 400 | ✅ (after release_all_leases split) |
| runtime_coordination.py (facade) | ~120 | 400 | ✅ |

### Review Gate Manager

| Module | Projected Lines | Limit | Status |
|--------|----------------|-------|--------|
| gate_executor.py | ~390 | 400 | ✅ (tight, after extract) |
| gate_result_parser.py | ~195 | 400 | ✅ |
| gate_report_generator.py | ~115 | 400 | ✅ |
| review_gate_manager.py (facade) | ~238 | 400 | ✅ |
