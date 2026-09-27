# Archived Documentation Manifest

**Generated:** 2026-07-12 (PR-11, `framework-status-audit-and-cockpit` track)
**Purpose:** deterministic, per-file record of what this sweep archived and why.

## Archival rule (by rule, not by volume)

A doc is archived under exactly one of three named sub-rules:

1. **explicit-comparisons** — every `docs/comparisons/*.md` file is cut, per the framework-status-audit's "docs/marketing bloat → CUT" decision, regardless of reachability or staleness.
2. **consolidated-archive** — any pre-existing `docs/archive/*.md` (no underscore) content is folded into `docs/_archive/`. Checked this sweep: `docs/archive/` does not exist on disk — nothing to consolidate.
3. **stale-unreachable** — any `docs/**/*.md` that is BOTH (a) unreachable via markdown links — resolved transitively, following link targets across files — from `docs/DOCS_INDEX.md` or root `README.md`, AND (b) untouched in git for more than 12 months. Threshold for this sweep: last-touch date before **2025-07-12** (today is 2026-07-12).

A file reachable from `DOCS_INDEX.md`/`README.md`, directly or transitively, is never archived, no matter its age. `tests/test_docs_index.py::TestNoStaleUnreachableDocsRemain` runs rule 3 live against the tree so this stays true as docs change.

## This sweep (2026-07-12) — 3 files archived, rule 1 (explicit-comparisons)

| Path (now) | Path (before) | Last touched | Rule |
|---|---|---|---|
| `comparisons/headless_vs_interactive.md` | `docs/comparisons/headless_vs_interactive.md` | 2026-07-05 | explicit-comparisons |
| `comparisons/vnx_vs_claude_code.md` | `docs/comparisons/vnx_vs_claude_code.md` | 2026-03-29 | explicit-comparisons |
| `comparisons/vnx_vs_frameworks.md` | `docs/comparisons/vnx_vs_frameworks.md` | 2026-03-29 | explicit-comparisons |

`docs/comparisons/` is now empty and was removed.

**Rule 3 (stale-unreachable) scan result: 0 matches.** Every `docs/**/*.md` outside `docs/_archive/` at the time of this sweep is either reachable from `DOCS_INDEX.md`/`README.md` (directly, or transitively through an intermediate doc such as `docs/operations/README.md` or `docs/manifesto/README.md`) or was touched within the last 12 months — the two conditions never coincide in the current tree. Prior sweeps already cleared the docs that used to match.

## Pre-existing archive contents (prior sweeps, unchanged by this PR)

40 files, moved in four earlier sweeps. Per-sweep provenance is in [`README.md`](README.md); this PR does not touch them.

## Docs-verversing sweep (archived 2026-09-27, PR-4) — 22 files, operator directive

Operator directive 2026-09-27: refresh all VNX documentation to the state of `main` (v1.6.6), move
non-relevant docs to the archive, merge overlapping docs. Every row below is either a completed,
one-off operational procedure; a runbook for a mechanism that was retired; a duplicate of an active
doc; or an unreferenced design artifact from a feature that shipped without adopting it. None were
rewritten — only moved, per the ADR-style rule that archived docs keep their content.

| Path (now) | Path (before) | Reason |
|---|---|---|
| `operations/AUTONOMOUS_PRODUCTION_GUIDE.md` | `docs/operations/AUTONOMOUS_PRODUCTION_GUIDE.md` | Describes a 70-PR/24-wave autonomous execution plan sourced from a private, not-in-repo planning doc; cites scripts that never shipped (`vnx_preflight.sh`, `t0_evidence_validator.py`) and a `.claude/vnx-system/` layout that no longer exists. Its own "Current State Delta" already flags the default-lane claim as stale — the tmux-spawn lane it describes as default was removed 2026-09-18 |
| `operations/SUPERVISOR_CUTOVER_PER_PROJECT.md` | `docs/operations/SUPERVISOR_CUTOVER_PER_PROJECT.md` | One-off per-project cutover steps for the unified supervisor, gated on "SUP-PR1..PR4 merged" — those PRs are long since on `main`. General ongoing guidance already lives in `UNIFIED_SUPERVISOR.md` |
| `operations/GEMINI_VERTEX_ROUTING.md` | `docs/operations/GEMINI_VERTEX_ROUTING.md` | Self-marked "Retired 2026-09-26" in its own header: `gemini_review` is in `dispatch_spec.RETIRED_GATE_NAMES` and can no longer be requested, so there is no review-gate quota left to route through Vertex |
| `operations/RECEIPT_PROCESSING_FLOW.md` | `docs/operations/RECEIPT_PROCESSING_FLOW.md` | Self-marked "HISTORICAL" in its own header: describes the deprecated `report_watcher.sh` flow. Duplicate of the active `RECEIPT_PIPELINE.md`, which documents the current `receipt_processor.sh` flow |
| `operations/RUNTIME_LIVENESS.md` | `docs/operations/RUNTIME_LIVENESS.md` | Point-in-time liveness measurement dated 2026-07-30, with operator-machine-specific hardcoded paths (`/Users/vincentvandeth/...`) and a tmux-spawn-worker measurement for a lane removed 2026-09-18. The generated, repeatable successor for daemon liveness is `docs/core/DAEMON_LIVENESS.md` |
| `lane-conformity-matrix.md` | `docs/lane-conformity-matrix.md` | Measurement taken at commit `6157a254` (2026-08-08); the doc's own header admits "De tmux-rijen en regelverwijzingen hieronder ... zijn niet bijgewerkt" and all four OI gates it tracked are since closed. The `claude_tmux_subscription` lane it lists as one of "exact drie lanes" was removed 2026-09-18 |
| `operations/MULTI_MODEL_GUIDE.md` | `docs/operations/MULTI_MODEL_GUIDE.md` | A 15-line pointer stub with no content of its own; cites a CLAUDE.md heading ("Subprocess Adapter Feature Flag") that does not exist and a `docs/research/` directory that does not exist. The real per-provider lane map lives in `docs/core/PROVIDER_LANES.md` |
| `contracts/f36-r12/rpc-schemas/*.json` (14 files) | `docs/contracts/f36-r12-rpc-schemas/*.json` | JSON-RPC schema design artifacts for the F36 R12 track. Zero references from code, tests, or any active doc (only a historical `claudedocs/` triage note mentions one filename) |
| `contracts/f36-r12/structured-index.sql` | `docs/contracts/f36-r12-structured-index.sql` | SQL index design paired with the schemas above; same zero-reference status |

Not archived by operator decision (open item): `docs/operations/TRANSCRIPT_BACKUP_ARCHIVE.md` documents a
workstation-level (Mac Mini) backup mechanism outside VNX's runtime scope, a candidate for removal from
this repo entirely rather than archival — left in place pending that decision. `docs/operations/TMUX_SPAWN_LANE.md`
also stays: it is the tombstone every removed-lane reference above points to.

## Docs-verversing sweep (archived 2026-09-27, PR-3) — 5 files, operator directive

Same operator directive as PR-4 above, scoped to `docs/core` and `docs/contracts`. None were
rewritten — only moved. The `docs/contracts/f36-r12-*` artifacts assigned to this dispatch were
already archived by PR-4 (see the row above); no further action was needed for them here.

| Path (now) | Path (before) | Reason |
|---|---|---|
| `core/190_RESIDUAL_BUGFIX_SWEEP_CONTRACT.md` | `docs/core/190_RESIDUAL_BUGFIX_SWEEP_CONTRACT.md` | PR-0 contract for a governance bugfix sweep completed in April 2026 ("All downstream PRs implement against this contract"); the certification test that reads it (`tests/test_residual_sweep_certification.py`) still needs the file's content, so it moves rather than being retired, and the test's path constant is updated in this PR |
| `core/framework-status-audit-and-cockpit_PRD.md` | `docs/core/framework-status-audit-and-cockpit_PRD.md` | PRD for the `framework-status-audit-and-cockpit` track, delivered: `docs/core/SUBSYSTEMS.md`, `vnx_cli/commands/subsystems.py`, and `.github/workflows/subsystems-drift.yml` all exist on `main`. Code comments citing `:89` for the governance-drift rationale (`dashboard/api_subsystems.py`, `scripts/lib/governance_enforcer.py`, `scripts/lib/migration_inventory.py`, `tests/dashboard/test_api_subsystems.py`) are repointed to the archived path in this PR |
| `core/VNX_SYSTEM_BOUNDARIES.md` | `docs/core/VNX_SYSTEM_BOUNDARIES.md` | January 2026 separation plan (`vnx-system/`, `terminals/library/`, `T-MANAGER`, `sessionstart_tmanager.sh`, `t0_pre_dispatch_intelligence.sh`, `sessionstart_t0.sh`); none of those exact paths are tracked in the repo (verified via `git ls-files`). Superseded by ADR-026 (per-project store) and ADR-032 (fabric artifacts in consumers) |
| `core/technical/DISPATCHER_SYSTEM.md` | `docs/core/technical/DISPATCHER_SYSTEM.md` | Self-marked "V7.3 — LEGACY REFERENCE", 1437 lines; `dispatcher_v7_compilation.sh` does not exist, its cited `.claude/vnx-system/...` and `.claude/terminals/library/...` paths are not tracked, and its `src/crawler/core/browser_pool.py` example is from the unrelated SEOcrawler repo |
| `core/technical/CONTEXT_ROTATION_SYSTEM.md` | `docs/core/technical/CONTEXT_ROTATION_SYSTEM.md` | Describes a v2.5 worker context-rotation mechanism (`vnx_context_monitor.sh`, `vnx_handover_detector.sh`, `vnx_rotate.sh`) that is not wired into `.claude/settings.json`'s generated hook list (`docs/core/00_VNX_ARCHITECTURE.md`'s generated hooks block); `vnx_rotation_recovery.sh` and `.claude/vnx-system/scripts/*` it cites are not tracked either. T0 rotation (the live mechanism) is documented in `docs/operations/T0_CONTEXT_ROTATION.md` |
