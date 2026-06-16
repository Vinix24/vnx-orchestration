<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->

# VNX Feature Plan
**Last updated**: 2026-06-16T12:24:20.067762+00:00

## Recently Merged
_Last 14 days — sourced from git merge commits._

**Other**
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
- #858 — PR-E (future-state): kanban honesty — degraded signal, no silent drops, staleness truth (#858) (2026-06-13)
- #857 — PR-0 (future-state): enforced test-isolation guard (no test mutates the live DB) (#857) (2026-06-13)
- #853 — docs(provenance): hash-chain ADR-023 + receipt/pipeline truth-pass (events_path, .vnx-data paths) (#853) (2026-06-13)
- #856 — test(hygiene): prevent production events-dir contamination + reconcile 3 silent test failures (#595 mock, #811 staging-guard) (#856) (2026-06-13)
- #855 — fix(t0-skill): correct dead dispatch path + intelligence.sh paths + §12 prefixes + CLAUDE.md stale blocks (#855) (2026-06-13)
- #854 — docs: CHANGELOG #840-#851 wave + ROADMAP shipped-item reclass + README date/tmux-default truth (#854) (2026-06-13)
- #851 — chore(roadmap): PM-reconcile overnight wave (#842-#850) — status sync to bb8f6159 (#851) (2026-06-11)
- #850 — fix(selflearn): injection-history-aware suppression breaks duplicate dominance (93% dup root-cause) (#850) (2026-06-11)
- #849 — feat(tracks): oi-lifecycle-closure — track done, oi-close, dispatch linkage backfill, status --tracks (#849) (2026-06-11)
- #848 — fix(dispatch): broker atomicity (orphan window, claim TOCTOU) + adapter pipe hygiene (H1/H6) (#848) (2026-06-11)
- #847 — feat(outcome): dispatch_metadata backfill tool + contract_invalid vocab sync (gate-F2) (#847) (2026-06-11)
- #846 — docs: truth-pass for 1.0 launch — version labels, ADR amendments, surface sync, provenance (#846) (2026-06-11)
- #845 — fix(tmux-lane): truthful completion receipts + extra-flags quoting (H3/H5) (#845) (2026-06-11)
- #844 — fix(routing): resolve kimi lane/constraint conflict + generalize raw-spawn guard to provider CLIs (#844) (2026-06-11)
- #843 — fix(observability): unify state/events path resolution + events_path receipt pointer (H2) (#843) (2026-06-11)
- #842 — fix(schema-init): view-ordering on legacy DBs unblocks v22/v23 (nightly phase-0 failure mode 2) (#842) (2026-06-11)
- #841 — chore(roadmap): PM-close — sweep-fix closure (LB-4..7 done, blockers resolved, evidence in pr_queue) (#841) (2026-06-10)
- #833 — feat(dashboard): lane-aware agent stream (replace fixed T0-T3) (#833) (2026-06-10)
- #840 — fix(audit-chain): distinguish unchained from broken ledger in verify (LB-5) (#840) (2026-06-10)
- #837 — fix(digest): event-type filter + canonical outcome vocabulary (#837) (2026-06-10)
- #838 — fix(nightly): Python 3.9 compat — lazy annotations in quality_db_init + version guard in pipeline (#838) (2026-06-10)
- #835 — fix(init): wire governance hooks block into scaffolded settings.json (LB-6) (#835) (2026-06-10)
- #839 — docs(changelog): add [1.0.0] release entry (LB-7) (#839) (2026-06-10)
- #836 — fix(scrub): remove client project data from wheel + docs index path (LB-4) (#836) (2026-06-10)
- #834 — chore(roadmap): PM-reconcile — sweep-blockers LB-4..7, 13 new features, status/freshness fixes (#834) (2026-06-10)
- #832 — fix(packaging): exclude benchmark dev-tooling from wheel (LB-3 artifact grep) (#832) (2026-06-09)
- #831 — feat(bench): codex lane + 6/6 skill-injection E2E + seed-decontaminatie + 1.0 docs (#831) (2026-06-07)
- #830 — feat(benchmark): field-tests T2 medium + T3 complex task-definitions (#830) (2026-06-04)
- #829 — feat(roadmap): park sales-copilot VNX upgrade (1.0.1, post-launch) (#829) (2026-06-04)
- #828 — feat(benchmark): field-tests realistic-bench suite — infrastructure + T1 task-defs (#828) (2026-06-04)
- #827 — chore(roadmap): privacy trim — slim public notes, move details to private state (#827) (2026-06-04)
- #825 — docs(bootstrap): tmux-spawn lane in CLAUDE bootstrap snippet + ops doc (#825) (2026-06-04)
- #824 — docs(t0-skill): tmux-spawn dispatch as default; subprocess-dispatch for terminal-pinned work (#824) (2026-06-04)
- #823 — fix(provider-dispatch): _dispatch_gemini respects args.model (OI-155) (#823) (2026-06-03)
- #822 — feat(smart-router): quality_tier discriminator + per-task min/max gates (PR-SR-FIX-3) (#822) (2026-06-03)
- #821 — fix(claude-spawn): capture completion_text from stream-json assistant events (#821) (2026-06-03)
- #820 — feat(roadmap): add bench-v2 smart-lanes benchmark to 1.1 milestone (#820) (2026-06-03)
- #819 — fix(provider-dispatch): uniform central-path resolution (OI-126) (#819) (2026-06-03)
- #818 — fix(smart-router): null-cost sort + strategy-tag (cost-collapse bug) (#818) (2026-06-03)
- #817 — feat(digest): D2 — progress_table + minimal digest skeleton (Phase-1) (#817) (2026-06-03)
- #816 — feat(infra): atomic_io.py + ADR-021 — digest D1 per architecture V2 (#816) (2026-06-03)
- #815 — docs(digest): architecture v2 — redesign per B3.1 trigger on PR #814 (#815) (2026-06-03)
- #811 — feat(governance): enforce /pending dispatch path (close T0 direct-call bypass) (#811) (2026-06-03)
- #812 — feat(oi): bulk pattern subcommands + 1.0 closing sprint (96→48 open) (#812) (2026-06-03)
- #813 — feat(smart-lanes): PR-2 Smart Router cost-tier classifier (flag-gated, default-off) (#813) (2026-06-03)
- #810 — chore(roadmap): refresh launch_state.note for 2026-06-03 scope-pull (9-juni launch) (#810) (2026-06-03)
- #808 — fix(receipts): dedup recent_receipts per dispatch_id (keep best status) (#808) (2026-06-03)
- #795 — feat(dispatch): PR-2 envelope claude-subprocess adapter (flag-gated, dual-receipt-safe) (#795) (2026-06-03)
- #806 — fix(dispatcher): survive scans that skip/reject all dispatches (set -e leak) + observable rejection (#806) (2026-06-03)
- #805 — fix(selflearn): control-for-difficulty model inference + decision-grade digest (no harmful routing, no truncation/dups) (#805) (2026-06-03)
- #804 — feat(governance): activate profile-gate resolver in request_reviews() (#804) (2026-06-03)
- #803 — feat(tracks): track_type + next_action_owner discriminator (additive, SQLite, 1.0.1) (#803) (2026-06-03)
- #802 — fix(reconciler): derive done from track.pr_ref + merged-state (decouple from legacy A/B/C dispatch.track join) (#802) (2026-06-02)
- #801 — feat(planning): dispatch->track linkage backfill (dry-run default, backup-on-apply) (#801) (2026-06-02)
- #800 — feat(planning): turn-on — vnx objective sync (auto-seed, human-gated) + advisory drift-gate (#800) (2026-06-02)
- #799 — feat(governance): worker-permission relay — auto-accept window + catastrophic hard-list + escalation (no silent hangs) (#799) (2026-06-02)
- #798 — fix(tmux): hook-driven version-agnostic lane signals + wire subagent-block (#798) (2026-06-02)

## Active features

_No active features._

## Completed

### F2
All PRs merged. (#847)

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

