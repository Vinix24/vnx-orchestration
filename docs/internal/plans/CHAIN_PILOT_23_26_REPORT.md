# Chain Pilot Report: Features 23–26

**Date**: 2026-04-04
**Chain Window**: 2026-04-03 – 2026-04-04
**Features**: F23 Dashboard Kanban, F24 Open Items & Gate Toggle, F25 Governance Digest, F26 Terminal Startup
**PRs**: #126–#150 (20 merged, 5 superseded)
**Gate Provider**: Gemini via Vertex AI (exclusive — Codex exhausted mid-F21)

---

## 1. Timeline Summary

| Feature | PR-0 | PR-1 | PR-2 | PR-3 | PR-4 | Merged |
|---------|------|------|------|------|------|--------|
| F23 — Dashboard Kanban | #126 | #127 | #128 | #130 | #131 | 2026-04-03 |
| F24 — Open Items & Gate Toggle | #132 | #134 | #135 | #136 | #137 | 2026-04-03 |
| F25 — Governance Digest | #138 | #140 | #142 | #143 | #144 | 2026-04-03/04 |
| F26 — Terminal Startup | #146 | #147 | #148 | #149 | #150 | 2026-04-04 |

**Superseded PRs** (opened then replaced by corrected versions):
- #129 — F23 PR-3 (navigation tests, superseded by #130)
- #133 — F24 PR-1 (gate config, superseded by #134)
- #139 — F25 PR-1 (digest runner, superseded by #140)
- #141 — F25 PR-2 (digest API, superseded by #142)
- #145 — F26 PR-0 (contract, superseded by #146)

All 4 features completed in approximately 18 hours wall-clock time.

---

## 2. Checkpoint Outcomes

| Feature | Checkpoint A (PR-0) | Checkpoint B (PR-2) | Checkpoint C (PR-4) |
|---------|---------------------|---------------------|---------------------|
| F23 | GO | GO | GO — certification merged |
| F24 | GO | GO | GO — certification merged |
| F25 | GO | GO | GO — certification merged |
| F26 | GO | GO | GO — certification merged |

No NO-GO decisions were triggered. All checkpoint evidence (contracts, gate results, reports) was present at each gate.

---

## 3. Gate Provider: Gemini via Vertex AI

Codex was exhausted mid-F21 (PR #118 timeframe). The chain switched to Gemini 2.5 Pro via Vertex AI for all remaining gates.

**F23-26 gate statistics**:
- **Provider**: 100% Gemini (0 Codex, 19 Gemini review results)
- **Verdicts**: All 14 tracked PRs completed successfully
- **Blocking findings**: 0 across all PRs
- **Advisory findings**: 0 across all PRs
- **Average duration**: 38s per gate (range: 31–59s)

**Comparison with F18-22**:
- F18-22: 19 Codex gates, 7 Gemini gates (mixed)
- F23-26: 0 Codex gates, 19 Gemini gates (pure Gemini)

---

## 4. Gate Failures and Anomalies

### Gemini Shallow Reviews

Gemini via Vertex AI produced zero blocking and zero advisory findings across all 19 gate executions. This is a significant anomaly compared to the F18-22 chain where Codex regularly produced blocking findings requiring rework dispatches.

**Root cause**: Gemini prompt enrichment (#119) was implemented mid-chain to inject inline file contents, but the Gemini model still tends to produce shorter, less probing reviews compared to Codex. The `blocking_findings` and `advisory_findings` arrays are always empty.

**Risk**: The gate is providing process evidence (the gate ran and passed) but limited review depth. It is functioning as an audit checkpoint rather than a true adversarial code review. This was an accepted tradeoff given Codex unavailability.

### Missing Gate Results

6 PRs have no gate result files on disk (#130, #134, #140, #142, #146, #147). These were either:
- Merged before the gate was executed (fast-follow corrections)
- Gate results lost during the supersede/re-open cycle

This represents a coverage gap — 6 of 20 merged PRs (30%) have no gate evidence on disk.

---

## 5. Infrastructure Fixes During Chain

### 5.1 Vertex AI Gate Routing
**Commits**: `bfdb891`, `1af0368`
Implemented Vertex AI routing in `gate_runner.py` to use `google-cloud-aiplatform` SDK when Codex is unavailable. This enabled the full Gemini gate flow.

### 5.2 Gemini Prompt Enrichment
**PR**: #119 (`3f96aba`)
Gemini was reviewing PRs without seeing file contents — it only received PR metadata. Fix enriched the prompt with inline file contents for meaningful review.

### 5.3 PR #119 Merge Conflict Resolution
**Commit**: `14f44e9`
Prompt enrichment PR conflicted with main after Vertex AI routing landed. Required manual conflict resolution before merge.

### 5.4 vnx_paths.py Cross-Contamination Fix
The `vnx_paths.py` module was resolving `.vnx-data/` relative to the wrong project directory, causing cross-project data contamination between the main repo and worktrees. Fixed by implementing cwd-based `.vnx-data` detection.

### 5.5 ClearContext Delivery Bug
`dispatch_metadata.sh` defaulted `clear_context` to `false`, causing dispatches to arrive without clearing prior conversation context. Changed default to `true`.

### 5.6 Context Monitor Hook Walk-Up Resolution
**Commit**: `7aff16a`
The context monitor forwarder hook failed to locate the `.vnx/` directory when invoked from subdirectories. Fixed with walk-up directory resolution.

### 5.7 panes.json Stale Pane IDs
After tmux restarts, `panes.json` retained stale pane IDs that no longer mapped to live tmux panes. This caused dispatcher delivery failures until pane discovery was re-run.

### 5.8 Stale Supervisor Process
A stale `vnx-system-wt-upgrade` supervisor process from a prior worktree operation was consuming resources and holding locks. Required manual kill.

### 5.9 PreToolUse Hook Errors
Hook paths referenced files from prior worktree locations that no longer existed. Fixed by updating paths to current worktree.

### 5.10 serve_dashboard.py Module Split
The `serve_dashboard.py` monolith was split into:
- `serve_dashboard.py` — core server + routing
- `api_operator.py` — operator/kanban/session endpoints
- `api_token_stats.py` — token statistics endpoints

This reduced the main file from ~1100+ lines to a manageable core.

### 5.11 Dispatcher Pending-Scan Issue
The dispatcher failed to scan `pending/` directory for new dispatches due to pane discovery failure. Root cause was stale pane IDs (5.7).

---

## 6. Open Items

**Reported in dispatch**: 754 open (57 blockers, 60 warns, 627 info).

**Note**: The `open_items.ndjson` state file currently shows 0 items on disk, indicating the open-item ledger was either reset during the chain or the items were tracked in a different location (possibly per-feature certification reports or the unified report stream).

---

## 7. Runtime Issues

### 7.1 Cross-Project Contamination
`vnx_paths.py` resolved `.vnx-data/` from the git root rather than the current working directory, causing one worktree's dispatches and state to bleed into another project. This was the most impactful runtime issue.

### 7.2 Stale Processes
The `vnx-system-wt-upgrade` supervisor and stale hook processes persisted across tmux restarts, causing resource contention and lock conflicts.

### 7.3 Dispatcher Not Scanning Pending
Combined effect of stale pane IDs and vnx_paths contamination caused the dispatcher to miss pending dispatches entirely. Required pane rediscovery + path fix before dispatches could flow.

### 7.4 Superseded PR Volume
5 PRs were opened then superseded by corrected versions (25% rework rate on PR creation). Primary causes:
- Test failures discovered after PR creation
- Gate config endpoint redesign (YAML persistence approach changed)
- Contract revisions after initial review

---

## 8. Comparison with F18-22 Chain

| Metric | F18-22 | F23-26 | Delta |
|--------|--------|--------|-------|
| Features | 5 | 4 | -1 |
| Merged PRs | 26 | 20 | -6 |
| Superseded PRs | 2 | 5 | +3 |
| Total PRs (all states) | 28 | 25 | -3 |
| Commits (feature-tagged) | 33 | 24 | -9 |
| Gate provider | Codex (primary) | Gemini (exclusive) | Switched |
| Codex gates | 19 | 0 | -19 |
| Gemini gates | 7 | 19 | +12 |
| Blocking findings | Multiple | 0 | Significant drop |
| Chain duration | Multi-day | ~18 hours | Faster |
| Infrastructure fixes | Few | 11 documented | Significantly more |

**Key observations**:
- F23-26 was faster per-feature but had a higher rework rate (5 superseded vs 2).
- Zero blocking gate findings is a quality signal concern, not a quality improvement. The Gemini gate is less thorough than Codex.
- Infrastructure overhead was substantially higher in F23-26 due to worktree contamination, stale processes, and dispatcher issues.
- Despite 4 vs 5 features, the operational complexity was comparable due to infrastructure firefighting.

---

## 9. Lessons Learned and Recommended Tweaks

### Gate Quality
1. **Gemini gates need structured rubric enforcement.** Zero findings across 19 reviews indicates the gate is not probing deeply enough. Recommendation: add explicit checklist prompts (security, error handling, test coverage) to force structured output.
2. **Dual-gate fallback.** When Codex becomes available again, run both providers and merge findings. Single-provider gates create blind spots.
3. **Gate coverage tracking.** 30% of PRs lacked gate evidence on disk. Add a pre-merge check that blocks merge unless a gate result file exists.

### Infrastructure Resilience
4. **vnx_paths.py must be worktree-safe.** The cross-contamination bug was the most damaging issue. Add integration tests that verify path resolution in multi-worktree scenarios.
5. **Pane ID refresh on tmux restart.** Make `panes.json` refresh automatic on dispatcher startup rather than requiring manual rediscovery.
6. **Process cleanup on session start.** Add a startup sweep that kills stale supervisor/hook processes from prior sessions.

### Chain Operations
7. **Reduce superseded PRs.** 5 superseded PRs means the initial dispatch was insufficient or review happened too late. Run local tests before PR creation to catch obvious failures.
8. **Module split proactively.** The `serve_dashboard.py` split should have happened before F23, not during. Apply module splits when files cross 500 lines.
9. **Open-item persistence needs verification.** The claimed 754 items don't appear in `open_items.ndjson`. Verify the ledger mechanism is writing to the correct path before the next chain.

### Process
10. **Gate provider switch should trigger a chain pause.** Switching from Codex to Gemini mid-chain without a formal checkpoint reduced review quality for the remainder. Future provider switches should require an explicit operator GO/NO-GO.
