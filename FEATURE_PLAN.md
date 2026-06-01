<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->

# VNX Feature Plan
**Last updated**: 2026-06-01T02:46:13.019489+00:00

## Recently Merged
_Last 14 days — sourced from git merge commits._

**Other**
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

**WAVE 5**
- #601 — docs: refresh README + ROADMAP for 1.0.0-rc2 (Wave 5/6/7/8 shipped + central install) (#601) (2026-05-18)

**WAVE 6**
- #692 — chore(h2-v2): T0 skill + CLAUDE.md hardening (Wave 6 pool discovery) (#692) (2026-05-29)

**WAVE-5**
- #680 — feat(int-2): Wave-5 ADR injection in dispatch context (#680) (2026-05-29)

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

_No completed features found in register or PR history._

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
Status: planned

### launch-option-b-parity — Option B parity (all lanes): uniform prepare()/govern() across subprocess + tmux + all provider lanes
Status: planned

### kimi-deepfix-major2 — Kimi #763 info-bug deep fix + MAJOR-2 regression test
Status: planned

### log-rotation — Log-rotation fix: event-stream ring-buffer growth + receipt log size bounds
Status: planned

### opt-a-envelope-extraction — Option A envelope extraction: dispatch_envelope.py + LaneRouter + delete legacy duplicated PREPARE/GOVERN
Status: planned

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

