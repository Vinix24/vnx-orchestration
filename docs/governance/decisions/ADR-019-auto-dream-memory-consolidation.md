# ADR-019 — Auto-Dream Memory Consolidation

**Status:** Accepted
**Date:** 2026-05-29
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Dormant self-learning loop (governance-receipt-gap memory). Boris Cherny "auto-dream" pattern. Per CC-COMMUNITY-SYNTHESIS-2026-05-29.md §A-14.

## Context

VNX has a `quality_intelligence.db` that accumulates `success_patterns`, `antipatterns`, and `intelligence_injections` over dispatch cycles. The self-learning loop (originally tracked in the governance-receipt-gap memory item) was never wired to a consolidation step: patterns accumulate without deduplication, stale entries are never retired, and the signal-to-noise ratio degrades over time.

Boris Cherny published the "auto-dream" pattern (CC-COMMUNITY-RESEARCH-OPUS-2026-05-29.md §A4) as a REM-sleep analogy for memory consolidation in agent systems: during an idle cycle, the agent reviews its accumulated memory, merges overlapping patterns, drops stale entries, and flags high-impact novelties for operator review. The analogy maps directly to the VNX intelligence pipeline.

Three design constraints bind this feature:

1. **ADR-007 tenant isolation** — all new tables carry `project_id` as a composite PK/UNIQUE component; consolidation runs are scoped per project by default.
2. **ADR-005 NDJSON-first** — every cycle emits a `dream_cycle_completed` event to the ledger before any DB write.
3. **Operator review gate** — the first 30 days require T0 approval before any DB mutation is applied. Auto-apply is allowed only for low-risk, idempotent operations (exact dedup, archive of entries untouched for >30 days) after the burn-in period.

## Decision

Implement nightly auto-dream consolidation via kimi K2.6 cheap-lane provider.

**Binding rules:**

1. Per-project consolidation by default (ADR-007 tenant isolation). Cross-project aggregation requires an explicit `--cross-project` flag — never implicit.
2. Optional cross-project aggregation via explicit `--cross-project` flag only.
3. T0 review-gate required for all cycles during first 30 days after enable.
4. Auto-apply post-30d ONLY for low-risk ops: exact dedup (same title + description hash), archive of entries with `first_seen` or `injected_at` older than 30 days and no recorded usage.
5. T0 review always required for: pattern merges (non-exact), new pattern additions, antipattern reclassification, any `flagged` items from the kimi consolidation.
6. Every consolidation cycle emits NDJSON event `dream_cycle_completed` BEFORE any DB write (ADR-005). The ledger event is the source of truth; the `dream_cycles` DB row is a projection.
7. Cycle output is written to `.vnx-data/state/dream/{cycle-id}-pending-review.json` with `(cycle_id, project_id)` as composite natural key (ADR-007).
8. kimi K2.6 is the sole consolidation provider — subscription-flat billing, no cost-per-token exposure, existing kimi_wrapper infrastructure.

## Reasoning

**Why kimi over gemini or sonnet?**
Consolidation prompts are large (up to 50K characters of pattern JSON) but the reasoning task is low-complexity: classify each item as KEEP / MERGE / DROP / ARCHIVE / FLAG. kimi K2.6 handles 128K context at subscription-flat cost, making it the right cheap-lane for a nightly batch job. Gemini is via OpenRouter (cost-per-token); sonnet runs on Anthropic OAuth (T0 allocation, not appropriate for background batch). kimi_wrapper.py already provides the subprocess infrastructure.

**Why nightly?**
The intelligence accumulation rate is 5–20 new patterns per day across active projects. A nightly cycle keeps the DB fresh without over-indexing on ephemeral patterns from a single dispatch.

**Why T0 review-gate first 30 days?**
The operator's governance philosophy (glass-box, human-in-the-loop) requires a burn-in period to verify that the consolidation LLM's decisions align with operator intent. The 30-day window provides ~30 consolidation cycles as evidence before trusting auto-apply. This mirrors the ADR-006 (staging→promote human gate) discipline.

**Why NDJSON-first?**
If the process crashes after DB write but before completing the pending-review.json, the operator has no audit trail. Emitting the NDJSON event first (per ADR-005) ensures the ledger always has a record of what the cycle produced, even if downstream writes fail.

## Consequences

### Accepted

- `dream_cycles` and `dream_pattern_archives` tables added to `quality_intelligence.db` via migration `0025_dream_consolidation.sql`.
- New directory `.vnx-data/state/dream/` for pending-review JSON files (not committed, runtime state).
- New event type `dream_cycle_completed` (and `dream_cycle_started`, `dream_cycle_failed`) in `.vnx-data/events/dream/<date>.ndjson`.
- kimi K2.6 is the standard consolidation provider; switching provider requires a new ADR or amendment.
- T0 receives a pending-review.json path in the cycle result; T0 is responsible for operator notification during first 30 days.
- Pattern archives in `dream_pattern_archives` are soft-deletes: the original row in `success_patterns` / `antipatterns` is not deleted by the auto-dream process. Deletion is a separate operator action after reviewing the archive record.

### Rejected

- **Anthropic SDK for the consolidation call** — blocked by ADR-003. kimi CLI subprocess only.
- **Immediate auto-apply without burn-in** — rejected. Operator review gate is non-negotiable for first 30 days.
- **Cross-project consolidation by default** — rejected. ADR-007 tenant isolation; cross-project is an explicit opt-in.
- **SQLite-first cycle tracking** — rejected. ADR-005 requires NDJSON ledger first.

## Implementation note

Scripts: `scripts/dream/consolidator.py` + (PR-2) `scripts/dream/cli.py`

Schema: `schemas/migrations/0025_dream_consolidation.sql` — adds `dream_cycles` and `dream_pattern_archives` to `quality_intelligence.db`.

Scheduler: LaunchAgent plist (macOS) / cron (Linux), shipped in PR-2.

T0 review-gate workflow integration (dispatch trigger on pending-review.json), operator HTML/markdown summary, and burn-in metrics are PR-2 scope.

## Cross-references

- ADR-003 — OAuth-only Claude routing (kimi CLI subprocess, no SDK)
- ADR-005 — NDJSON audit ledger as primary surface (NDJSON-first invariant)
- ADR-006 — Staging→promote human gate (burn-in review discipline)
- ADR-007 — Multi-tenant project_id stamping (composite keys, per-project default)
- ADR-015 — Wave 7 cheap-lane providers (kimi K2.6 as approved cheap-lane)
- `scripts/lib/kimi_wrapper.py` — kimi subprocess infrastructure
- `claudedocs/CC-COMMUNITY-SYNTHESIS-2026-05-29.md` §A-14 — architecture choice record
- `claudedocs/CC-COMMUNITY-RESEARCH-OPUS-2026-05-29.md` §A4 — Boris Cherny REM-sleep analogy
- Memory: `governance-receipt-gap` — original dormant self-learning loop record
