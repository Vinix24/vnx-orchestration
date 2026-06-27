# ADR-026 — Per-project state store, canonical; cross-project learning via a governance-class federation

**Status:** Accepted
**Date:** 2026-06-27
**Decided by:** Operator (Vincent van Deth), informed by a 4-model provider panel (DeepSeek-V4, Kimi-K2.7, Codex-gpt5.5, GLM-5.2 via the provider lanes — 4/4 unanimous)
**Resolves:** The store-topology question left open by ADR-007. ADR-007 fixed the project_id-stamping RULE; this ADR fixes the store FORM. Amends the "one shared DB" shape implied by `claudedocs/_archive/2026-05-20-pre-centralisatie/2026-04-30-single-vnx-migration-plan.md` (Model B).

## Context

VNX state lives in SQLite (`quality_intelligence.db`, `runtime_coordination.db`). Two central-store models exist on disk and the system has drifted between them:

- **Shared (Model B):** one `~/.vnx-data/state/` with all projects' rows separated by a `project_id` column (ADR-007). Per ADR-007's Context, the move to a central project_id-namespaced store was driven by the fact that, under per-project isolation, "cross-project learning was structurally impossible." On disk: ~10 GB, but **frozen since 2026-06-20** — nothing live writes to it.
- **Per-project (Model A′):** each project resolves `~/.vnx-data/<project_id>/state/` (`scripts/lib/vnx_paths.py::resolve_central_data_dir`). This is the LIVE runtime (vnx-dev 6.9 GB written 2026-06-27; mission-control 915 MB; seocrawler-v2 17 GB). A later (Phase 6) drift that left the shared store orphaned.

The operator asked whether all projects should run from one shared store to maximize cross-project learning. A provider panel investigated, grounded in the repo. The findings:

1. **Per-project is already the live runtime and isolation is fail-safe.** Physical file separation means a missed `project_id` filter cannot bleed across projects. The shared model's safety depends on perfect filter discipline that REPEATEDLY failed: the multi-tenant retrofit took nine codex review rounds (ADR-007 Context); tenant-stamping contamination was the last 1.0 release blocker; the QI-write-tier still carries latent `DEFAULT 'vnx-dev'` exceptions (a writer silently degrading to `vnx-dev` re-contaminates a non-vnx-dev store on every write — the 2026-06-24 amendment).
2. **Cross-project learning is real but NARROW — the governance/process class only.** The selector already classifies every pattern as `governance` / `process` / `code` (`scripts/lib/intelligence_sources/_common.py::classify_pattern_category`), penalizes governance in code-dispatches, and caps governance per injection — the universal/domain split is already encoded. Universal governance patterns (gate behavior, report/receipt enforcement, no-anthropic-sdk, PR structure) transfer across projects; domain/code patterns are project-specific noise (~91% of seocrawler antipatterns). Cross-project CONFIDENCE never transfers — `confidence_reconcile.py` joins within one db_path — only the pattern catalogue does.
3. **The federation layer to share the governance class correctly already exists and is dormant.** `schemas/migrations/0018_cross_project_intelligence.sql` defines `global_intelligence.db` (`global_patterns` privacy-safe family-keys + `cross_project_recommendations`), and `scripts/lib/intelligence_aggregator.py` (373 lines) implements it. It is not wired into the dispatch path (`global_intelligence.db` is never populated; `_get_central_qi_conn` is NOT federation — it reads the current project only).
4. **Skills are orthogonal to the store** (git markdown copied to `<project>/.claude/skills/`), but their propagation is currently broken: `bootstrap-skills` is copy-once and there is no `vnx skills sync`, so a skill refined in one project does not propagate today. The fix is a sync command, never the SQLite DB.

## Decision

**Per-project state stores (`~/.vnx-data/<project_id>/`) are the canonical model.** Cross-project learning is delivered NOT by co-mingling raw rows in a shared DB, but by the normalized `global_intelligence` federation, scoped to the universal **governance/process** pattern class only, opt-in and measured.

Concrete rules:

- The ADR-007 `project_id`-stamping rule remains binding for every multi-tenant table. Only the store FORM changes (per-project dirs, not one shared DB).
- The orphaned shared `~/.vnx-data/state/` is retired after reconciliation (mine its universal catalogue into `global_intelligence.db`; confirm no project's per-project store is missing rows that live only in the shared store).
- Cross-project intelligence flows ONLY through `global_intelligence` (normalized family-keys + per-project counts + `consumed_at` audit), never through shared raw rows. It is gated behind `VNX_USE_FEDERATION` (default OFF) and turned on only after an A/B lift measurement; if lift ≈ 0 the system stays pure per-project (the recommendation degrades gracefully).
- A new guard to ADD when the aggregator is revived (not present in `0018` today): a `cross_project_recommendation` should require ≥2 projects with aligned `scope_tags` so a domain pattern cannot masquerade as universal (relocalized contamination guard).
- Skill propagation is handled by the skills channel (a `vnx skills sync`, to be added), not the data store.

## Reasoning

1. **Safety that is a filesystem guarantee beats safety that is a discipline.** Per-project isolation cannot fail by negligence; the shared model failed by negligence three times (the codex rounds, the 1.0 blocker, the fail-open re-contamination). For a governance-first runtime, fail-safe wins.
2. **Share only what transfers.** The value of centralizing was cross-project learning, but only the governance class transfers; raw-sharing imports mostly domain noise plus the contamination tax. The normalized federation shares exactly the transferable class.
3. **Reuse, don't rebuild.** The federation engine already exists (0018 + aggregator); the remaining work is one read-source + one refresh job behind a flag — not a new architecture.
4. **Measure before defaulting on.** Federation value is unmeasured (`~/.vnx-aggregator/` never existed). Default OFF + A/B lift means the system never pays for federation it doesn't benefit from.

## Consequences

### Accepted
- `resolve_central_data_dir` is renamed `resolve_project_data_dir` (the name lied; it is per-project).
- sales-copilot onboards per-project (`~/.vnx-data/sales-copilot/`) with explicit project_id stamping (it has none; never trust the default); its 41 stale rows in the shared store import into its own store only.
- ~10 GB reclaimed by retiring the shared store (`.pre-retirement` + 30-day retention; drop the four 2.7 GB presnaps).
- A dormant aggregator is revived behind `VNX_USE_FEDERATION` (default OFF).
- A `vnx skills sync` is added to fix the copy-once skill-propagation gap.

### Rejected
- **Raw shared `~/.vnx-data/state`.** Discipline-fragile (every query must filter project_id; historically failed), contamination-prone, one giant FTS DB with cross-project write-lock contention, imports domain noise.
- **Pure per-project with no federation.** Throws away the universal governance learning ADR-007 calls "the whole value proposition." Federation restores it without the write-path contamination tax.
- **Skill sharing via the DB.** Wrong vehicle; skills are code, shared through git + a sync command.

## Implementation note

Migration scoping + the precise reclaim plan are in `claudedocs/2026-06-27-store-model-research-SYNTHESIS.md` and `claudedocs/2026-06-27-sales-copilot-vnx-cutover-PLAN.md`. The panel reports are at `~/.vnx-data/vnx-dev/unified_reports/20260627-storemodel-{deepseek,kimi,codex,glm}.md`.

## See also

- ADR-007 — Multi-tenant project_id stamping (the rule this ADR preserves; this ADR amends only the store form).
- `schemas/migrations/0018_cross_project_intelligence.sql` — the dormant federation schema this ADR revives.
- `claudedocs/_archive/2026-05-20-pre-centralisatie/2026-04-30-single-vnx-migration-plan.md` — the original Model B plan this ADR supersedes on store form.
