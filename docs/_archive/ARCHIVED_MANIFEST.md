# Archived Documentation Manifest

**Generated:** 2026-07-12 (PR-11, `framework-status-audit-and-cockpit` track)
**Purpose:** deterministic, per-file record of what this sweep archived and why.

## Archival rule (by rule, not by volume)

A doc is archived under exactly one of three named sub-rules:

1. **explicit-comparisons** — every `docs/comparisons/*.md` file is cut, per the framework-status-audit's "docs/marketing bloat → CUT" decision, regardless of reachability or staleness.
2. **consolidated-archive** — any pre-existing `docs/archive/*.md` (no underscore) content is folded into `docs/_archive/`. Checked this sweep: `docs/archive/` does not exist on disk — nothing to consolidate.
3. **stale-unreachable** — any `docs/**/*.md` that is BOTH (a) unreachable via markdown links — resolved transitively, following link targets across files — from `docs/DOCS_INDEX.md` or root `README.md`, AND (b) untouched in git for more than 12 months. Threshold for this sweep: last-touch date before **2025-07-12** (today is 2026-07-12).
4. **dead-mechanism** — a doc that is the canonical contract for a code path confirmed absent or removed from the running system, per operator decision (2026-09-27 docs refresh, `claudedocs/2026-09-24-tmux-opruimplan.md`), regardless of reachability or staleness. Used for the tmux pane-delivery contracts: the daemon they govern (`dispatcher_minimal.sh`) shows `absent` in `docs/core/DAEMON_LIVENESS.md`, and the tmux-spawn claude lane they assumed was removed 2026-09-18 (`docs/operations/TMUX_SPAWN_LANE.md`).

A file reachable from `DOCS_INDEX.md`/`README.md`, directly or transitively, is never archived under rule 3, no matter its age. Rule 4 can archive a reachable file when the mechanism it documents is confirmed dead. `tests/test_docs_index.py::TestNoStaleUnreachableDocsRemain` runs rule 3 live against the tree so that rule stays true as docs change.

## Sweep 2026-09-27 — 8 files archived, rule 4 (dead-mechanism)

Docs-refresh audit (operator decision 2026-09-27): archive the contracts for the tmux-pane delivery pipeline (`dispatcher_minimal.sh` + `dispatch_deliver.sh` send-keys/paste-buffer transport, `pane_manager.sh` pane titling, the V7 JSON→Markdown popup translator). That dispatcher daemon is `absent` per `docs/core/DAEMON_LIVENESS.md` and has been since before the tmux-spawn claude lane was removed on 2026-09-18 (`docs/operations/TMUX_SPAWN_LANE.md`). The pane-addressing half of the tmux injection path (`pane_manager.sh` discovery, `input_mode_guard.sh`) is not part of this sweep — `claudedocs/2026-09-24-tmux-opruimplan.md` tracks that work separately (plakken P05–P07) because kimi/codex/gemini dispatches still use tmux panes today; this sweep only removes the dead claude-pane-specific delivery documentation.

| Path (now) | Path (before) | Rule |
|---|---|---|
| `core/10_JSON_DISPATCH_FORMAT.md` | `docs/core/10_JSON_DISPATCH_FORMAT.md` | dead-mechanism |
| `core/21_TMUX_IDENTITY_INVARIANTS.md` | `docs/core/21_TMUX_IDENTITY_INVARIANTS.md` | dead-mechanism |
| `core/90_DELIVERY_FAILURE_LEASE_CONTRACT.md` | `docs/core/90_DELIVERY_FAILURE_LEASE_CONTRACT.md` | dead-mechanism |
| `core/110_INPUT_READY_TERMINAL_CONTRACT.md` | `docs/core/110_INPUT_READY_TERMINAL_CONTRACT.md` | dead-mechanism |
| `core/140_REQUEUE_AND_CLASSIFICATION_ACCURACY_CONTRACT.md` | `docs/core/140_REQUEUE_AND_CLASSIFICATION_ACCURACY_CONTRACT.md` | dead-mechanism |
| `core/150_DELIVERY_SUBSTEP_OBSERVABILITY_CONTRACT.md` | `docs/core/150_DELIVERY_SUBSTEP_OBSERVABILITY_CONTRACT.md` | dead-mechanism |
| `core/160_DELIVERY_FAILURE_LOGGING_CONTRACT.md` | `docs/core/160_DELIVERY_FAILURE_LOGGING_CONTRACT.md` | dead-mechanism |
| `contracts/TERMINAL_STARTUP_SESSION_CONTROL_CONTRACT.md` | `docs/contracts/TERMINAL_STARTUP_SESSION_CONTROL_CONTRACT.md` | dead-mechanism |

Notes per file:

- `10_JSON_DISPATCH_FORMAT.md` documents Smart Tap V7's JSON→Markdown popup translator. Smart Tap is `absent` in `docs/core/DAEMON_LIVENESS.md`; its examples also predate this repo (`src/crawler/plugins`, a SEOcrawler path).
- `21_TMUX_IDENTITY_INVARIANTS.md`, `90_DELIVERY_FAILURE_LEASE_CONTRACT.md`, `110_INPUT_READY_TERMINAL_CONTRACT.md`, `140_REQUEUE_AND_CLASSIFICATION_ACCURACY_CONTRACT.md`, `150_DELIVERY_SUBSTEP_OBSERVABILITY_CONTRACT.md`, `160_DELIVERY_FAILURE_LOGGING_CONTRACT.md` all govern the `dispatch_with_skill_activation()` delivery sequence and pane-mapping in `dispatcher_minimal.sh`, which does not run (`docs/core/DAEMON_LIVENESS.md`: `dispatcher` — absent). Archiving 110 and 140 also resolves the two duplicate doc-number prefixes under `docs/core/` (a second, unrelated `110`-slot is free again; `140_DASHBOARD_READ_MODEL_CONTRACT.md` is now the sole live `140`).
- `TERMINAL_STARTUP_SESSION_CONTROL_CONTRACT.md` documents the fixed T0–T3 2x2 tmux dev-profile grid (SP-4: "Dev profile must spawn all four terminals"). `vnx start --profile` (`scripts/commands/start.sh:109-127`) and `POST /api/operator/session/start` (`dashboard/serve_dashboard.py:648`) both still exist, but the fixed four-terminal model they were built for is superseded by ephemeral per-dispatch workers (`docs/manifesto/ARCHITECTURE.md`), and the contract's own `VNX_PROFILE` preset variable (§2.4) is read nowhere in the tree — only the unrelated `VNX_PROFILE_SELECTOR` is. The contract describes a startup shape the runtime no longer builds.

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
