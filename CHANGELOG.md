# Changelog

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
