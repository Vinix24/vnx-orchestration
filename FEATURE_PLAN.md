<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->

# VNX Feature Plan
**Last updated**: 2026-05-13T18:08:15.599112+00:00

## Recently Merged
_Last 14 days — sourced from git merge commits._

**Other**
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
- #394 — fix(followups): contract-based request_reviews + public event_store + session_id pop (W3J) (#394) (2026-05-01)
- #393 — fix(infra): .geminiignore dashboard + role default + notification scope (W3I) (#393) (2026-05-01)
- #392 — chore: audit + close deferred-PR OIs (W5D) (#392) (2026-05-01)
- #389 — fix(misc): 5 small fixes (W5C) (#389) (2026-05-01)
- #391 — fix(append_receipt): NDJSON-first ordering + flag derived from record (W4H) (#391) (2026-05-01)
- #390 — fix(security/validator): tenant_id from session + D-8 blocking + slug-check push events (W3H) (#390) (2026-05-01)
- #388 — fix(isolation): project-scoped sockets/locks/tmpfiles (W4G) (#388) (2026-05-01)
- #387 — fix(subprocess): timeout misclassification + SIGKILL fallback + non-blocking readline (W3E) (#387) (2026-05-01)
- #386 — fix(misc): runtime_core marker + report_path safety + tz datetime + heartbeat scope (W5B) (#386) (2026-05-01)
- #385 — fix(gemini): dedupe file content + hallucination guardrail (W3G) (#385) (2026-05-01)
- #384 — fix(subprocess): manifest-scoped git ops + worker attribution (W4F) (#384) (2026-05-01)
- #383 — fix(gate-receipts): commit_sha + dispatch_id top-level + _mark_unavailable propagation (W3F) (#383) (2026-05-01)
- #382 — fix(t0-state): feature_state schema split + any-ID filter (W4E) (#382) (2026-05-01)
- #381 — fix(schema): add report_findings table for phase-3 session-dispatch-link (W4D) (#381) (2026-05-01)
- #380 — fix(state): singleton lock race + session_ids refresh (W4C) (#380) (2026-05-01)
- #379 — chore: CHANGELOG fix + GovernanceProfile rename + gate-status check (W5A) (#379) (2026-05-01)
- #378 — fix(runtime): lease release on every completion path + heartbeat subprocess cleanup (W4B) (#378) (2026-05-01)
- #377 — fix(gates): resolve PR-branch file content via git show, not cwd (W3D) (#377) (2026-05-01)
- #375 — fix(session-store): fcntl.flock around read-modify-write (W4A) (#375) (2026-05-01)
- #376 — fix(guards): haiku parser type guards + run_id regex + drain idempotency (W3C) (#376) (2026-05-01)
- #374 — refactor(append_receipt): split into focused modules (W1C) (#374) (2026-05-01)
- #373 — fix(hygiene): unused imports, import order, multi-import lines, validation, fail-closed git diff (W3B) (#373) (2026-05-01)
- #372 — fix(gate-enforcement): set -e safe RC capture + BSD-portable grep (W3A) (#372) (2026-05-01)
- #371 — refactor(test_dispatch_register): split tests for size compliance (W2D) (#371) (2026-05-01)
- #370 — refactor(dispatch_register): split append_event (W2C) (#370) (2026-05-01)
- #368 — refactor(subprocess_dispatch): split into focused modules (W1A) (#368) (2026-05-01)
- #369 — refactor(receipt_processor): split into sourced helper libs (W1B) (#369) (2026-05-01)
- #367 — refactor(t0_decision_summarizer): split _parse_haiku + main (W2B) (#367) (2026-05-01)
- #366 — refactor(gate_request_handler): split long methods (W2A) (#366) (2026-05-01)
- #358 — fix(rebase): resolve intelligence_selector conflict — preserve CFX-5 + Migration P1 (#358) (2026-05-01)
- #365 — docs(headless): missing transition doc + 4-mode matrix in README + adapter docs (#365) (2026-05-01)
- #362 — fix(dispatcher): cross-project contamination guard (OI-1067) (#362) (2026-04-30)
- #359 — fix(oi-1100): receipt processor recovers expired leases (#359) (2026-04-30)
- #357 — feat(cfx-17): gate-side codex severity translator (#357) (2026-04-30)
- #355 — test(dashboard): TS fixture-completeness gate (CFX-14) (#355) (2026-04-30)
- #354 — fix(dashboard): SSE + timer unmount cleanup + lifecycle tests (CFX-13) (#354) (2026-04-30)
- #352 — fix(intelligence): content-addressable pattern_id stability (CFX-5) (#352) (2026-04-30)
- #353 — test(governance): codex stream parser version-fixture suite (CFX-9) (#353) (2026-04-30)
- #351 — refactor(intelligence): tag_combination as JSON array (CFX-6) (#351) (2026-04-30)
- #350 — test(dashboard): tighten console-error filter, never mask React warnings (CFX-15) (#350) (2026-04-30)
- #349 — fix(governance): rc_release_on_failure cleanup_complete handling (CFX-16) (#349) (2026-04-30)
- #348 — fix(scripts): path resolution sweep + smoke gate (CFX-11) (#348) (2026-04-30)
- #346 — feat(operator): ARC pipeline → suggest CLI + dashboard widget (ARC-5) (#346) (2026-04-30)
- #347 — feat(intelligence): F57 insights reader + recommendation aggregator (ARC-4) (#347) (2026-04-30)
- #345 — feat(intelligence): receipt classifier with provider abstraction (ARC-3) (#345) (2026-04-30)
- #344 — fix(intelligence): verify failure-decay end-to-end + regression tests (ARC-2) (#344) (2026-04-30)
- #343 — fix(launchd): generic reload_plist.sh + active-jobs smoke test (ARC-1) (#343) (2026-04-30)
- #342 — refactor(governance): summarizers use gate_status.is_pass() (CFX-12) (#342) (2026-04-30)
- #341 — fix(observability): append_receipt stderr discipline (CFX-8) (#341) (2026-04-30)
- #340 — fix(observability): use full SHA-256 for instruction_sha256 (CFX-10) (#340) (2026-04-30)
- #338 — fix(intelligence): pattern dedup + injection diversity (P1 mid) (#338) (2026-04-30)
- #339 — fix(governance): subprocess manifest lifecycle active->completed cleanup (CFX-7) (#339) (2026-04-30)
- #337 — fix(intelligence): fcntl-lock worker_health.json RMW (CFX-4) (#337) (2026-04-30)
- #336 — test(ci): smoke tests + nightly canary workflow (PR-T4) (#336) (2026-04-30)
- #335 — test(intelligence): integration tests for end-to-end learning loop (PR-T2) (#335) (2026-04-30)
- #334 — feat(multi-tenant): add project_id column (migration Phase 0) (#334) (2026-04-30)
- #333 — docs(t0): codify policies + remove dead scripts + audit-driven cleanup (#333) (2026-04-30)
- #332 — feat(observability): component health beacon framework (PR-T1) (#332) (2026-04-30)
- #330 — fix(launchd): conversation analyzer plist path + smoke test (#330) (2026-04-30)
- #329 — docs(manifesto): operator-narrative + v0.10.0 changelog + headless transition (#329) (2026-04-30)
- #331 — fix(ci): tighten Legacy Path Gate regex to literal .vnx-data/state/ (#331) (2026-04-30)
- #326 — fix(intelligence): stamp dispatch_id at injection time (P0) (#326) (2026-04-30)
- #328 — feat(intelligence): activate T0 decision log + outcome reconciliation (P0) (#328) (2026-04-30)
- #327 — fix(intelligence): reconcile pattern confidence stores (P0 quick win) (#327) (2026-04-30)
- #301 — feat(governance): ci_gate audit type for review-gate framework (Tier 3) (#301) (2026-04-30)
- #303 — fix(round3): codex round-2 findings PR #303 — StreamEvent compat + rotation timing (#303) (2026-04-30)
- #302 — feat(observability): dispatch_created + dispatch_promoted register events (Tier 5) (#302) (2026-04-30)
- #321 — fix(governance): closure_verifier CLI flag forwarding + E2E coverage (CFX-2) (#321) (2026-04-30)
- #320 — fix(governance): subprocess dispatch git-scope manifest (CFX-1) (#320) (2026-04-30)
- #317 — feat(supervisor): runtime_supervise + 60s dispatcher tick (SUP-PR3) (#317) (2026-04-30)
- #300 — fix(round3): codex round-2 findings PR #300 — branch forwarding + contradictions detector (#300) (2026-04-30)
- #311 — fix(governance): dedup confidence updates after VNX-R4 (Tier 4) (#311) (2026-04-30)
- #305 — test(dashboard): F60 Playwright console + network error detection (Tier 6) (#305) (2026-04-30)
- #325 — docs(readme): v0.10.0 — supervisor + observability + codex severity (16 PRs) (#325) (2026-04-30)
- #316 — feat(supervisor): lease_sweep + dispatcher prelude tick (SUP-PR2) (#316) (2026-04-30)
- #322 — refactor(governance): canonicalize gate result schema (CFX-3) (#322) (2026-04-29)
- #315 — refactor(supervisor): single-owner cleanup_worker_exit helper (SUP-PR1) (#315) (2026-04-29)
- #307 — feat(observability): codex+gemini token tracking in receipts (Tier 5 GAP-4) (#307) (2026-04-29)
- #319 — feat(supervisor): receipt_processor_supervisor.sh + integration tests (SUP-PR4) (#319) (2026-04-29)
- #306 — build(dashboard): tsc strict-mode tightening + typecheck script (Tier 6) (#306) (2026-04-29)
- #308 — test(dashboard): F60 Playwright network failure scenarios (Tier 6) (#308) (2026-04-29)
- #324 — fix(governance): codex severity prompt tightening (vertex_ai_runner) (#324) (2026-04-29)
- #323 — fix(governance): codex severity prompt tightening (#323) (2026-04-29)

**W1**
- #356 — fix(night-w1-prebugs): consolidate 4 pre-existing main bugs (OI-1227..1230) (#356) (2026-04-30)

**W2**
- #361 — refactor(night-w2-cluster-a): split 5 oversize functions into helpers (#361) (2026-04-30)
- #360 — fix(night-w2-oi1107): pass --role through subprocess delivery path (#360) (2026-04-30)

**W3**
- #363 — refactor(night-w3): split cluster B — 5 oversize functions (OI-1089/1090/1091/1200/1202) (#363) (2026-04-30)

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

