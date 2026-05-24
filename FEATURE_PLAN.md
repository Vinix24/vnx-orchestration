<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->

# VNX Feature Plan
**Last updated**: 2026-05-21T07:04:56.577840+00:00

## Recently Merged
_Last 14 days — sourced from git merge commits._

**Other**
- #614 — fix(resume): VNX_RESUME_IN_PROGRESS bypass for PAUSED-guards (#614) (2026-05-21)
- #612 — feat(safety): PAUSED marker guards in daemon scripts (#612) (2026-05-21)
- #613 — fix(singleton): atomic flock(2) replaces mkdir-race (closes OI-1518) (#613) (2026-05-21)
- #611 — feat(centralisatie): vnx pause/resume + partial-failure cleanup runbook (Dag 2 PR C) (#611) (2026-05-20)
- #610 — feat(benchmark): FTS5 rebuild benchmark for SEOcrawler maintenance window (Dag 2 PR B) (#610) (2026-05-20)
- #609 — feat(centralisatie): dispatch_id collision resolver + schema-prep migrations (Dag 2 PR A) (#609) (2026-05-20)
- #608 — feat(wave2a): bump 1.0.0-rc3 + dry-run tests against 3 projects (#608) (2026-05-20)
- #607 — feat(migration): schema versioning + rollback playbook (Wave 2a prep) (#607) (2026-05-20)
- #606 — fix(dashboard): bundle OI-1494 fixes (5 items) + agent-stream archive replay (#606) (2026-05-20)
- #605 — fix(receipts): project_id scoping audit + fixes (Wave 2a prep) (#605) (2026-05-20)
- #604 — fix(centralisatie): project_id scoping audit + critical write-leak fixes (#604) (2026-05-20)
- #603 — docs: refresh HANDOFF + README status banner (master roadmap link) (#603) (2026-05-20)
- #588 — feat(schema): idempotent bootstrap with PRAGMA user_version + transaction wrapping (#588) (2026-05-18)
- #591 — feat(doctor): vnx doctor --strict for central-install pre-flight validation (#591) (2026-05-18)
- #587 — feat(install): install-central.sh for centralized VNX install with project-pin (#587) (2026-05-18)
- #600 — feat(intelligence): A/B random-skip framework with weekly lift report (#600) (2026-05-18)
- #596 — fix(governance): _emit_governance retry-with-backoff instead of SystemExit (Kimi audit) (#596) (2026-05-18)
- #589 — feat(skill-coverage): pre-flight skill-coverage scanner for central install (#589) (2026-05-18)
- #593 — fix(intelligence): catalogus-hygiene + recency-decay (audit BLOCKER #2) (#593) (2026-05-18)
- #595 — refactor(dispatch): shared runtime_overrides module (eliminate delivery/recovery duplicate) (#595) (2026-05-18)
- #594 — feat(intelligence): fine-grained task_class subclass + scope_tags activation (#594) (2026-05-18)
- #592 — fix(pool): real-subprocess integration tests for pool spawn (Sonnet audit BLOCKER #1) (#592) (2026-05-18)
- #586 — feat(vnx_paths): .vnx-overrides resolver for per-project central-install customization (#586) (2026-05-17)
- #590 — feat(schema): migration 0021 central install metadata + pin tracking (#590) (2026-05-17)
- #584 — feat(cli): vnx version + vnx update subcommands for central install rollback (#584) (2026-05-17)
- #585 — feat(pyproject): pipx-installable wheel with vnx console_script (#585) (2026-05-17)
- #583 — chore: sync VERSION + pyproject.toml to 1.0.0-rc2 (#583) (2026-05-17)
- #582 — fix(provider_dispatch): rotate per-terminal NDJSON ring buffer post-dispatch (#582) (2026-05-17)
- #580 — fix(smart-router): end-to-end auto-route wiring + enforcer false-positive on Kimi CLI provider (2 blockers) (#580) (2026-05-17)
- #578 — fix(governance): atomic OI saves + WAL migrations + closure_verifier self-reference loop (3 blockers + 4 high) (#578) (2026-05-17)
- #576 — fix(security): PRAGMA allowlist + project_id_fn multi-tenant + missing contracts + dup ImportError (2 blockers + 4 medium) (#576) (2026-05-17)
- #574 — fix(intelligence): payload 2000 + dead method removal + project_id_fn + silent except logging (3 blockers + dead code) (#574) (2026-05-17)
- #573 — fix(bench): BENCH terminal-id + stdin judge prompt + anonymized model-id + drop duplicate + count-leak (3 blockers + 2 warns) (#573) (2026-05-17)
- #572 — fix(wiring-gate): use ${VNX_STATE_DIR} placeholder in docstring (Legacy-path-gate false-positive) (#572) (2026-05-17)
- #571 — feat(gate): wiring-gate dead-code detection (#571) (2026-05-17)
- #568 — fix(receipts): persist token_usage + cost_usd + pr_id for subprocess worker dispatches (#568) (2026-05-17)
- #567 — fix(pool): integration-fix PR-6.5a tests after PR-6.5b worktree-create + e2e integration test (#567) (2026-05-17)
- #565 — feat(pr-sr-4): smart_router wiring + route_decisions.ndjson (opt-in via --auto-route) (#565) (2026-05-17)
- #566 — feat(pr-6.5b): per-worker git worktree manager (#566) (2026-05-17)
- #564 — feat(pr-6.5c): worker heartbeat + PID validation in reaper (#564) (2026-05-17)
- #563 — fix(kimi-spawn): handle Kimi CLI Wire Protocol camelCase events (TurnBegin/ContentPart/TextPart/etc) (#563) (2026-05-17)
- #562 — feat(pr-6.5e): per-worker .vnx-data/workers/<terminal_id>/ subdirectory (#562) (2026-05-17)
- #559 — feat(pr-sr-2): constraint enforcer with HardConstraintViolation + shell guard (#559) (2026-05-17)
- #561 — feat(pr-sr-3): smart_router classifier + recommendation lookup (pure, unwired) (#561) (2026-05-17)
- #560 — feat(pr-d5-f): provider spawns emit unified_report v1 frontmatter (shadow-mode) (#560) (2026-05-17)
- #558 — feat(pr-6.5a): real subprocess spawn in pool_manager (replaces stub from PR-6.3) (#558) (2026-05-17)
- #557 — feat(pr-6.5d): migration auto-apply hook in T0 state bootstrap (#557) (2026-05-17)
- #556 — feat(pr-d5-e): unified report schema enforcement (JSONSchema + validator + guardrail) (#556) (2026-05-17)
- #555 — feat(sr-1): provider constraints SSOT (7 hard guard-rails) (#555) (2026-05-17)
- #554 — refactor(intelligence): split intelligence_selector.py (2511 LOC) per source module (#554) (2026-05-17)
- #552 — feat(p0b): token usage + cost tracking end-to-end for all 5 providers (#552) (2026-05-17)
- #551 — feat(p0a): intelligence injection for all providers (codex/gemini/litellm) (#551) (2026-05-17)
- #548 — data(benchmark): 56-dispatch model comparison results + routing recommendations (#548) (2026-05-17)
- #547 — feat(benchmark): suite infrastructure (9 models x 7 tasks orchestrator + judge + analyzer) (#547) (2026-05-16)
- #537 — fix(oi-1479): token_usage extraction + cost_usd computation per provider (#537) (2026-05-16)
- #533 — fix(oi-1476): align project_id regex + yaml placeholder substitution (#533) (2026-05-16)
- #508 — chore(hardening): narrow silent-except across 7 hot files (14 findings, OI-1437) (#508) (2026-05-15)
- #509 — chore(hardening): narrow silent-except across 13 singleton files (13 findings, OI-1437) (#509) (2026-05-15)
- #506 — refactor(replay_harness): modularize into package — close OI-1443/1444/1445/1446/1447 (#506) (2026-05-15)
- #507 — refactor(cleanup_worker_exit): decompose 104-line function — close OI-1448 (#507) (2026-05-15)
- #505 — docs(adr-003): clarify API-key + CLI permitted; SDK still banned (#505) (2026-05-15)
- #504 — refactor(conversation_analyzer): modularize into package — close OI-1438/1439/1440/1441/1442 (#504) (2026-05-15)
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

**WAVE 0**
- #443 — chore(repo-hygiene): Wave 0 — OI-1373 Tier 3 + 9 stale docs archive (combined) (#443) (2026-05-09)

**WAVE 1**
- #457 — docs: refresh README + CHANGELOG for post-rc1 work (Wave 1 + Wave 5 P0/P1) (#457) (2026-05-10)

**WAVE 5**
- #601 — docs: refresh README + ROADMAP for 1.0.0-rc2 (Wave 5/6/7/8 shipped + central install) (#601) (2026-05-18)
- #569 — docs: Wave 5/6/7/8 documentation overhaul (#569) (2026-05-17)

**WAVE 8**
- #570 — fix(wiring): activate 4 dead-code modules from Wave 8 fast-path (auto_apply, validator class, schema-emit, smart_router.route) (#570) (2026-05-17)

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
- #513 — feat(wave4.6): PR-4.6.6 — unified event shape via CanonicalEvent + EventStore enforcement (#513) (2026-05-15)
- #512 — feat(wave4.6): PR-4.6.5 — litellm_spawn handler extracted from litellm_adapter (#512) (2026-05-15)
- #511 — feat(wave4.6): PR-4.6.3 — codex_spawn handler extracted from codex_adapter (#511) (2026-05-15)
- #510 — feat(wave4.6): PR-4.6.4 — gemini_spawn handler extracted from gemini_adapter (#510) (2026-05-15)
- #490 — feat(wave4.6): PR-4.6.2 — extract claude_spawn from subprocess_dispatch (byte-identical) (#490) (2026-05-14)
- #488 — feat(wave4.6): PR-4.6.1 — provider_dispatch.py entry-point (additive shim) (#488) (2026-05-14)

**WAVE5**
- #532 — feat(wave5): PR-5.7 — operator demo runbook + Control Centre docs + completion report (#532) (2026-05-16)
- #530 — feat(wave5): PR-5.6 — hybrid dispatch routing with receipt-tail lifecycle tracker (#530) (2026-05-16)
- #528 — feat(wave5): PR-5.5 — Control Centre CLI shell skill + operator commands (#528) (2026-05-16)
- #525 — feat(wave5): PR-5.2 — per-project T0 lifecycle management (spawn/heartbeat/kill/reap) (#525) (2026-05-16)
- #524 — feat(wave5): PR-5.4 — cross-project intelligence aggregator (global + per-project facets) (#524) (2026-05-16)
- #523 — feat(wave5): PR-5.3 — multi-tenant lease isolation (schema v12) (#523) (2026-05-16)
- #522 — feat(wave5): PR-5.1 — multi-project state aggregator write-pad (#522) (2026-05-16)
- #521 — docs(wave5): PR-5.0 — ADR-017 Control Centre product-shape architecture (#521) (2026-05-16)
- #462 — feat(wave5): CFX-W5-2 — plumbing gaps (pr_id key + headless daemon entry points) (#462) (2026-05-13)
- #461 — feat(wave5): PR-W5.5 — production plumbing for P0-P4 context-bundle classes (#461) (2026-05-10)
- #460 — feat(wave5): PR-W5.4 — schema introspection injection (DDL grounding for DB workers) (#460) (2026-05-10)
- #459 — feat(wave5): PR-W5.3 — operator memory injection (curated wisdom into worker context) (#459) (2026-05-10)
- #458 — feat(wave5): PR-W5.2 — code anchor injection (file:line current-state grounding) (#458) (2026-05-10)
- #456 — feat(wave5): PR-W5.1 — ADR injection by file-touch (governance context to dispatches) (#456) (2026-05-10)
- #455 — feat(wave5): PR-W5.0 — prior-round findings injection (highest signal-to-effort smart-context) (#455) (2026-05-10)

**WAVE6**
- #575 — fix(wave6): real spawn impl + config field reads + single heartbeat threshold (3 blockers) (#575) (2026-05-17)
- #546 — fix(wave6): OI cleanup group 1 (idempotency + regex + ledger + audit) (#546) (2026-05-16)
- #544 — feat(wave6): PR-6.8 — Control Centre pool-integration (cross-project pool view + supervisor) (#544) (2026-05-16)
- #543 — feat(wave6): PR-6.7 — vnx pool CLI (status/scale/config/reap subcommands) (#543) (2026-05-16)
- #542 — feat(wave6): PR-6.6 — health monitoring + dead-worker reap (tick = reap → decide → execute) (#542) (2026-05-16)
- #541 — feat(wave6): PR-6.5 — provider-mix per pool with lowest-share-first allocation (#541) (2026-05-16)
- #540 — feat(wave6): PR-6.4 — pluggable scaling policies (queue_depth_v1 + cost_aware_v1) (#540) (2026-05-16)
- #539 — feat(wave6): PR-6.3 — PoolManager core (decision engine + state repo + manager) (#539) (2026-05-16)
- #538 — feat(wave6): PR-6.2 — schema v14 elastic worker pool tables + migration scripts (#538) (2026-05-16)
- #535 — feat(wave6): PR-6.1 — vnx_workers.yaml + WORKER_REGISTRY (ADR-013 implementation) (#535) (2026-05-16)
- #534 — feat(wave6): PR-6.0 — ADR-018 elastic worker pool design freeze (#534) (2026-05-16)

**WAVE7**
- #579 — fix(wave7): claude cost tracking + kimi audit-gap status + redact prompt in logs (3 blockers) (#579) (2026-05-17)
- #577 — fix(wave7): claude cost tracking + kimi audit-gap status + redact prompt in logs (3 blockers) (#577) (2026-05-17)
- #550 — feat(wave7): PR-7.7 — Kimi CLI as 5th provider (OAuth via kimi login) (#550) (2026-05-17)
- #545 — fix(wave7): OI cleanup group 2 — litellm usage stream + unified report .md suffix (#545) (2026-05-16)
- #536 — feat(wave7): PR-7.6 — provider governance unification (receipt + unified report for all providers) (#536) (2026-05-16)
- #531 — feat(wave7): vnx.env loader + DeepSeek V4-Pro/V4-Flash model registry update (#531) (2026-05-16)
- #520 — feat(wave7): PR-7.5 — provider behavior contracts (capabilities + tool-shape + cache-control) (#520) (2026-05-15)
- #519 — feat(wave7): PR-7.4 — cost-routing policy engine (feature-flag gated) (#519) (2026-05-15)
- #518 — feat(wave7): PR-7.3 — GLM-5.1 lane via OpenRouter (z.AI direct deferred) (#518) (2026-05-15)
- #517 — feat(wave7): PR-7.2 — Kimi K2.6 + K2-0905 lane via LiteLLM Moonshot endpoint (#517) (2026-05-15)
- #516 — feat(wave7): PR-7.1 — DeepSeek V4 lane via LiteLLM subprocess bridge (#516) (2026-05-15)
- #515 — feat(wave7): PR-7.0 — ADR-015 LiteLLM Path B for DeepSeek/Kimi/GLM integration (#515) (2026-05-15)

## Active features

_No active features._

## Completed

_No completed features found in register or PR history._

## Planned (from ROADMAP.yaml)

### roadmap-autopilot — Roadmap Autopilot, Auto-Next Feature Loading, and Multi-Reviewer Gates
Status: planned

