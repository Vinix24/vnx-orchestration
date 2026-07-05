# Archived Documentation

This directory contains superseded public documents that are kept only for traceability.

Rules:
- Active user guidance should not point here by default.
- Historical evidence and retired specs may stay here when they still add context.
- Private strategy, business plans, and maintainer-only notes belong in the external BUSINESS workspace, not in this repository.

## Wave 1-3 era (archived 2026-05-17)

Moved during the Wave 5/6/7/8 docs overhaul. These docs date from 2026-03-29 to 2026-03-31 and describe pre-v0.10.0 certification contracts that have been superseded by the ADR-based governance model (ADR-003 through ADR-018).

- `core/22_FPB_CERTIFICATION_MATRIX.md` — FP-B runtime recovery certification matrix
- `core/30_FPC_EXECUTION_CONTRACTS.md` — FP-C task classes, execution targets, routing invariants
- `core/31_FPC_INTELLIGENCE_CONTRACT.md` — FP-C bounded injection and recommendation classes
- `core/32_FPC_CERTIFICATION_MATRIX.md` — FP-C execution modes certification matrix
- `core/50_DOUBLE_FEATURE_TRIAL_CONTRACT.md` — double-feature trial pass/fail rules

## 1.0 sprint (archived 2026-05-29)

Moved during the 1.0 doc-update. Superseded by today's merged state.

- `HANDOFF-2026-05-20.md` — session handoff written 2026-05-20; describes Wave 8 in flight on PR #602 and pre-cutover hardening steps. All those PRs are long since merged. Superseded by current README + ROADMAP.md state.
- `plans/wave2a-robust-pipeline-blueprint-2026-05-25.md` — Wave 2a central cutover architecture blueprint (2026-05-25). All 8 planned PRs (#619–#633 + others) merged and validated. Superseded by shipped code.

## 1.0 release sweep (archived 2026-07-03)

Moved during the post-release docs sweep (1.0.0 published to PyPI 2026-07-02).

- `operations/wave1-rollback.md` — rollback procedures for the Wave 1 shadow-mode central-DB cutover (2026-05-09, PRs #450–#454). The cutover completed long ago; the `VNX_USE_CENTRAL_DB` flag semantics live in the active operations docs. Kept for traceability.

## Governance docs sweep (archived 2026-07-05)

Moved during the docs-governance sweep for the D1-D5 signed-attestation pipeline (PRs #1004-#1012).

- `governance/week_2026_W15.md` — a single weekly governance report from 2026-04-10, every field marked "Geen data" (no data). No other week reports were ever produced; the practice never continued past this one file. Kept only because it is referenced from old history — do not use as a template for a reporting cadence that does not exist.
