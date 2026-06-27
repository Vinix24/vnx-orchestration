<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->

# VNX Feature Plan
**Last updated**: 2026-06-27T21:40:41.439897+00:00

## Recently Merged
_Last 14 days — sourced from git merge commits._

**Other**
- #963 — fix(audit): clear medium bugs — falsified old_value, json-list crash, truthy flag, doctor jq, requirements (C2,C5,D7,F6,F9) (#963) (2026-06-27)
- #962 — docs(audit): fix VNX_AUTOPILOT flag + SECURITY email + CONTRIBUTING marker + PyPI metadata (audit #16/D1,O1,O2,O5) (#962) (2026-06-27)
- #961 — fix(governance): enforce report contract + idempotency ordering + side-door CI (audit high #2,#3,#10) (#961) (2026-06-27)
- #960 — test(audit): fix the bare-FAIL test cluster (audit high #11-#15) (#960) (2026-06-27)
- #959 — fix(test): teach docs-command validation about the pip-only CLI surface (#959) (2026-06-27)
- #958 — fix(dx): worker-CLI doctor probe + dispatch preflight + prerequisites docs (audit high #6,#7,#8) (#958) (2026-06-27)
- #957 — refactor(demo): retire the demo command + demo mode (audit high #9) (#957) (2026-06-27)
- #956 — fix(install): add missing pyyaml dep + align pip-install docs with README (audit high #4,#5) (#956) (2026-06-27)
- #955 — fix(dashboard): default loopback bind + CSRF/token guard on mutating POSTs (audit critical #1) (#955) (2026-06-27)
- #954 — chore(roadmap): seed the multi-provider repo-audit remediation backlog (#954) (2026-06-27)
- #953 — feat(config): runtime honours UI-set config — gate/dispatch flags (P0 PR 6b) (#953) (2026-06-27)
- #952 — feat(config): runtime honours UI-set config — intelligence read-sites (P0 PR 6a) (#952) (2026-06-27)
- #951 — feat(config): config control-plane page — toggles + approval modal + audit drawer (P0 PR 5) (#951) (2026-06-27)
- #950 — feat(config): config control-plane API — GET inventory/audit + POST set (P0 PR 4) (#950) (2026-06-27)
- #949 — feat(config): config control-plane DAO — set_config + DB-resolver (P0 PR 3) (#949) (2026-06-27)
- #948 — feat(config): config_store_db — config control-plane persistence (P0, PR 2) (#948) (2026-06-27)
- #947 — feat(config): config_registry SSOT — operator config control-plane foundation (P0) (#947) (2026-06-27)
- #946 — feat(dashboard): surface the planning (future-state) layer as an operator page (#946) (2026-06-27)
- #945 — feat(dashboard): surface system health as an operator page (#945) (2026-06-27)
- #944 — feat(dashboard): add future-ready (Queued) lane to the kanban (#944) (2026-06-27)
- #943 — feat(dashboard): show scout-enrichment per dispatch in the kanban (#943) (2026-06-27)
- #942 — fix(status): resolve data dir via ensure_env, not install-relative (#225) (#942) (2026-06-27)
- #941 — fix(doctor): make consumer-setup checks layout-aware (standalone-dev WARN) (#941) (2026-06-27)
- #940 — feat(cli): add `vnx skills sync` — propagation refresh for project skills (#940) (2026-06-27)
- #939 — docs(t0): reconcile T0 CLAUDE.md to the current skill + arc (#939) (2026-06-27)
- #938 — feat(intelligence): wire the LLM tagger at persist-time (deepseek default) (#938) (2026-06-27)
- #879 — fix(migration): 0031 ADR-007 runtime tenant + FK repair (#879) (2026-06-15)
- #878 — chore(audit): backfill utility for recent worktree-only receipts (dry-run-first) (#878) (2026-06-15)
- #876 — fix(reconciler): defer to declared phase when no dispatch/PR evidence (prevents done→queued downgrade) (#876) (2026-06-15)
- #877 — docs(operations): add transcript backup & archive guide + index entry (#877) (2026-06-15)
- #875 — docs(fsr): sync docs to the future-state reconciliation batch (1.0.1) (#875) (2026-06-14)
- #871 — feat(fsr-d): wire OI→track bridge + reconcile into autopilot_tick, gate advance on sync failure (#871) (2026-06-14)
- #873 — feat(roadmap): local-model PM-gate plan (1.x) + honest FSR batch status (1.0.1) (#873) (2026-06-14)
- #872 — feat(skill-preapprove): pre-approve worker-role skills so detached lanes don't stall (#872) (2026-06-14)
- #862 — feat(fsr-c): open-item→track bridge through tracks.py (R4.1–R4.4, D1/D3/D5) (#862) (2026-06-14)
- #863 — feat(fsr-b): tenant-scoped canonical tracks in build_t0_state (#863) (2026-06-14)
- #861 — feat(fsr-a2): version reconciliation via declarative invariant manifest (#861) (2026-06-14)
- #859 — feat(fsr-a1): ADR-007 dispatches in-place rebuild — composite UNIQUE, 12-step, fail-closed tenant (#859) (2026-06-14)
- #860 — chore(kimi): raise default per-chunk stall threshold 300s→600s (#860) (2026-06-14)

## Active features

_No active features._

## Completed

### F6
All PRs merged. (#963)

## Planned (from ROADMAP.yaml)

### roadmap-autopilot — Roadmap Autopilot, Auto-Next Feature Loading, and Multi-Reviewer Gates
Status: shipped-dark

### fut-1 — Track-layer schema + DAL + CLI + audit-ordering (PR-FUT-1)
Status: done

### fut-2 — Track tenant-scoping per ADR-007 — composite PK over project_id, seeding from roadmap, composite-FK dispatches.track → tracks(track_id, project_id), project-aware CLI
Status: done

### launch-wave0-hygiene — 1.0 launch — Wave 0 hygiene: untrack .venv + scratch, strip emoji/buzzwords (keep FEATURE_PLAN/PR_QUEUE tracked)
Status: done

### launch-renames — 1.0 launch — Wave 1 renames: _vN files -> canonical names + compat shims (remove in 1.0.1)
Status: done

### launch-scrub — 1.0 launch — Wave 1 scrub: remove residual private-project artifacts from repo + wheel
Status: done

### launch-readme — 1.0 launch — Wave 1 credibility: README control-plane repositioning + ADR section + architecture diagram
Status: done

### launch-governance-core — 1.0 launch — Wave 1b: role-based manager block + auto-inject T0-action footer (enforcement, not convention)
Status: done

### launch-provider-intel — 1.0 launch — Wave 2: provider-aware intelligence + receipt token/cost + report linkage (ADR-007 provider column)
Status: done

### launch-kimi-robust — 1.0 launch — Wave 2: kimi 1.44.0 robust spawn + fail-loud on empty extraction (drop --yolo)
Status: done

### launch-capability-interim — 1.0 launch — Wave 2: scope worker capabilities (drop --dangerously-skip-permissions, empty ambient MCP)
Status: done

### launch-deepseek-lane — 1.0 launch — governed DeepSeek-harness lane (own-key, key-auth, MCP-off, account-safe)
Status: done

### launch-adr020-contract — 1.0 launch — ADR-020 parallel multi-track execution contract (design-ratified; implementation 1.2)
Status: done

### launch-tmux-spawn-enrichment — 1.0 launch — fix(tmux-spawn): reuse skill+intelligence enrichment + emit unified_report (dogfood default lane)
Status: done

### launch-tmux-default-routing — 1.0 launch — feat(dispatch): route default Claude lane through enriched tmux-spawn (dogfood + Wave-1b)
Status: done

### launch-readme-honesty — 1.0 launch — docs(readme): honesty pass on tmux-lane maturity, lane uniformity, OpenRouter scope
Status: done

### tmux-lane-structural-refactor — 1.0 launch — June-15-critical: tmux subscription-lane PREPARE+GOVERN structural completion (tmux lane only, strangler-fig)
Status: done

### launch-option-b-parity — Option B parity (all lanes): uniform prepare()/govern() across subprocess + tmux + all provider lanes
Status: planned

### kimi-deepfix-major2 — Kimi #763 info-bug deep fix + MAJOR-2 regression test
Status: planned

### log-rotation — Log-rotation fix: event-stream ring-buffer growth + receipt log size bounds
Status: planned

### opt-a-envelope-extraction — Option A envelope extraction: dispatch_envelope.py + LaneRouter + delete legacy duplicated PREPARE/GOVERN
Status: in-progress

### capability-binding-full — Full role->capability binding: mcp_servers allowlist + permission_mode per role + un-dead generate_claude_settings + per-adapter materialization + permission_enforcement receipt field
Status: planned

### per-folder-agents — Per-folder agents: folder-addressable role = skill + permission-profile + model
Status: planned

### openrouter-arbitrary — OpenRouter-arbitrary / OpenAI-compat proxy-gated lane class
Status: planned

### provider-hardening — Provider hardening: fail-loud on empty extraction + CI-canary per CLI version + version-pin
Status: planned

### bench-v2-smart-lanes — Smart Lanes field-tests benchmark
Status: planned

### gemma-4-12b-integration — Gemma 4 12B local-lane evaluation
Status: planned

### aef-style-enrichment-layer — Dispatch enrichment between staging and pending
Status: planned

### usage-aware-routing — Usage/budget aggregator + usage-aware routing
Status: planned

### gap4-self-learning-reactivation — GAP 4: reactivate self-learning loop — nightly intelligence pipeline cron
Status: done

### gap6-submit-reliability — GAP 6: bounded guarded-retry + adaptive settle for tmux submit reliability
Status: done

### gap5-report-pipeline-decision — GAP 5: report_pipeline retention decision (keep — load-bearing)
Status: done

### tracks-layer-activation — Structural-doctor: repair v26-but-absent-tracks divergence + activate dark FUT-1/2 tracks layer
Status: done

### planning-future-state-layer — Planning object model: TRACK(=feature) -> DISPATCH(execution leaf) -> output+receipt future-state layer
Status: shipped-dark

### receipt-hashchain-wire — GAP 3b: per-append hash-chain wire + epoch rotation + verify_history + fork-tests
Status: in-progress

### provider-lanes-doc-pass — Provider-lanes USP doc + README honesty fix + ROADMAP/docs reflect tracks-activation
Status: done

### report-to-receipt-converter — Universal report->receipt converter: validates report_body_contract, no hooks, generic conversion
Status: done

### oi-2331-intelligence-repair — OI-2331: repair 4 regressed nightly intelligence pipeline phases after GAP 4 reactivation dormancy
Status: done

### oi-2328-atomic-prune — OI-2328: atomic prune transaction for DB maintenance (vnx_db_maintenance.py)
Status: done

### sales-copilot-vnx-upgrade — Sales-copilot project — VNX install upgrade to post-1.0
Status: planned

### launch-sweep-blockers — 1.0 pre-publish blocker fix-set: wheel client-data scrub, audit_chain unchained-status, init hooks-wiring, CHANGELOG 1.0.0
Status: done

### oi-222-ssrf-hardening — OI-222: SSRF hardening — is_global instead of is_private (CGNAT 100.64/10) + DNS-rebinding IP-pinning, then wire url_policy into fetch paths
Status: planned

### oi-223-lane-safety-loader — OI-223: lane_safety loader — make routing_policy.yaml lane_safety block live (force_headless now hardcoded in lane_adapter.HEADLESS_FORCED_MODELS)
Status: planned

### oi-224-bench-runner-edges — OI-224: bench-runner edge-cases — retry isolation (headless shared checkout), stale-report prefix match, worktree-removal result
Status: planned

### oi-225-bench-harness-generalize — OI-225: generalize bench harness into a shippable feature (seeds-free, token-free, bring-your-own-tasks)
Status: planned

### nightly-pipeline-python-compat — Nightly pipeline phase 0-schema-init: Python 3.9 crash fix (Path | None at quality_db_init.py:905) + crontab PATH + version guard
Status: done

### outcome-normalization — Dispatch-outcome integrity: weekly_digest classifier vocab + event-type filter, status-default policy, dispatch_metadata backfill (73% unknown root-caused)
Status: in-progress

### adr007-composite-keys-batch — ADR-007 batch: project_id + composite UNIQUE on SPC/intelligence tables (governance_metrics, spc_*, success_patterns, antipatterns, prevention_rules, dispatch_experiments, retry_budgets, dream_pattern_archives)
Status: planned

### dashboard-ops-completion — Dashboard operational completion: launch-script PORT export, /api/events/stream lane-aware migration, README port truth
Status: done

### oi-lifecycle-closure — Track/OI lifecycle closure path: vnx track done, oi-close/unlink_open_item, dispatch<->track linkage migration, vnx status --tracks, queue hygiene
Status: done

### otel-wire-or-degrade — OTel decision: wire OTEL_EXPORTER_OTLP_ENDPOINT (+ tmux-lane callpoints) or degrade otel deps to [observability] extra
Status: planned

### sweep-hardening-batch1 — Sweep hardening batch 1: state-dir guard, routing-policy conflict, tmux receipt truth, extra-flags quoting, broker atomicity, stderr drain, provider-CLI bypass guard
Status: done

### roadmap-governance-hygiene — Roadmap/docs governance hygiene: PM-mutator enforcement, drift-snapshot freshness, stale ROADMAP docs archive, docs-truth pass
Status: in-progress

### injection-history-suppression — Self-learning quality: injection-history-aware suppression breaks 93% duplicate dominance
Status: done

### future-state-rebuild-batch — Future-state rebuild batch (FSR): ADR-007 in-place dispatches rebuild + version-reconciliation manifest + tenant-scoped build_t0_state + OI->track bridge -> autopilot_tick wiring
Status: in-progress

### pm-gate-agent-automation — PM-gate agent automation — event-driven per-receipt roadmap maintenance
Status: planned

### audit-sec-dashboard-auth — Dashboard: default loopback bind + token/CSRF auth on mutating POSTs (audit critical #1)
Status: done

### audit-dep-pyyaml — Add missing pyyaml runtime dependency (audit high #5)
Status: done

### audit-docs-pip-install — Fix pip-install-not-on-PyPI doc contradiction (audit high #4)
Status: done

### audit-demo-modernize — Demo: retire the redundant 4-tmux T1-T3 model (audit high #9)
Status: done

### audit-dx-doctor-worker-cli — vnx doctor: probe worker CLIs it dispatches to (audit high #7)
Status: done

### audit-dx-dispatch-preflight — dispatch-agent: preflight claude binary + surface the failure reason (audit high #6)
Status: done

### audit-dx-prerequisites-docs — Add a Prerequisites section to the pip happy-path (audit high #8)
Status: done

### audit-gov-report-parser-contract — report_parser: enforce body-contract + phantom-guard before task_complete (audit high #10)
Status: done

### audit-gov-sidedoor-ci — Enforce the dispatch side-door audit in CI (audit high #2)
Status: done

### audit-correctness-receipt-idempotency — Receipt idempotency: write-ahead the dedup marker (audit high #3)
Status: done

### audit-tests-build-project-status — Fix brittle source-grep test (audit high #11)
Status: done

### audit-tests-build-t0-brief — Fix build_t0_brief rc=1-on-empty-state_dir tests (audit high #12)
Status: done

### audit-tests-fsr-b-tenant-guard — Fix tenant-guard test escaping tmp isolation (audit high #13)
Status: done

### audit-tests-fsr-isolation-guard — Fix isolation-guard fixture missing project-id marker (audit high #14)
Status: done

### audit-tests-feedback-loop — Fix feedback-loop silent column-swallow (audit high #15)
Status: done

### audit-docs-autopilot-flag — Fix non-existent VNX_AUTOPILOT flag in docs (audit high #16)
Status: done

### audit-sweep-medium — Repo-audit medium-severity sweep (41 findings)
Status: in-progress

### audit-sweep-low — Repo-audit low-severity / AI-slop sweep (39 findings)
Status: planned

