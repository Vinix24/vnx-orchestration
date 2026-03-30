# Changelog

All notable changes to VNX are documented here.

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
