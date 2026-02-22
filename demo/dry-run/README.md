# VNX Dry-Run Replay

Non-LLM demonstration of the VNX Glass Box Governance pipeline.

## What This Is

A replay of a real demo session (LeadFlow AI-Powered Lead Scoring) that demonstrates the complete governance lifecycle without requiring any LLM API calls. All data comes from actual governance artifacts generated during a live demo.

## What It Shows

1. **PR Queue** — 6 PRs across 3 parallel tracks with dependency chains
2. **Dispatch Promotion** — Human-approved staging → queue transitions
3. **Multi-Provider Execution** — Claude Code + Codex CLI on different terminals
4. **Receipt Capture** — Structured NDJSON receipts with provenance (git SHA, model, provider)
5. **Quality Advisory** — Automated verdicts: approve / approve_with_followup / hold
6. **Open Items** — Blocker tracking with deduplication and evidence-based closure
7. **Recursive Governance** — Follow-up dispatches governed by the same pipeline

## Quick Start

```bash
# Normal speed (2s between steps)
bash replay.sh

# Fast mode (0.5s between steps)
bash replay.sh --fast
```

## Evidence Files

All data in `evidence/` was captured from a live demo session on 2026-02-21:

| File | Records | Description |
|------|---------|-------------|
| `receipts.ndjson` | 16 | Full receipt ledger (8 started + 8 complete) |
| `dispatch_audit.jsonl` | 6 | Promote events with actor and timestamp |
| `pr_queue_state.json` | 6 PRs | Final queue state (5 completed, 1 queued) |
| `progress_state.yaml` | 3 tracks | Track A/B/C history with dispatch IDs |
| `open_items_digest.json` | 60 items | 34 done, 3 blockers, 6 warnings, 17 info |
| `last_quality_summary.json` | 1 | Latest quality gate decision |
| `dispatches/*.md` | 8 | Original dispatch files sent to terminals |
| `reports/*.md` | 7 | Unified reports (deliverables from agents) |

## Session Timeline

Execution times from receipt ledger (all times CET, 2026-02-21):

```
16:03  T2 starts PR-1 (Claude Code, Sonnet)
16:08  T2 completes PR-1 → APPROVE
16:10  T1 starts PR-2 (Codex CLI)              ← parallel
16:11  T2 starts PR-5 (Claude Code, Sonnet)    ← parallel
16:17  T2 completes PR-5 → APPROVE
16:18  T1 completes PR-2 → APPROVE_WITH_FOLLOWUP
16:22  T1 starts PR-2 iteration 2 (follow-up)
16:26  T1 completes PR-2 iteration 2 → APPROVE
16:30  T1 starts PR-4 (Codex CLI)              ← parallel
16:31  T3 starts PR-3 (Claude Opus)            ← parallel
16:33  T1 completes PR-4 → APPROVE
16:35  T3 completes PR-3 → APPROVE
16:42  T3 starts PR-6 (Claude Opus, planning gate)
16:45  T3 completes PR-6 architecture review → HOLD (3 blockers)
16:48  T3 starts PR-6 follow-up dispatch
16:56  T3 completes PR-6 follow-up → APPROVE
```

Total duration: ~53 minutes for 6 PRs with quality gates.
