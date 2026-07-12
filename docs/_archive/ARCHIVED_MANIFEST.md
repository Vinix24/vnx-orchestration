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
