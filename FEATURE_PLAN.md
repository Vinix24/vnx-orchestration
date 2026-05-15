<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->

# VNX Feature Plan
**Last updated**: 2026-05-15T14:05:51.809698+00:00

## Recently Merged
_Last 14 days — sourced from git merge commits._

**Other**
- #503 — fix(rp): bootstrap audit ordering + test infra hardening (close OI-1450/1451/1452) (#503) (2026-05-15)
- #502 — fix(dispatcher): log stderr, script_dir leak, receipt processor bootstrap-mode (#502) (2026-05-15)
- #500 — chore(hardening): narrow silent-except across replay_harness/cleanup_worker_exit/conversation_analyzer (9 findings, OI-1437) (#500) (2026-05-15)
- #499 — chore(hardening): narrow silent-except in session_resolver.py (4 findings, OI-1437) (#499) (2026-05-14)
- #498 — chore(hardening): narrow silent-except in api_operator.py (5 findings, OI-1437) (#498) (2026-05-14)
- #497 — chore(hardening): narrow silent-except in append_receipt payload.py (5 findings, OI-1437) (#497) (2026-05-14)
- #496 — chore(hardening): narrow silent-except in dispatch_register.py (5 findings, OI-1437) (#496) (2026-05-14)
- #495 — chore(hardening): narrow silent-except in api_intelligence.py (6 findings, OI-1437) (#495) (2026-05-14)
- #494 — chore(hardening): narrow silent-except in learning_loop.py (5 findings, OI-1437) (#494) (2026-05-14)
- #493 — chore(hardening): narrow silent-except in gather_intelligence.py (6 findings, OI-1437) (#493) (2026-05-14)
- #492 — chore(hardening): narrow silent-except in intelligence_selector.py (10 findings, OI-1437) (#492) (2026-05-14)
- #491 — chore(hardening): narrow silent-except in build_t0_state.py (13 findings, OI-1437) (#491) (2026-05-14)
- #489 — chore(contrib): add CONTRIBUTING.md + ci lint gate (atomic-write + silent-except) (#489) (2026-05-14)
- #487 — chore(docs): CHANGELOG — OI-1370 systemic locking refactor series (#482-#486) (#487) (2026-05-13)
- #486 — fix(oi-1370): PR N4 — final migration; close OI-1370 race comprehensively (#486) (2026-05-13)
- #485 — feat(oi-1370): PR N3 — migrate compact_state.compact_receipts + backfill_headless_receipts to state_writer (#485) (2026-05-13)
- #484 — feat(oi-1370): PR N2 — migrate gate_register_emit + cleanup_worker_exit to state_writer (#484) (2026-05-13)
- #483 — feat(oi-1370): PR N1 — state_writer.append_locked helper (no behavior change) (#483) (2026-05-13)
- #482 — docs(architect): OI-1370 systemic locking refactor plan (#482) (2026-05-13)
- #481 — chore(docs): post-rc1 sprint refresh — CHANGELOG + README + FEATURE_PLAN regenerator (12 PRs) (#481) (2026-05-13)
- #480 — feat(gates): validate + document Vertex routing path for gemini quota workaround (#480) (2026-05-13)
- #467 — refactor(oi-1294): split compact_open_items_digest below 70-line threshold (#467) (2026-05-13)
- #464 — chore(t0): canonical subprocess_dispatch.py routing in T0 template + skill (#464) (2026-05-13)
- #463 — chore(start): default VNX_QUEUE_POPUP_ENABLED=0 in init-time preset templates (#463) (2026-05-13)
- #465 — fix(security): OI-1369 — reject path traversal in project_id (^[a-z][a-z0-9-]{1,31}$) (#465) (2026-05-13)
- #449 — docs(governance): ADR-011 amendment v2 — split subagent pilot into 2 sequential gates (#449) (2026-05-09)
- #448 — docs(release): cut v1.0.0-rc1 — architectural stabilization milestone (#448) (2026-05-09)
- #447 — docs(governance): ADR-011 amendment — resolve Tentative section to Conditional/Pilot (#447) (2026-05-09)
- #446 — fix(migration): rewrite 0016 schema-first per ADR-009 (resolves OI-1375/1376/1377) (#446) (2026-05-09)
- #445 — docs(governance): add ADR-013 (workers=N) + ADR-014 (autonomous chain dispatch) (#445) (2026-05-09)
- #442 — chore(repo-hygiene): OI-1373 Tier 4 — move 18 FEATURE_PLAN files to private claudedocs/ (#442) (2026-05-09)
- #441 — chore(repo-hygiene): OI-1373 Tier 2 — move strategy files to private claudedocs/ (#441) (2026-05-09)
- #440 — chore(repo-hygiene): OI-1373 Tier 1 — move business agent drafts to private claudedocs/ (#440) (2026-05-09)
- #439 — ci(governance): enforce ADR-003 — block Anthropic SDK imports (#439) (2026-05-09)
- #432 — feat(migration): one-shot data import script (Phase 6 P4 — DRAFT, operator-review-required) (#432) (2026-05-09)
- #438 — feat(skills): add database-engineer + intelligence-engineer specialists (#438) (2026-05-09)
- #437 — ci(profile-a): exclude migration scripts from legacy path gate (#437) (2026-05-07)
- #436 — docs(skill): add VNX CI workflow conclusion check before merge (t0-orchestrator) (#436) (2026-05-07)
- #435 — fix(build_current_state): remove legacy path literal from line 263 hint (#435) (2026-05-07)
- #434 — fix(dashboard): clean up agent stream lifecycle (#434) (2026-05-07)
- #433 — chore(gemini): switch default reviewer model to gemini-2.5-pro for deeper reviews (#433) (2026-05-07)
- #431 — feat(envelopes): per-project paths + four-tuple envelope (Phase 6 P3 v2) (#431) (2026-05-07)
- #430 — fix(intelligence): wire stamp_source_dispatch_ids into all injection callers (closes OI-1341) (#430) (2026-05-07)
- #429 — docs(skill): add Codex Defense Checklist to backend-developer skill (#429) (2026-05-07)
- #428 — fix(intelligence): re-evaluate scope match after canonical remap (closes OI-1340) (#428) (2026-05-07)
- #427 — fix(closure_verifier): require pr_id on claude_github_optional payloads (closes OI-1339) (#427) (2026-05-07)
- #426 — fix(project-id): unify _get_project_id fallback via project_scope.current_project_id (closes OI-1342) (#426) (2026-05-07)
- #425 — fix(closure_verifier): extend report_path enforcement to codex_gate handler (closes OI-1338) (#425) (2026-05-07)
- #422 — feat(t0-state): strategic_state surface from strategy/ (Phase 2 W-state-5) (#422) (2026-05-06)
- #417 — feat(strategy): typed current_state.md projector v2 (Phase 2 W-state-3) (#417) (2026-05-06)
- #416 — feat(identity): four-tuple identity layer + per-project registry (Phase 6 P2) (#416) (2026-05-06)
- #415 — feat(strategy): prd_index + adr_index builders (Phase 2 W-state-4) (#415) (2026-05-06)
- #413 — feat(strategy): decisions.ndjson append-only log + writer (Phase 2 W-state-2) (#413) (2026-05-06)
- #412 — feat(aggregator): read-only federation aggregator (Phase 6 P1) (#412) (2026-05-06)
- #410 — feat(strategy): roadmap.yaml schema + reader/writer Python module (Phase 2 W-state-1) (#410) (2026-05-06)
- #408 — fix(dispatcher): drain + dead_letter receipt classification (Phase 1.5 PR-4) (#408) (2026-05-06)
- #409 — fix(dispatcher): propagate role-alias mapping to gather_intelligence (Phase 1.5 PR-5) (#409) (2026-05-06)
- #407 — fix(dashboard): guard null in /operator/reports toLowerCase call (#407) (2026-05-06)
- #406 — fix(dispatch_project_guard): canonicalize non-existent paths + Project-ID stamping (Phase 1.5 PR-3) (#406) (2026-05-06)
- #405 — fix(intelligence): success_patterns multi-tenant correctness (Phase 1.5 PR-2) (#405) (2026-05-06)
- #404 — fix(closure_verifier): evidence-contract restoration (Phase 1.5 PR-1) (#404) (2026-05-06)
- #396 — fix(append_receipt): remove dead duplicate _maybe_reroute_ghost_receipt (UR-001) (#396) (2026-05-06)
- #403 — feat(cli): vnx status dashboard subcommand (W-UX-3) (#403) (2026-05-06)
- #402 — chore: ADRs (No-Redis + F43 packaging) + threshold-OI cleanup script (cherry-pick of #395) (#402) (2026-05-06)
- #400 — fix(t0-state): GC retention sweep for t0_detail JSON snapshots (W-UX-4) (#400) (2026-05-06)
- #401 — feat(strategy): current_state.md auto-projector + retire vestigial state (W-UX-2) (#401) (2026-05-06)
- #399 — docs(kickoff): reflect PR #398 MERGED + add Phase 1 PR #395+#396 retry guidance (#399) (2026-05-06)
- #398 — docs(strategy): persist strategic state + 17 phase FEATURE_PLAN.md files (#398) (2026-05-06)

**W7**
- #424 — feat(observability): tier labeling + governance gating (Phase 3 W7-G feature-end) (#424) (2026-05-06)
- #421 — feat(gemini): adapter streaming refactor gated by VNX_GEMINI_STREAM (Phase 3 W7-D) (#421) (2026-05-06)
- #420 — feat(litellm): adapter proof-of-concept via W7-B mixin (Phase 3 W7-E) (#420) (2026-05-06)
- #419 — feat(codex): adapter migration to streaming via W7-B mixin (Phase 3 W7-C) (#419) (2026-05-06)
- #418 — feat(ollama): adapter streaming refactor via W7-B mixin (Phase 3 W7-F) (#418) (2026-05-06)
- #414 — feat(streaming): _streaming_drainer mixin + event_store dispatch_id fix (Phase 3 W7-B) (#414) (2026-05-06)
- #411 — feat(events): CanonicalEvent schema + EventStore observability_tier (Phase 3 W7-A) (#411) (2026-05-06)

**WAVE 0**
- #443 — chore(repo-hygiene): Wave 0 — OI-1373 Tier 3 + 9 stale docs archive (combined) (#443) (2026-05-09)

**WAVE 1**
- #457 — docs: refresh README + CHANGELOG for post-rc1 work (Wave 1 + Wave 5 P0/P1) (#457) (2026-05-10)

**WAVE1**
- #454 — feat(wave1): PR-W1.5 — Dashboard shadow wiring + canary divergence test pack + rollback docs (#454) (2026-05-10)
- #453 — feat(wave1): PR-W1.4 — IntelligenceSelector + DispatchRegister shadow wiring (5 read sites) (#453) (2026-05-09)
- #452 — feat(wave1): PR-W1.3 — T0 state-builder shadow wiring (4 read sites) (#452) (2026-05-09)
- #451 — feat(wave1): PR-W1.2 — shadow_logger NDJSON writer + report CLI + rotation (#451) (2026-05-09)
- #450 — feat(wave1): PR-W1.1 — shadow_verifier independent comparator with 6 hard metrics (#450) (2026-05-09)

**WAVE2**
- #478 — feat(wave2): Phase 1a redo — migrate function_size_gate to vnx_core + shim layer (#478) (2026-05-13)
- #469 — feat(wave2): Phase 0a — vnx-orchestration package skeleton + build validation (#469) (2026-05-13)

**WAVE4**
- #468 — feat(wave4): OTel export foundation — dispatch completion metrics + span emission (opt-in via OTEL_EXPORTER_OTLP_ENDPOINT) (#468) (2026-05-13)

**WAVE4.5**
- #479 — feat(wave4.5): PR-2b redo — gate reviewer prompts use gh pr diff + fail-loud on errors (#479) (2026-05-13)
- #477 — feat(wave4.5): PR-3 redo — guard build_intelligence_context against empty dispatch_id (audit-safe) (#477) (2026-05-13)
- #472 — feat(wave4.5): PR-2 — codex/gemini adapters use PromptAssembler + tri-file activation (#472) (2026-05-13)
- #471 — feat(wave4.5): PromptAssembler provider-agnostic methods (codex, gemini, litellm) (#471) (2026-05-13)

**WAVE4.6**
- #490 — feat(wave4.6): PR-4.6.2 — extract claude_spawn from subprocess_dispatch (byte-identical) (#490) (2026-05-14)
- #488 — feat(wave4.6): PR-4.6.1 — provider_dispatch.py entry-point (additive shim) (#488) (2026-05-14)

**WAVE5**
- #462 — feat(wave5): CFX-W5-2 — plumbing gaps (pr_id key + headless daemon entry points) (#462) (2026-05-13)
- #461 — feat(wave5): PR-W5.5 — production plumbing for P0-P4 context-bundle classes (#461) (2026-05-10)
- #460 — feat(wave5): PR-W5.4 — schema introspection injection (DDL grounding for DB workers) (#460) (2026-05-10)
- #459 — feat(wave5): PR-W5.3 — operator memory injection (curated wisdom into worker context) (#459) (2026-05-10)
- #458 — feat(wave5): PR-W5.2 — code anchor injection (file:line current-state grounding) (#458) (2026-05-10)
- #456 — feat(wave5): PR-W5.1 — ADR injection by file-touch (governance context to dispatches) (#456) (2026-05-10)
- #455 — feat(wave5): PR-W5.0 — prior-round findings injection (highest signal-to-effort smart-context) (#455) (2026-05-10)

## Active features

_No active features._

## Completed

### F43
All PRs merged. (#402)

## Planned (from ROADMAP.yaml)

### roadmap-autopilot — Roadmap Autopilot, Auto-Next Feature Loading, and Multi-Reviewer Gates
Status: planned

