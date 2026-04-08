# VNX Scripts Index

**Status**: Active  
**Last Updated**: 2026-04-08  
**Owner**: VNX Maintainer  
**Purpose**: High-level map of the active script surface in this repository.

---

## How To Read This Index

- `scripts/commands/` contains shell entrypoints used by `bin/vnx`.
- `scripts/` contains operational scripts, daemons, utilities, and helpers.
- `scripts/lib/` contains the reusable implementation modules that power the CLI and runtime.
- `scripts/_archive/` contains retired scripts kept only for historical reference.

This index is intentionally concise. It points you to the canonical areas instead of trying to document every internal helper inline.

## CLI Command Entrypoints

These files implement the main `bin/vnx` command surface:

- `scripts/commands/start.sh` — start VNX sessions and runtime processes
- `scripts/commands/stop.sh` — stop sessions and managed processes
- `scripts/commands/doctor.sh` — validate environment, paths, and required dependencies
- `scripts/commands/recover.sh` — recover from runtime/process inconsistencies
- `scripts/commands/new_worktree.sh` — create isolated VNX worktrees
- `scripts/commands/finish_worktree.sh` — run merge-preflight and finish a worktree
- `scripts/commands/headless.sh` — headless/runtime-adapter workflows
- `scripts/commands/merge_preflight.sh` — run pre-merge validation
- `scripts/commands/registry.sh` — inspect or manage local registry state
- `scripts/commands/roadmap.sh` — roadmap-related CLI helpers

## Runtime And Dispatch

Core dispatch/runtime orchestration:

- `scripts/dispatcher_v8_minimal.sh` — main dispatch delivery entrypoint
- `scripts/pr_queue_manager.py` — queue and promotion management
- `scripts/open_items_manager.py` — open-item creation, digestion, and rescan flow
- `scripts/dispatch_lifecycle_tracker.py` — lifecycle event tracking
- `scripts/runtime_core_cli.py` — runtime-core operator tooling
- `scripts/runtime_cutover_check.py` — runtime-core certification/validation checks
- `scripts/rollback_runtime_core.py` — runtime-core rollback helper

Key implementation modules:

- `scripts/lib/runtime_core.py`
- `scripts/lib/runtime_state_machine.py`
- `scripts/lib/dispatch_router.py`
- `scripts/lib/dispatch_broker.py`
- `scripts/lib/lease_manager.py`
- `scripts/lib/runtime_reconciler.py`
- `scripts/lib/tmux_adapter.py`
- `scripts/lib/local_session_adapter.py`
- `scripts/lib/subprocess_adapter.py`
- `scripts/lib/subprocess_dispatch.py`

## Receipts, Reports, And Evidence

These scripts process worker outputs into auditable receipts:

- `scripts/append_receipt.py` — canonical receipt append helper
- `scripts/report_parser.py` — parse report markdown into structured receipt fields
- `scripts/report_watcher.sh` — watch reports and trigger receipt processing
- `scripts/receipt_processor_v4.sh` — receipt processing and T0 delivery pipeline
- `scripts/heartbeat_ack_monitor.py` — receipt/ACK confirmation monitoring
- `scripts/report_miner.py` — extract learnings and reusable signals from reports

Related docs:

- `docs/operations/RECEIPT_PIPELINE.md`
- `docs/operations/RECEIPT_PROCESSING_FLOW.md`
- `docs/core/11_RECEIPT_FORMAT.md`

## Intelligence And Governance

Primary intelligence/governance scripts:

- `scripts/gather_intelligence.py` — aggregate intelligence and usage signals
- `scripts/intelligence_daemon.py` — background intelligence pipeline
- `scripts/build_t0_quality_digest.py` — quality digest generation for T0
- `scripts/build_t0_tags_digest.py` — tags digest generation for T0
- `scripts/governance_aggregator.py` — governance metrics aggregation
- `scripts/query_quality_intelligence.py` — query quality and intelligence state
- `scripts/review_gate_manager.py` — review-gate policy execution
- `scripts/gate_runner.py` — deterministic gate execution

Related docs:

- `docs/intelligence/TAG_TAXONOMY.md`
- `docs/intelligence/COST_TRACKING_GUIDE.md`
- `docs/core/technical/INTELLIGENCE_SYSTEM.md`

## Operational Utilities

Common operational/support scripts:

- `scripts/generate_valid_dashboard.sh` — dashboard launcher and validator
- `scripts/check_intelligence_health.py` — health check for intelligence surfaces
- `scripts/cleanup_auto.sh` — cleanup automation
- `scripts/session_gc.py` — garbage collection for old session artifacts
- `scripts/setup_daily_cleanup_cron.sh` — scheduled cleanup helper
- `scripts/send_digest_email.py` — digest email delivery

## Libraries

Important shared library areas:

- `scripts/lib/vnx_paths.sh` and `scripts/lib/vnx_paths.py` — canonical path resolution
- `scripts/lib/process_lifecycle.sh` — PID and process lifecycle helpers
- `scripts/lib/dashboard_read_model.py` — dashboard projection layer
- `scripts/lib/gate_*.py` — gate execution, parsing, recording, and artifacts
- `scripts/lib/headless_*.py` — headless runtime, stream, and registry support

## Archived Scripts

Retired scripts live under `scripts/_archive/`. They are not part of the active supported surface and should not be used as references for new work unless you are doing historical investigation.
