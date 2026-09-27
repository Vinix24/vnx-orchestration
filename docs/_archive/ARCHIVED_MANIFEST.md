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

## docs/investigations sweep (archived 2026-09-27)

Moved during the 2026-09-27 docs refresh to the v1.6.6 tree (docs-verversing PR 1). All 11 files in `docs/investigations/` were dated, point-in-time triage/spike reports with zero inbound links from `DOCS_INDEX.md` or any other live doc (`git grep` confirmed the only inbound reference anywhere in the repo was a code comment in `scripts/hooks/pretooluse_worker_scope_enforce.py`, updated to the new path in this same sweep). `docs/investigations/` is now empty and was removed.

| Path (now) | Path (before) | Last touched | Reason |
|---|---|---|---|
| `investigations/20260801-oi-triage-t6-ledger-existence.md` | `docs/investigations/20260801-oi-triage-t6-ledger-existence.md` | 2026-08-01 | Dated OI-existence triage (T6, PR #1302). Superseded — see note below. |
| `investigations/20260801-t8-oi-triage-ledger-existence.md` | `docs/investigations/20260801-t8-oi-triage-ledger-existence.md` | 2026-08-01 | Dated OI-existence triage (T8, PR #1304). Superseded — see note below. |
| `investigations/20260801-r1-oi-triage.md` | `docs/investigations/20260801-r1-oi-triage.md` | 2026-08-01 | Dated OI-existence triage (R1, PR #1315). Latest of the three overlapping T6/T8/R1 triages — see note below. |
| `investigations/20260801-oi-triage-ledger-existence.md` | `docs/investigations/20260801-oi-triage-ledger-existence.md` | 2026-08-01 | Dated OI-existence triage (T2), one-day snapshot. |
| `investigations/20260801-oi-triage-r5.md` | `docs/investigations/20260801-oi-triage-r5.md` | 2026-08-01 | Dated OI-existence triage (R5), one-day snapshot. |
| `investigations/20260801-oi-triage-t12.md` | `docs/investigations/20260801-oi-triage-t12.md` | 2026-08-01 | Dated OI-existence triage (T12), one-day snapshot. |
| `investigations/20260801-t9-oi-triage-ledger-existence.md` | `docs/investigations/20260801-t9-oi-triage-ledger-existence.md` | 2026-08-02 | Dated OI-existence triage (T9); cites `tmux_interactive_dispatch.py`, since removed with the tmux-spawn lane (#1868). |
| `investigations/spike-worker-scope-hook-feasibility.md` | `docs/investigations/spike-worker-scope-hook-feasibility.md` | 2026-07-22 | Feasibility spike for the PreToolUse worker-scope hook, run in the tmux-spawn lane (since removed, #1868). Still cited by `scripts/hooks/pretooluse_worker_scope_enforce.py:20` — that citation was repointed to the new path. |
| `investigations/20260816-blocked-track-deliverables.md` | `docs/investigations/20260816-blocked-track-deliverables.md` | 2026-08-16 | Derived deliverables snapshot for 12 plan-gate-blocked tracks (OI-1560-p3); the tracks DB is the live source. |
| `investigations/20260816-oi1118-unknown-receipts.md` | `docs/investigations/20260816-oi1118-unknown-receipts.md` | 2026-08-16 | Pinned root-cause analysis for OI-1118; not re-verified against current open-item status by this sweep. |
| `investigations/20260816-oi1133-quarantine-causes.md` | `docs/investigations/20260816-oi1133-quarantine-causes.md` | 2026-08-16 | Pinned root-cause analysis for OI-1133; not re-verified against current open-item status by this sweep. |

**On the three overlapping 2026-08-01 OI-existence triages (T6, T8, R1):** all three re-tested largely the same open-item set (OI-105/225/546/557/559/560 appear in all three) on the same day and reached conflicting verdicts on individual items — e.g. OI-105 is ONBEOORDEELBAAR in T6 and T8 but ACHTERHAALD in R1; OI-223 is BESTAAT-GROOT in T6 but ACHTERHAALD in T8. **R1 (PR #1315) is the latest stand**: it merged after both T6 (#1302) and T8 (#1304), covers the widest item range (OI-005 through OI-629), and its OI-105 verdict is the one that stuck. The open-item ledger, not any of these three files, is the authoritative record of current OI status — none of the three is a template for a live triage cadence.

## Pre-existing archive contents (prior sweeps, unchanged by this PR)

40 files, moved in four earlier sweeps. Per-sweep provenance is in [`README.md`](README.md); this PR does not touch them.
