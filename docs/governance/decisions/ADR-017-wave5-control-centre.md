# ADR-017: Wave 5 — VNX Control Centre

**Status**: Accepted
**Date**: 2026-05-16
**Deciders**: Vincent van Deth (operator)
**Related ADRs**: ADR-005 (NDJSON ledger), ADR-007 (multi-tenant project_id), ADR-010 (subprocess routing), ADR-013 (workers=N), ADR-016 (unified event shape)

## Context

VNX has shipped phase 6 (per-project paths + project_id propagation + federation aggregator read-only) and Wave 4.6 (provider dispatch generalization + unified event shape). The next product-shape question: how do operators supervise N concurrent VNX projects from one place?

Three options exist:

- **Option A**: Per-project interactive Claude Code session. Operator opens N tabs/terminals. No central state. Today's reality.
- **Option B**: Web dashboard reading per-project state via REST API. Heavy infrastructure (web server + frontend + auth).
- **Option C**: Control Centre — one interactive Claude Code session with skill `@control-centre` that supervises N headless per-project T0's via central state aggregator. CLI-shell UX, NDJSON-based audit trail, no web frontend.

## Decision

Wave 5 implements **Option C — Control Centre**.

The Control Centre is one interactive Claude Code session running `@control-centre` skill. It:
1. Reads federation-aggregator state (read-only, exists since PR #412)
2. Spawns or contacts N headless per-project T0's via subprocess_dispatch
3. Routes operator commands (`status`, `dispatch <project> <task>`, `heartbeat`, `incident <project>`) to the right T0
4. Streams per-project NDJSON ledger updates into a unified view
5. Provides multi-tenant lease isolation in runtime_coordination.db (schema v10)

## Consequences

**Positive:**
- One operator-substrate for N projects (no tab-juggling)
- Demonstrates VNX moats stacked: M2 (audit) + M3 (multi-tenant) + M4 (hybrid dispatch) in one product
- Stays within ADR-003/005/010/013 invariants (subprocess only, NDJSON audit, project_id isolated)
- No web infrastructure — CLI-shell UX matches operator workflow

**Negative:**
- Multi-tenant lease isolation requires schema v10 migration
- Per-project T0 lifecycle management adds new failure modes
- Control Centre itself is a new SPOF (mitigated by stateless design — restart loses nothing)

**Risk mitigation:**
- 12-item risk register in `claudedocs/wave5-control-centre-architecture.md` §8
- Multi-tenant schema migration with rollback (PR-5.3)
- Falsifiable tests per multi-tenancy risk

## Rejected alternatives

- **Web dashboard**: too much infrastructure for current scale; revisit when N projects ≥ 20
- **Per-project sessions only**: doesn't scale operator attention; misses cross-project intelligence
- **Tmux-based supervisor**: not provider-agnostic; locks Control Centre to local tmux

## Implementation roadmap

Wave 5 lands in 8 PRs:

- **PR-5.0** (this ADR)
- **PR-5.1**: Multi-project state aggregator (write-pad extension on Phase 6 PR #412)
- **PR-5.2**: Per-project T0 spawning + lifecycle (start/stop/heartbeat)
- **PR-5.3**: Multi-tenant lease isolation in runtime_coordination.db (schema v10)
- **PR-5.4**: Cross-project intelligence aggregation (per-project facet + global facet)
- **PR-5.5**: Control Centre CLI shell skill (@control-centre)
- **PR-5.6**: Hybrid dispatch routing from Control Centre to project-T0
- **PR-5.7**: Operator demo + docs + first multi-project run

Effort: ~25 werkdagen total, ~6 weken with 3-track parallel.

Hard prereq: ADR-016 (Wave 4.6 PR-4.6.6 unified events) — merged.

## See also

- `claudedocs/wave5-control-centre-architecture.md` — Wave 5 design doc
- ADR-005, ADR-007, ADR-010, ADR-013, ADR-016
- ADR-015 — Wave 7 LiteLLM Path B (Control Centre dispatches to all 5 providers including LiteLLM lanes)
- ADR-018 — Elastic worker pool (Wave 6 per-project pools; PR-6.8 extends Control Centre
  with multi-project pool table and `pool_state_unified` aggregator view)
