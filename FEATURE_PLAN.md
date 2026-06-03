<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->

# VNX Feature Plan
**Last updated**: 2026-06-03T19:04:32.341342+00:00

## Recently Merged
_Last 14 days — sourced from git merge commits._

**Other**
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
- #797 — docs(t0): document the live planning/tracks method + worker-dispatch policy in T0 CLAUDE.md (#797) (2026-06-02)
- #796 — chore(roadmap): postpone launch +1 week + reflect 2026-06-02 overnight merges + activate 1.x queue (#796) (2026-06-02)
- #794 — fix(db-maintenance): atomic prune transaction (OI-2328) (#794) (2026-06-01)
- #793 — feat(planning): Phase 3 — advisory rollup reconciler (derived_status, idempotent, never auto-writes ROADMAP) (#793) (2026-06-01)
- #792 — fix(intelligence): repair 4 regressed nightly pipeline phases (OI-2331) (#792) (2026-06-01)
- #791 — feat(dashboard): planning kanban — objectives/deliverables/open-items by horizon (reuses design system) (#791) (2026-06-01)
- #790 — feat(planning): Phase 2 — deliverable plane + proposed->ready human gate (vnx deliverable add/list/promote) (#790) (2026-06-01)
- #789 — feat(dispatch): PR-1 flag-gated dispatch envelope (codex lane, VNX_UNIFIED_ENVELOPE, legacy default) (#789) (2026-06-01)
- #788 — feat(governance): unified_report as universal interface — AGENTS.md report-contract + generic report->receipt conversion (no hooks) (#788) (2026-06-01)
- #787 — feat(planning): Phase 1 — tracks seeder + horizon + deliverables view + vnx objective list (#787) (2026-06-01)
- #786 — feat(receipts): per-append hash-chain wiring, flag-gated VNX_CHAIN_RECEIPTS (GAP 3b) (#786) (2026-06-01)
- #785 — docs: provider-lanes USP doc + README hash-chain honesty fix (#785) (2026-06-01)
- #784 — feat(strategy): project ROADMAP -> strategy/roadmap.yaml + seed decisions (light up strategic_state boot surface) (#784) (2026-06-01)
- #783 — chore(roadmap): backfill 2026-06-01 work + fix tmux-lane drift (SSOT reconcile) (#783) (2026-06-01)
- #782 — feat(doctor): structural-doctor for tracks-layer activation (dry-run default, backup-on-apply) (#782) (2026-06-01)
- #781 — fix(tmux): bounded guarded-retry + adaptive settle for submit reliability (GAP 6) (#781) (2026-06-01)
- #780 — feat(intelligence): install nightly intelligence pipeline cron (GAP 4 — reactivate self-learning loop) (#780) (2026-06-01)
- #779 — feat(ops): opt-in DB maintenance for quality_intelligence.db (GAP 3a) (#779) (2026-06-01)
- #778 — feat(intelligence): dispatch_metadata provider/model column + idempotent migration (GAP 2) (#778) (2026-06-01)
- #777 — feat(reports): stamp uniform frontmatter on worker-authored reports (schema-uniform with synthesized) (#777) (2026-06-01)
- #776 — feat(receipts): uniform provider/model/lane across all lanes + resolve unknown-intermediate (#776) (2026-06-01)
- #775 — feat(tmux): CAPTURE — tmux-spawn conversation into EventStore (observable + minable, uniform) (#775) (2026-06-01)
- #774 — fix(tmux): tmux-spawn delivery — v2.1.159 readiness + verified submit (the worker now actually executes) (#774) (2026-06-01)
- #773 — feat(tmux): RECEIPT step — F1 receipt-presence guarantee (no lost subscription session) + dedup (#773) (2026-06-01)
- #772 — feat(tmux): GOVERN step — git-derived report synthesis + validate_body (shadow), kill placeholder report (#772) (2026-06-01)
- #771 — feat(tmux): T1 shared prepare() — permission preamble + worker-rules footer + report-contract directive (June-15 parity) (#771) (2026-06-01)
- #768 — chore(roadmap): consolidate SSOTs — ROADMAP.yaml current 1.0/1.x state, views derived (S0) (#768) (2026-06-01)
- #769 — docs(readme): honesty pass on tmux-lane maturity, lane uniformity, OpenRouter scope (#769) (2026-06-01)
- #751 — docs(contract): parallel multi-track execution contract supersedes chain contract (ADR-020) (#751) (2026-06-01)
- #767 — feat(dispatch): route default Claude lane through enriched tmux-spawn (dogfood + Wave-1b) (#767) (2026-06-01)
- #766 — fix(tmux-spawn): reuse skill+intelligence enrichment + emit unified_report (dogfood the default lane) (#766) (2026-06-01)
- #758 — chore(scrub): remove residual private-project artifacts from repo + wheel (#758) (2026-06-01)
- #757 — docs(readme): control-plane repositioning + governed-memory + architecture/ADRs + blog series (#757) (2026-06-01)
- #765 — feat(provider): governed DeepSeek-harness lane (own-key, key-auth, MCP-off — account-safe) (#765) (2026-06-01)
- #763 — fix(kimi): 1.44.0 content-block stream-json parser + fail-loud on empty extraction (incl. robust spawn) (#763) (2026-06-01)
- #770 — fix(dispatch): place prompt before variadic scope flags so the worker receives it (#770) (2026-06-01)
- #764 — fix(security): scope worker capabilities — drop skip-permissions + empty ambient MCP (interim, pre-full-binding) (#764) (2026-06-01)
- #761 — feat(governance): provider-aware intelligence + receipt token/cost capture (non-Claude parity) (#761) (2026-06-01)
- #760 — chore(governance): thin-T0 role + role-based manager block + auto-inject footer (packaged/wheel) (#760) (2026-06-01)
- #759 — refactor(rename): _vN scripts -> canonical names + compat shims (remove in 1.0.1) (#759) (2026-05-31)
- #753 — chore(security): remove dead prep-applier + de-advertise private products from public wheel (#753) (2026-05-30)
- #752 — fix(security): pre-publish hardening — bash_check RCE + heartbeat shell-injection + strip private portfolio from wheel (#752) (2026-05-30)
- #750 — refactor(hygiene): extract _validate_review_evidence helpers (no behavior change) (#750) (2026-05-30)
- #749 — fix(hygiene): size findings are advisory (warning), not blocking + OI backfill (#749) (2026-05-30)
- #747 — fix(migrate): create runtime_schema_version table before stamping it (#747) (2026-05-30)
- #746 — fix(resolver-unify): unify project_id + DB path resolution for pool, dream, init (#746) (2026-05-30)
- #745 — fix(migrate): seed default pool_config row + stamp runtime_schema_version (#745) (2026-05-30)
- #743 — fix(migrate): bootstrap runtime_coordination.db + fix XDG path resolution for track/pool (#743) (2026-05-30)
- #742 — chore(release): bump version rc3 → 1.0.0 for public launch (#742) (2026-05-29)
- #740 — fix(blk-initmigrate): bootstrap runtime DBs in vnx init; add vnx migrate command (#740) (2026-05-29)
- #741 — fix(selflearn): unify canonical DB path + add nightly dream phase (#741) (2026-05-29)
- #738 — docs(1.0): update roadmap + archive superseded docs for 1.0 launch state (#738) (2026-05-29)
- #730 — feat(ci): add Profile D pip-install smoke test (PR-CI-SMOKE) (#730) (2026-05-29)
- #737 — fix(dream): guided exit when resolve_project_root fails outside git repo (#737) (2026-05-29)
- #736 — feat(dispatch): scope register_dispatch to composite (dispatch_id, project_id) — ADR-007 (#736) (2026-05-29)
- #732 — chore(packaging): wheel hygiene — exclude pycache/tests/benchmarks, remove stale dist/ (#732) (2026-05-29)
- #735 — fix(dream): route all file I/O through canonical vnx_paths data root (#735) (2026-05-29)
- #733 — fix(blk-readme): make quickstart work on clean pip install (#733) (2026-05-29)
- #734 — fix(blk-initpath): guard _scaffold_vnx_data_local behind inside_project (#734) (2026-05-29)
- #731 — fix(bootstrap): route dream/track through _engine bootstrap; guided pool status error (#731) (2026-05-29)
- #729 — feat(ra6-autopilot-tick): autopilot_tick + scheduler wiring, ships DARK (#729) (2026-05-29)
- #728 — Fix pool status project display (#728) (2026-05-29)
- #727 — feat(pool): N-3 wire VNX_POOL_TASK_CONSUMER to pool_worker_runner spawn (#727) (2026-05-29)
- #726 — feat(pool): N-2 pool_worker_runner single-claim entrypoint (#726) (2026-05-29)
- #725 — fix(ra3b-gate-holes): close 4 advance-gate bypass holes (RA-3b) (#725) (2026-05-29)
- #720 — feat(dispatch): atomic claim_next_queued_dispatch + migration 0026 (PR-N-1) (#720) (2026-05-29)
- #724 — fix(gate): fetch origin/<branch> before diff, use origin/main...origin/<branch> (#724) (2026-05-29)
- #723 — feat(ra5-step-driver): add step subcommand driving active feature PR queue (#723) (2026-05-29)
- #721 — feat(ra4-human-gate): add human approval gate primitive to roadmap advance (#721) (2026-05-29)
- #722 — fix(dream): resolve quality DB via canonical vnx_paths.resolve_state_dir (#722) (2026-05-29)
- #719 — fix(dream): kill process group on kimi timeout, add entry log (PR-DREAM-HANG-2) (#719) (2026-05-29)
- #718 — feat(ra3-gate-enforce): enforce review-gate evidence in reconcile/advance (#718) (2026-05-29)
- #717 — test(fut-2b): ADR-007 structural regression tests for track child tables (#717) (2026-05-29)
- #716 — feat(ra2-materialize): branch + worktree provisioning in load_feature (#716) (2026-05-29)
- #715 — feat(dispatch): extend VNX_ISOLATED_WORKTREE to provider_dispatch (PR-PROVIDER-ISO) (#715) (2026-05-29)
- #714 — feat(ra1-projectid): ADR-007 project_id stamp on roadmap_state.json + receipts (#714) (2026-05-29)
- #713 — fix(hygiene): wire vulture, clear 100%-confidence dead-code findings (OI-001/OI-002/OI-005) (#713) (2026-05-29)
- #712 — feat(dispatch): extend repo-map enrichment to all providers (PR-REPOMAP) (#712) (2026-05-29)
- #711 — fix(dream): guard empty-DB hang + add kimi timeout (GAP-7/ADR-019) (#711) (2026-05-29)
- #710 — fix(central-qdb): detect partial-init QI DB and complete bootstrap (OI-011) (#710) (2026-05-29)
- #709 — feat(dispatch): complete smart-router for non-Claude providers + constraint-aware routing (#709) (2026-05-29)
- #708 — feat(dream-scheduler-v2): install-scheduler subcommands + GAP-7 receipt preflight (#708) (2026-05-29)
- #707 — fix(dream-mode-tier): register dream in TIER_OPERATOR_ONLY + bin/vnx dispatch (#707) (2026-05-29)
- #706 — fix(pool): terminate subprocess and remove worktree on scale_down and reap (OI-010) (#706) (2026-05-29)
- #705 — feat(dispatch): per-dispatch git worktree isolation, env-gated VNX_ISOLATED_WORKTREE (default off) (#705) (2026-05-29)
- #656 — feat(governance): PreToolUse hook blocks raw claude worker-spawns, enforces subprocess_dispatch (#656) (2026-05-29)
- #702 — feat(smart-routing-activate): cost-aware auto-route with VNX_AUTO_ROUTE env gate (#702) (2026-05-29)
- #701 — fix(kimi-dispatch-enrichment): wire intelligence injection into kimi dispatch path (#701) (2026-05-29)
- #699 — fix(a14-wire-0025): wire 0025_dream_consolidation.sql into quality-DB bootstrap (#699) (2026-05-29)
- #697 — fix(a14-pr2-fix): address all 5 codex blocking findings on PR #696 (#697) (2026-05-29)
- #691 — fix(issue-687): dispatches sqlite_sequence preservation in 0022 (#691) (2026-05-29)
- #684 — feat(h-cost-tracking): universal cost tracking for all providers (#684) (2026-05-29)
- #683 — refactor(hyg-4): extract subparser registrations and dispatch chain from main() (#683) (2026-05-29)
- #682 — feat(d-role-prompts): add database-engineer, intelligence-engineer, security-engineer roles + update architect (#682) (2026-05-29)
- #679 — feat(int-1): adrs table + FTS5 + indexer (PR-INT-1) (#679) (2026-05-29)
- #678 — feat(skills): add STEP 0 ADR-check to 5 skill SKILL.md files (#678) (2026-05-29)
- #677 — fix(trace): dual-scheme pr_id+pr_number + backfill — cat-C gap 73% → 0% (PR-B-TRACE) (#677) (2026-05-29)
- #676 — feat(future): track layer schema + CLI + migration (PR-FUT-1) (#676) (2026-05-28)
- #675 — feat(route): enforce provider_constraints.yaml in dispatch pre-flight (PR-ROUTE-1) (#675) (2026-05-28)
- #674 — docs(readme): rewrite for pip-1.0 + tmux-leaseless-lane + multi-provider architecture (PR-DOC-README) (#674) (2026-05-28)
- #673 — fix(docs): align QUICKSTART/MIGRATION with real pip-CLI surface + hello-world examples/ fallback (PR-DOC-1) (#673) (2026-05-28)
- #672 — fix(lane): compute success from receipt status + shell-quote completion-protocol values (PR-HYG-3) (#672) (2026-05-28)
- #671 — refactor(start): drop upfront T1-T3 pane grid — T0 only at startup, workers spawn on-demand (PR-START-1) (#671) (2026-05-28)
- #670 — fix(quality): emit tool_unavailable warnings + declare quality-extras in pyproject (PR-QUAL-1) (#670) (2026-05-28)
- #669 — fix(obs): codex_spawn model-default to gpt-5.5 + token-usage extraction (PR-OBS-1) (#669) (2026-05-28)
- #668 — chore(hygiene): remove schraplijst-Phase-1 dead modules (~4.1k LOC, fresh-verified) (PR-HYG-2B) (#668) (2026-05-28)
- #667 — chore(hygiene): remove audit-identified dead code (unused imports, unreachable bin/vnx cases, ongebruikte helpers) (PR-HYG-2A) (#667) (2026-05-28)
- #659 — refactor(migrate-central): split main into _parse_args + _run_apply (OI-1540/1532, part 3/3) (#659) (2026-05-26)
- #658 — docs(rc9): changelog + readme sync (cheap-lanes, constraint-rename, OI-refactors, governance fixes) (#658) (2026-05-26)
- #657 — refactor(migrate-central): extract migrate_schema module (OI-1536/1533, part 2/3) (#657) (2026-05-26)
- #655 — feat(governance): traceability_audit tool — cross-ref PRs/commits/dispatches/receipts, report gaps (#655) (2026-05-26)
- #654 — feat(governance): emit pr_merged receipt linking pr_number on merge/closure (fixes FPY/history gap) (#654) (2026-05-26)
- #653 — refactor(migrate-central): extract migrate_import module (OI-1537/1539, part 1/3) (#653) (2026-05-26)
- #652 — feat(subprocess-dispatch): execute cheap-lane via provider_dispatch instead of Claude fallback (CL2) (#652) (2026-05-26)
- #650 — refactor(install-central): move shim content to template file (OI-1562) (#650) (2026-05-26)
- #649 — refactor(doctor): extract worktree + settings checks from cmd_doctor (OI-1573) (#649) (2026-05-26)
- #648 — refactor(receipt-proc): extract mtime-calc python from bootstrap-protection (OI-1525/1524) (#648) (2026-05-26)
- #643 — refactor(providers): rename deepseek-path-d-blocked -> deepseek-harness-subscription-blocked; allow own-key+hardening, block subscription-redirect (#643) (2026-05-26)
- #644 — fix(provider-dispatch): non-claude (kimi/codex) receipt captures correct status + output + tokens (#644) (2026-05-26)
- #647 — refactor(dispatcher): extract stuck-cleanup python + supervisor-ticks (OI-1521/1523) (#647) (2026-05-26)
- #646 — refactor(benchmark): extract source-info + report-writers from main (OI-1510) (#646) (2026-05-26)
- #645 — refactor(quality-db): bootstrap_qi_db migration registry (OI-1542/1544/1541) (#645) (2026-05-26)
- #642 — fix(quality-advisory): relax shell size thresholds (func 50->60, file 500->600) to cut false-positive OIs (#642) (2026-05-26)
- #640 — fix(install): preserve project worker_permissions overrides across cutover (#640) (2026-05-26)
- #638 — docs(strategy): three-tier public/private layering model (#638) (2026-05-26)
- #639 — feat(supervisor): flood-safe crash-recovery sweep for orphaned active dispatches (#639) (2026-05-26)
- #641 — fix(migration): 0017 dispatches/terminal_leases rebuild preserves all columns dynamically (unblocks build_t0_state) (#641) (2026-05-26)
- #637 — fix(dispatch): complexity-scaled chunk_timeout / total_deadline defaults (#637) (2026-05-26)
- #636 — fix(runtime-coord): add terminal_leases.worker_pid + schema-drift regression guard (#636) (2026-05-26)
- #635 — fix(runtime-coord): self-heal worker_states.project_id in init (OI-095) (#635) (2026-05-26)
- #627 — fix(install-central): shim exec bin/vnx not vnx-cli (was typo) (#627) (2026-05-25)
- #626 — feat(wave2a-8): housekeeping — stale session cleanup + runbook D3/D4 (#626) (2026-05-25)
- #625 — fix(wave2a-7): T0 template legacy path + doctor cross-platform glob (#625) (2026-05-25)
- #624 — fix(wave2a-6): installer template-leak — no developer paths in install output (#624) (2026-05-25)
- #623 — fix(wave2a): env isolation check + runbook v3 (PR-WAVE2A-5) (#623) (2026-05-25)
- #622 — fix(wave2a): test-mode apply --test-apply flag (PR-WAVE2A-4) (#622) (2026-05-25)
- #621 — fix(wave2a): backup retention policy --cleanup-backups (PR-WAVE2A-3) (#621) (2026-05-25)
- #620 — fix(wave2a): per-project migrator mode --project flag (PR-WAVE2A-2) (#620) (2026-05-25)
- #619 — fix(wave2a): TCC pre-flight check + per-project backup access probe (PR-WAVE2A-1) (#619) (2026-05-25)
- #618 — docs(wave2a): robust pipeline architecture blueprint (PR-WAVE2A-BLUEPRINT) (#618) (2026-05-25)
- #617 — fix(migrator): canonical bootstrap dispatch_experiments + rollback exception masking (PR-MIGRATOR-FIX-V2) (#617) (2026-05-24)
- #616 — fix(migrator): canonical bootstrap schema-order for --fresh-central (PR-MIGRATOR-FIX) (#616) (2026-05-24)
- #615 — feat(dashboard): VNX-branded light theme (PR-DASH-V2) (#615) (2026-05-21)
- #614 — fix(resume): VNX_RESUME_IN_PROGRESS bypass for PAUSED-guards (#614) (2026-05-21)
- #612 — feat(safety): PAUSED marker guards in daemon scripts (#612) (2026-05-21)
- #613 — fix(singleton): atomic flock(2) replaces mkdir-race (closes OI-1518) (#613) (2026-05-21)

**WAVE 6**
- #692 — chore(h2-v2): T0 skill + CLAUDE.md hardening (Wave 6 pool discovery) (#692) (2026-05-29)

**WAVE-5**
- #680 — feat(int-2): Wave-5 ADR injection in dispatch context (#680) (2026-05-29)

**WAVE0**
- #756 — chore(wave0): untrack .venv+scratch (keep FEATURE_PLAN/PR_QUEUE), strip emoji/buzzwords (#756) (2026-05-31)

**WAVE4**
- #633 — docs(wave4): central-install runbook + cutover procedure (PR-WAVE4-6) (#633) (2026-05-25)
- #632 — test(wave4): central-install e2e integration tests (PR-WAVE4-5) (#632) (2026-05-25)
- #631 — fix(regen-settings): guard against central-install write; detect pre-fix contamination (PR-WAVE4-4) (#631) (2026-05-25)
- #630 — fix(bin): preserve VNX_PROJECT_ROOT across env reset; add write guard; redirect cmd_update (PR-WAVE4-3) (#630) (2026-05-25)
- #629 — fix(install-central): write install-mode marker, export VNX_PROJECT_ROOT from shim, fix verify_install (PR-WAVE4-2) (#629) (2026-05-25)
- #628 — fix(install-central): separate PROJECT_ROOT from VNX_HOME in central-install mode (PR-WAVE4-1) (#628) (2026-05-25)

**WAVE6**
- #698 — fix(wave6-pool-lease-spawn): insert terminal_leases row before add_member in pool scale_up (#698) (2026-05-29)

## Active features

_No active features._

## Completed

### F1
All PRs merged. (#773)

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

### usage-aware-routing — Usage/budget aggregator + usage-aware routing (clean-API + admin-API + receipt-OBSERVE) + Mission Control widget
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
Status: planned

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

