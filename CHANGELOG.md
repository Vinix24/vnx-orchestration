# Changelog

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
