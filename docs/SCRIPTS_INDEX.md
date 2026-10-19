# VNX Scripts Index

**Status**: Active  
**Last Updated**: 2026-10-18  
**Owner**: VNX Maintainer  
**Purpose**: High-level map of the active script surface in this repository.

---

## How To Read This Index

- `scripts/commands/` contains shell entrypoints used by `bin/vnx`.
- `scripts/` contains operational scripts, daemons, utilities, and helpers.
- `scripts/lib/` contains the reusable implementation modules that power the CLI and runtime.

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
- `scripts/commands/gate.sh` — run the required review gates for a PR (`vnx gate <pr>`)
- `scripts/commands/worktree_release.sh` — governed release path for locked worktrees (`vnx worktree-release`)

`vnx dispatch-agent` is a pip CLI command (`vnx_cli/commands/dispatch_agent.py`), not a
`bin/vnx` subcommand; `scripts/commands/dispatch-agent.sh` is a standalone repo-local
wrapper (see `docs/guides/AGENT_CREATION_GUIDE.md`).

## Runtime And Dispatch

Core dispatch/runtime orchestration:

- `scripts/commands/dispatch.sh` + `scripts/lib/dispatch_cli.py` — the single-entry dispatch door (`vnx dispatch`); decides the lane (`claude_headless`, `kimi`/`glm-harness`/`deepseek-harness` via `scripts/lib/provider_dispatch.py`) and stages/fires the dispatch
- `scripts/pr_queue_manager.py` — queue and promotion management
- `scripts/open_items_manager.py` — open-item creation, digestion, and rescan flow
- `scripts/pr_merge.py` — the only sanctioned merge path (writes the `pr_merged` receipt; a bare `gh pr merge` does not); the checks it runs are in `docs/core/DISPATCH_RULES.md` §2.2
- `scripts/pr_ready.py` + `scripts/lib/pr_readiness.py` — `vnx pr-ready <PR>`: required contexts and gate evidence on the PR head, before the merge door is called
- `scripts/planning_cli.py` — the planning layer behind `vnx horizon`: objectives, deliverables, plan-gate
- `scripts/providers_cli.py` — `vnx providers`: `list` shows the state of each model provider family; the write verbs refuse until every launch function calls `provider_switch.require` (`docs/core/PROVIDER_LANES.md`, section "Provider switch")
- `scripts/runtime_core_cli.py` — runtime-core operator tooling
- `scripts/runtime_cutover_check.py` — runtime-core certification/validation checks
- `scripts/rollback_runtime_core.py` — runtime-core rollback helper

Key implementation modules:

- `scripts/lib/runtime_core.py`
- `scripts/lib/runtime_state_machine.py`
- `scripts/lib/dispatch_router.py`
- `scripts/lib/dispatch_broker.py`
- `scripts/lib/dispatch_envelope.py` — headless envelope lane (`claude_headless`), isolated worktree per dispatch
- `scripts/lib/gate_lane_contract.py` — per-gate lane/diff-size contract (e.g. `max_diff_chars`)
- `scripts/lib/lease_manager.py`
- `scripts/lib/runtime_reconciler.py`
- `scripts/lib/tmux_adapter.py`
- `scripts/lib/subprocess_adapter.py`
- `scripts/lib/subprocess_dispatch.py`

## Receipts, Reports, And Evidence

These scripts process worker outputs into auditable receipts:

- `scripts/append_receipt.py` — canonical receipt append helper
- `scripts/report_parser.py` — parse report markdown into structured receipt fields
- `scripts/receipt_processor.sh` — receipt processing and T0 delivery pipeline (`scripts/report_watcher.sh` is a deprecated `exit 0` stub kept only as a historical marker; it is replaced by this script)
- `scripts/heartbeat_ack_monitor.py` — receipt/ACK confirmation monitoring
- `scripts/receipt_query.py` — read the receipt ledger (`by-dispatch`, `by-pr`, `by-track`, `since`, `digest`) and record a T0's decision on an outcome (`open-outcomes`, `decide`; `docs/core/DISPATCH_RULES.md` §13)

Related docs:

- `docs/operations/RECEIPT_PIPELINE.md`
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
- `scripts/lib/review_seat_evaluation.py` — which reviewer fills which review seat on one PR head; the merge door and the forge check use the same evaluation
- `scripts/lib/forge_gate_publisher.py` — publish a gate verdict as a GitHub check-run (`docs/operations/FORGE_GATE.md`)
- `scripts/learning_loop_nightly.py` — the learning-loop phase of the nightly conversation analyzer (`scripts/conversation_analyzer_nightly.sh`); writes a beacon `vnx doctor` reads

Related docs:

- `docs/intelligence/SELF_LEARNING_LOOP.md`
- `docs/intelligence/SCOUT_PREPASS.md`
- `docs/intelligence/TAG_TAXONOMY.md`
- `docs/intelligence/COST_TRACKING_GUIDE.md`
- `docs/core/technical/INTELLIGENCE_SYSTEM.md`

## Operational Utilities

Common operational/support scripts:

- `scripts/generate_valid_dashboard.sh` — dashboard launcher and validator
- `scripts/check_intelligence_health.py` — health check for intelligence surfaces
- `scripts/cleanup_auto.sh` — cleanup automation
- `scripts/setup_daily_cleanup_cron.sh` — scheduled cleanup helper
- `scripts/send_digest_email.py` — digest email delivery

## T0 Context Guard And Rotation

Enforcement of the 500K T0 rotation, and the successor spawner. Contract and exit codes: `docs/operations/T0_CONTEXT_ROTATION.md`.

- `scripts/vnx_guard.py` — `vnx guard install` and `vnx guard check`
- `scripts/hooks/t0_context_guard.py` — the guard hook (prompt, tool call, Stop)
- `scripts/t0_rotate_spawn.sh` — open the successor T0 window after `/build-log wrap`
- `scripts/lib/t0_context_budget.py` — context size from the transcript and the rotation band
- `scripts/lib/t0_rotation_state.py` — the state the guard and the spawner share
- `scripts/lib/guard_install.py`, `scripts/lib/guard_check.py`, `scripts/lib/hook_coverage.py` — what install writes and what check reads

## CI And Release

- `scripts/ci/check_docs_file_line_refs.py` — fail when a `docs/` file:line reference drifts from the tree
- `scripts/ci/check_function_size_ratchet.py` — a function may not cross or grow past 70 executable lines in a PR
- `scripts/ci/check_changelog_fragments.py` — CHANGELOG entries arrive as `changelog.d/` fragments (`changelog.d/README.md`)
- `scripts/release/assemble_changelog.py`, `scripts/release/changelog_fragments.py` — fold the fragments into `CHANGELOG.md` in the release PR
- `scripts/ci/check_xdist_parity.py`, `scripts/ci/pytest_collection_manifest.py`, `scripts/ci/record_xdist_parity_verdict.py`, `scripts/ci/xdist_parity_history.py` — the xdist shadow job and its parity verdict

## Packages

- `packages/claude-code-toolkit/context-rotation/` — a Claude Code plugin that hands a long session over to a fresh one; on main, not yet released. Its own scripts and tests live inside the package (`packages/claude-code-toolkit/context-rotation/README.md`).

## Libraries

Important shared library areas:

- `scripts/lib/vnx_paths.sh` and `scripts/lib/vnx_paths.py` — canonical path resolution
- `scripts/lib/process_lifecycle.sh` — PID and process lifecycle helpers
- `scripts/lib/gate_*.py` — gate execution, parsing, recording, and artifacts
- `scripts/lib/headless_*.py` — headless runtime, stream, and registry support

## Archived Scripts

Retired scripts are removed from the repository rather than archived. `scripts/_archive/` does not exist on disk. If you are looking for a script that used to exist, check `git log --all -- scripts/<name>` to find when it was removed.
