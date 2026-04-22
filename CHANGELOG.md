# Changelog

## W0 PR 3 — terminal_state_check.py regression fix (2026-04-22)

- **fix(w0-pr3)**: Restore comprehensive `scripts/lib/terminal_state_check.py` deleted in c90615e; add `tests/test_terminal_state_check_regression.py` to prevent re-deletion (12 tests, 12 passed)
## Unreleased

### Bug Fixes

- **W0 PR-2 fix**: `receipt_processor_v4.sh` — fix shell quoting in `_auto_release_lease_on_receipt` (array-based args replace unquoted `${:+}` expansion) and fix conflicting state on `task_timeout+no_confirmation` (skip auto-release when shadow intentionally keeps terminal blocked); 8 new tests (22 total)

### Features

- **W0 PR-2**: Auto-lease-release on task receipt — `receipt_processor_v4.sh` now calls `release-on-receipt` automatically on `task_complete`/`task_failed`/`task_timeout` events, eliminating the need for manual `release-on-failure` after every worker receipt; `RuntimeCore.release_on_receipt()` resolves generation internally with dispatch-id ownership guard and idempotent idle-terminal handling
### Security
- **W0 PR-4 security fix**: `vnx_snapshot.py` — path traversal (Zip Slip) + symlink hardening: `do_restore` now uses `tarfile.extractall(filter="data")` (Python 3.12 safe extraction, raises on path-traversal/absolute-symlink members) instead of the previous unsafe `extractall` with suppressed warnings; `do_snapshot` now filters out absolute symlinks and relative symlinks that escape `.vnx-data/` before they enter the archive; 5 new security tests (17 total)

### Fixes
- **W0 PR-5 fix**: `.github/workflows/burn-in-headless.yml` — remove `skip_billing_gate` input and its conditional job guard (billing safety now unconditional); fix unexpanded `$VNX_HOME` in single-quoted heredoc by using `os.environ.get("VNX_HOME")` in Python instead of shell expansion

### Features
- **W0 PR-6**: `scripts/lib/dispatch_instruction_validator.py` — dispatch instruction template validator (D-1..D-8): Dispatch-ID format, description presence, scope item count thresholds (warn ≥9/block ≥16), unbounded-task language detection, gate/quality-gate alignment, file directory breadth, instruction size, and success-criteria presence; 35 tests in `tests/test_dispatch_instruction_validator.py`
- **W0 PR-5**: `.github/workflows/burn-in-headless.yml` — scheduled weekly burn-in CI (Sunday 02:00 UTC) running billing-safety gate (BS-1..BS-6) followed by burn-in certification (B-1..B-10), snapshot tooling regression, and fixture smoke checks; `workflow_dispatch` for manual runs; zero API cost (CLI stub via `VNX_HEADLESS_CLI=echo`)
- **W0 PR-5**: `scripts/lib/exit_classifier.py` — maps subprocess exit conditions to named failure classes (`SUCCESS/TIMEOUT/TOOL_FAIL/INFRA_FAIL/NO_OUTPUT/INTERRUPTED/PROMPT_ERR/UNKNOWN`) with retryability, signal extraction, and operator hints
- **W0 PR-5**: `scripts/lib/log_artifact.py` — structured human-readable run-log writer (`<run_id>.log`) and raw output capture (`<run_id>.out`) for operator inspection without file spelunking
- **W0 PR-5**: `scripts/lib/headless_inspect.py` — operator inspection tools: `format_run_line`, `format_run_detail`, `list_runs`, `build_health_summary`, `format_health_summary`
- **W0 PR-5**: `tests/conftest.py` — shared pytest fixtures (`vnx_state_dir`, `vnx_registry`, `vnx_artifact_dir`, `vnx_dispatch_dir`, `vnx_fake_project`, `vnx_snapshot_dir`) for burn-in and snapshot test suites; `make_vnx_dispatch_bundle` factory fixture
- **W0 PR-5**: `tests/fixtures/dispatch_bundle_research.json` + `dispatch_bundle_analysis.json` — CI fixture bundles for headless adapter integration tests
- **W0 PR-5**: `tests/test_billing_safety.py` — 12 billing-safety assertions across BS-1..BS-6: no SDK imports, no direct API URLs, no hardcoded keys, no key assignments, CLI-only subprocess, clean fixture files
- **W0 PR-4**: `vnx snapshot/restore/quiesce-check` — CLI tools for project-state backup and migration readiness: tarball + SQL dump of `.vnx-data/`, fail-safe restore with overwrite guard, and read-only quiesce verification across 4 conditions (active dispatches, held leases, in-flight gates, uncommitted changes)
- **W0 PR-1**: `scripts/dispatcher_supervisor.sh` — dedicated auto-restart supervisor for `dispatcher_v8_minimal.sh` with exponential backoff (2s→60s), stale singleton lock cleanup before each restart, SIGTERM-safe child shutdown, and `status` subcommand
- **F32 Wave D PR-1**: T2/T3 default subprocess delivery — `deliver_dispatch_to_terminal` now defaults T1/T2/T3 to subprocess adapter; T0 remains tmux by default; `VNX_ADAPTER_Tx=tmux` opts any terminal back to tmux
- **F36 PR-1**: T0 decision summarizer (`t0_decision_summarizer.py`) — haiku-powered structured decision log writer with file-locking JSONL append, log rotation, and assembler query interface
- **F36 PR-1b**: T0 decision log passive writer (`t0_decision_log.py`) — zero-LLM path converting decision_executor events to JSONL records with cursor tracking for idempotent incremental replay
- **F36 PR-233 fix**: `_rotate_if_needed` holds exclusive lock across full copy+truncate to prevent concurrent-writer data loss; `process_events_file` resets stale cursor when it exceeds file length after source reset
- **F36 PR-233 re-gate fix**: inode-based cursor invalidation in `process_events_file` detects source-file replacement (same or greater line count) and resets cursor to 0; `.claude/scheduled_tasks.lock` untracked and added to `.gitignore`
- **F36 PR-233 final fix**: parse-before-advance in `process_events_file` — partial trailing JSON line does not advance cursor (retried next invocation); malformed non-last lines log warning and advance as before
- **F36 PR-233 round-4 fix**: legacy cursor upgrade in `process_events_file` — cursor written without inode (legacy `save_cursor` format) is upgraded with current inode even when no new events exist, enabling same-length file replacement detection on all subsequent runs
- **F36 Wave B PR-2**: T0 escalations log (`t0_escalations_log.py`) — passive JSONL writer for escalation records with dual adapter hooks: `decision_executor._handle_escalate()` emits executor-source records; `governance_escalation.transition_escalation()` emits governance-source records with full entity/trigger data; batch-replay CLI with inode-based cursor tracking
- **F36 Wave B PR-1**: `VNX_ADAPTER_T0=subprocess` cutover flag — `is_headless_t0()` added to receipt processor; T0 snapshot annotated with `adapter/headless` fields when headless; `dispatch_deliver.sh` documents explicit T0 subprocess support; `heartbeat_ack_monitor` docstring updated for T0 coverage
- **F36 Wave C PR-1**: Shadow mode decision parity harness (`shadow_mode_runner.py`) — runs the headless T0 decision engine in dry-run mode against recent trigger events, compares shadow decisions to the actual decision log, and generates JSONL + markdown parity reports under `{VNX_DATA_DIR}/shadow_parity/`; 64 tests covering all public functions
- **F36 Wave C PR-239 fix**: Shadow runner pairing correctness — replaced positional event↔decision alignment with `dispatch_id`-keyed lookup (FIFO fallback for non-dispatch events); prevents stale pairings when cursor lag or independent "last N" slices cause index drift; 12 new tests, 76 total
### Fixes
- **F32-R3**: `deliver_via_subprocess` now fail-closes on non-zero subprocess exit code — `adapter.observe()` is checked after events are drained; non-zero returncode returns `success=False` regardless of parsed event count; fixes broken test assertions in `test_subprocess_dispatch.py` and `test_subprocess_dispatch_integration.py` (`result is True/False` → `result.success is True/False`)
- **F36-R8 PR-234**: Fix cross-platform `stat` portability in `check_flood_protection()` (GNU/Linux compatibility); defer SHA fallback warning until after `log()` is defined; manual mode now honors last-processed watermark in `should_process_report()`

## v0.9.0 — Streaming + Autonomous Loop + A/B Test (2026-04-11)

### Features
- **F42 PR-1**: Restore EventStore from git history + dashboard archive endpoints for historical dispatch event retrieval
- **F42 PR-2**: Headless T0 decision loop — decision parser extracted from replay harness, decision executor with 5 decision types and loop guards, trigger wiring for closed autonomous loop
- **A/B Test**: First systematic comparison of interactive vs headless execution across F40 (moderate) and F42 (complex) — published results in docs/research/

### Research
- Published headless A/B test results: docs/research/HEADLESS_AB_TEST_RESULTS.md
- Finding: headless produces functionally equivalent output with ~4% less LOC and ~18% fewer tests
- Conclusion: execution mode does not determine quality — instruction quality does

## v0.8.0 — Headless Intelligence & Governance Profiles (2026-04-11)

### Features
- **F39**: Headless T0 benchmark — decision framework with deterministic pre-filter (Level-1: 100%, Level-2: 73-87%, Level-3: 67-78%), context assembler, replay harness, file-based gate locks (#204)
- **F41**: Intelligence pipeline activation — governance aggregator backfill (722 metrics, 58 SPC control limits), nightly pipeline scheduling via launchd, quality digest with real SPC data (#206)
- **F41**: 3-layer headless trigger system — file watcher on unified_reports, silence watchdog (10-min stale lease/dispatch detection), optional haiku LLM triage, 366 LOC (#206)
- **Headless dispatch writer** — programmatic dispatch creation for autonomous T0 orchestration (#207)
- **Governance profiles** — config-driven review profiles (default/light/minimal) replacing hardcoded business/coding split, configurable via `.vnx/governance_profiles.yaml` (#207)

### Fixes
- **Subprocess adapter**: Add `--dangerously-skip-permissions` for headless `claude -p` write/edit capability (#207)
- **Receipt processor**: Replace 10-minute time cutoff with watermark-based processing; update watermark after sweep, not per-file (#206)
- **CI**: Replace hardcoded absolute paths in launchd plists with install-time placeholders (#206)
- **Receipt processor**: Handle `on_moved` events for atomic file delivery (#206)

### Docs
- README: Add headless workers, multi-provider review gates, and mission control dashboard sections (#208)

### Housekeeping
- System cleanup: blocker fixes, ~25K LOC dead code removed, doc updates (#205)
- Unified T0 state builder replacing 8+ startup scripts (#200)

## v0.7.x — F38 Dashboard Unified (2026-04-10)

### Features
- **F38 PR-2**: Dashboard frontend — session history browser (`/operator/reports`), agent selector component, domain filter tabs (Coding/Analytics), Reports sidebar nav link, SWR hooks and types for reports and agents

## v0.6.x — F37 Auto-Report Pipeline (2026-04-08)

### Fixes
- **fix-2**: Stop hook uses `git rev-parse --show-toplevel` for PROJECT_ROOT — eliminates symlink confusion causing assembler not to be invoked; activate F37 with `VNX_AUTO_REPORT=1` default in `vnx_paths.sh`
- **fix-3**: Heartbeat monitor skips subprocess-adapter terminals (`VNX_ADAPTER_T*=subprocess`) to prevent ghost `task_started` events and phantom leases; activate haiku classifier with `VNX_HAIKU_CLASSIFY=1` in `vnx_paths.sh`

### Features
- **F37 PR-5**: Receipt processor integration and end-to-end tests — 39 tests covering auto-generated report validation, tag flow integrity, manual report backward compatibility, subprocess trigger path, and end-to-end fixture through `ReportParser`
- **F37 PR-5**: Fix `render_markdown()` to include `**Terminal**` field required for receipt processor terminal detection

## v0.6.0 — Headless Pipeline + Post-Chain Refactoring (2026-04-07)

### Features
- **F31**: Headless worker resilience — timeout protection via `select.select()`, lease heartbeat renewal, health monitoring daemon, LLM failure diagnosis
- **F32**: T1 as headless backend-developer — pure `claude -p` subprocess execution, no tmux dependency
- **F33**: Dashboard domain filter — agent selector by name, domain filter tabs (Coding/Content/All)
- **F34**: Skill context inlining — 3-tier CLAUDE.md resolution for headless workers (`agents/{role}` → `.claude/skills/{role}` → `.claude/terminals/{terminal}`)
- **F35**: End-to-end headless pipeline certification — 10/10 evidence checks PASS, 268 subprocess/headless tests, production-ready verdict

### Refactoring
- **F36**: Post-chain code housekeeping — 10 oversized modules split across 3 parallel tracks, all under 800-line/70-function thresholds
- Decision summarizer (`t0_decision_summarizer.py`) — haiku-powered T0 session summary
- Orchestrator agent directory (`agents/orchestrator/`) — condensed CLAUDE.md for headless T0

### Architecture (planning, not yet implemented)
- Headless T0 feasibility study — CONDITIONAL GO verdict
- State architecture for stateless T0 sessions (6.5% token budget)
- Framework comparison (7 frameworks: LangGraph, CrewAI, OpenAI SDK, AG2, Mastra, Claude SDK, n8n)
- Governance & intelligence layer architecture (stream-based reporting, tag pipeline, quality checks)
- Repository housekeeping — internal docs moved to private folder, contracts reorganized

---

All notable changes to VNX are documented here.

## v0.5.2 — Dashboard Agent Stream (Feature 29)

Released: 2026-04-06

Highlights:
- EventStore NDJSON persistence for agent stream events with atomic append and file locking (PR-1)
- Open-item auto-close on dispatch completion and SubprocessAdapter integration (PR-1)
- SSE endpoint `GET /api/agent-stream/{terminal}` for real-time event streaming with `since` reconnection (PR-2)
- Stream status endpoint `GET /api/agent-stream/status` listing terminals with active event data (PR-2)
- Dashboard Agent Stream page with terminal selector, color-coded event rendering, auto-scroll, and auto-reconnect (PR-3)
- Sidebar "Agent Stream" link under Operator section (PR-3)

## v0.5.1 — Terminal Startup And Session Control (Feature 26)

Released: 2026-04-04

Highlights:
- profile-aware session startup: coding_strict projects get 2x2 tmux layout (4 panes), business_light projects get single terminal
- session stop with clean tmux teardown via vnx stop
- dry-run mode returns planned actions without executing side effects
- dashboard session control buttons (Start, Stop, Attach) on project cards with pending states and outcome display
- serve_dashboard.py module split: extracted api_operator.py (762 lines) and api_token_stats.py (380 lines), reducing serve_dashboard.py from ~1570 to 438 lines
- 208 tests across backend (183 Python) and frontend (25 TypeScript) covering session lifecycle, profile detection, layout creation, dry-run safety, and UI interactions

Resolves:
- OI-373: dashboard_actions.py:start_session refactored with profile-aware direct tmux path
- OI-374: serve_dashboard.py decomposed into focused modules (438 + 762 + 380 lines)

## v0.5.0 — Governance Runtime Upgrade

Released: 2026-03-30

This release consolidates the largest upgrade to VNX since the initial public preview. Compared to `v0.1.0`, VNX now has a much stronger orchestration core, better recovery and worktree handling, richer intelligence and receipt pipelines, a dashboard attention model, and a significantly more mature governance surface.

Highlights:
- one-command worktree lifecycle with deterministic gates
- governance-aware finish flow and stronger pre-merge enforcement
- hardened dispatcher/tmux delivery and `vnx recover`
- intelligence export/import and self-learning feedback loop
- token/model tracking in receipts and analytics
- dashboard attention model, event timeline, and terminal health views
- Codex CLI and multi-model orchestration improvements
- configurable per-terminal models and Opus 4.6 1M default
- improved public README and documentation surface

Representative merged work since `v0.1.0`:
- dispatch lifecycle, queue, and receipt delivery hardening
- context rotation stabilization and lifecycle hooks
- lease reliability and terminal unlock behavior
- git worktree support with provenance tracking
- outbox delivery pattern and stale-pending catchup
- role-aware intelligence filtering and session analytics
- intelligence feedback loop and recommendation tracking
- dashboard attention model and operator visibility improvements
- metrics/token tracking and model detection fixes

Upgrade note:
- This is still a pre-1.0 release.
- The system is substantially beyond early preview quality, but long-running operational proving and broader adoption hardening are still ongoing.

## v0.1.0 — Public Preview

Released: 2026-02-22

Initial public preview release of VNX.
