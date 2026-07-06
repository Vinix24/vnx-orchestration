<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->

# VNX Feature Plan
**Last updated**: 2026-07-06T06:59:07.229673+00:00

## Recently Merged
_Last 14 days — sourced from git merge commits._

**Other**
- #1024 — fix(migrate): wire+repair the Horizon 0022-0031 pipeline into vnx migrate (P0) (#1024) (2026-07-06)
- #1023 — fix(lane): thread project_root from invocation context so central-mode workers spawn in the project, not the keystone (#1023) (2026-07-06)
- #1022 — docs(core): Horizon planning module (#1022) (2026-07-05)
- #1019 — docs(governance): sweep to shipped reality (#1001-1018) (#1019) (2026-07-05)
- #1021 — docs(intelligence): sweep to shipped reality (#1001-1018) (#1021) (2026-07-05)
- #1020 — docs(dispatch): sweep to shipped reality (#1001-1018) (#1020) (2026-07-05)
- #1018 — test(horizon): parity + ADR-007 cross-project isolation coverage [D3] (#1018) (2026-07-05)
- #1017 — feat(dispatch): configurable tmux-spawn concurrency via VNX_TMUX_MAX_CONCURRENT (N-slot semaphore, default 1) (#1017) (2026-07-05)
- #1016 — chore(dispatch): default tmux-spawn workers to --dangerously-skip-permissions (isolated worktree; VNX_WORKER_SCOPED=1 opts into scoped) (#1016) (2026-07-05)
- #1015 — refactor(horizon): rename pm skill → horizon + reconcile terminology (pm alias kept) [D2] (#1015) (2026-07-05)
- #1014 — feat(horizon): ship vnx horizon command group (full surface, tenant-safe resolver, objective/deliverable aliases) [D1] (#1014) (2026-07-05)
- #1013 — chore(providers): bump worker pin Sonnet 4.6 → Sonnet 5 (#1013) (2026-07-05)
- #1011 — feat(gov): signed budgeted audited gate override (recorded deviation, never silent) [D4] (#1011) (2026-07-05)
- #1012 — feat(gov): init provisions attest trust-root + ships the gate workflow (no key) [D5] (#1012) (2026-07-05)
- #1009 — feat(gov): server-side attestation verify gate, staged advisory + CODEOWNERS trust-root [D3] (#1009) (2026-07-05)
- #1010 — docs(intel): self-learning loop — operator-gated tiers, off-switches, philosophy [D7] (#1010) (2026-07-05)
- #1008 — feat(intel): operator-gated skill-refinement proposals from rework attribution [D6] (#1008) (2026-07-05)
- #1006 — feat(intel): tagger A/B harness (optional precision, no default-on) [D4] (#1006) (2026-07-05)
- #1007 — feat(gov): in-repo attest record, content-keyed + diff-bound (squash-safe) [D2] (#1007) (2026-07-04)
- #1005 — feat(intel): outcome-grounding shadow-verify V2 vs V1 + directional check [D5] (#1005) (2026-07-04)
- #1004 — feat(gov): SSH-key signing/verify + attestation manifest (test-key; keychain = operator step) [D1] (#1004) (2026-07-04)
- #1003 — feat(intel): schedule operator-gated proposal tier + gate stale-pattern supersede [D3] (#1003) (2026-07-04)
- #1002 — refactor(intel): drop dead success_rate column, reversible migration [D2] (#1002) (2026-07-04)
- #1001 — refactor(intel): explicit confidence range contract + keep subprocess delta path [D1] (#1001) (2026-07-04)
- #1000 — fix(reconcile): flip criterion requires 7 consecutive clean runs [Tier-2 finding] (#1000) (2026-07-04)
- #999 — docs(reconcile): closed-loop backward closure — reconcile, reopen valve, operational notes [D5] (#999) (2026-07-04)
- #998 — feat(reconcile): advisory-first continuous wiring — tick, review log, flip streak [D4] (#998) (2026-07-04)
- #997 — feat(reconcile): audited done→active reopen valve + re-close guard [D6] (#997) (2026-07-04)
- #996 — feat(reconcile): vnx objective reconcile — git-grounded batch auto-close [D3] (#996) (2026-07-04)
- #995 — refactor(reconcile): extract close_track_if_done with close-time revalidation [D2] (#995) (2026-07-04)
- #994 — feat(reconcile): ALL-merged multi-PR derivation + declared-done stability [D1] (#994) (2026-07-04)
- #993 — docs(release): 1.0.0 post-launch sweep — released framing + SSOT reframe + archive (#993) (2026-07-04)
- #992 — feat(skills): trigger-rich future-state skill descriptions + pm model-invocable (#992) (2026-07-03)
- #991 — feat(gate): full provider-family panel for plan-gate assurance (#991) (2026-07-03)
- #985 — docs(release): flip 1.0.0 RC framing to released 2026-07-02 (#985) (2026-07-02)
- #984 — fix(security): block /state/ path traversal in dashboard [P0] (#984) (2026-07-02)
- #983 — fix(launch): dependency + doc launch-blockers from the pre-1.0 provider-panel sweep (#983) (2026-07-01)
- #982 — docs(launch): 1.0 HN-launch readiness — reconcile release status + refresh stale manifesto stamps (#982) (2026-07-01)
- #981 — fix(governance): architecture batch A7+A6 — surface door-invariant breaches distinctly (#981) (2026-07-01)
- #980 — fix(governance): audit batch G2-G5 — phantom outcome-stamp, contract_hash gate, guard-error audit, exhaustive side-door scan (#980) (2026-07-01)
- #979 — fix(audit): hardening batch — watchdog/watcher recovery, mirror fail-open, SQLite WAL/busy_timeout (#979) (2026-07-01)
- #978 — fix(audit): correctness batch — override truthy, cross-project autowire, idempotency keys + clear-context test (#978) (2026-07-01)
- #977 — feat(governance): SessionStart hook-verifier for pre-assigned session id (F1.1) (#977) (2026-06-29)
- #976 — feat(governance): pre-assigned --session-id on the leaseless tmux spawn (F1.1) (#976) (2026-06-29)
- #975 — feat(governance): leaseless tmux lane writes its dispatch_metadata row with role (#975) (2026-06-29)
- #974 — feat(governance): governed tmux lane stamps agent role forward (#974) (2026-06-28)
- #973 — fix(dashboard): exclude benchmark from rework per-role FPY (honest panel) (#973) (2026-06-28)
- #971 — feat(dashboard): rework/skill-attribution observability panel (slice 1b) (#971) (2026-06-28)
- #970 — feat(intelligence): rework-attribution engine (slice 1a) (#970) (2026-06-28)
- #969 — feat(provenance): close dispatch→commit half of the chain (#969) (2026-06-28)
- #968 — feat(observability): governance/audit-trail dashboard page (self-learning + tagging + provenance + runtime) (#968) (2026-06-28)
- #967 — feat(observability): light up the provenance chain — populate the registry at append (+ fix latent upsert bug) (#967) (2026-06-28)
- #966 — feat(observability): tagging audit event — per-action tagger trail (dashboard panel 2) (#966) (2026-06-28)
- #965 — feat(smart-router): hybrid routing policy — capability-threshold, then cheapest (+ 2 missing classes) (#965) (2026-06-28)
- #963 — fix(audit): clear medium bugs — falsified old_value, json-list crash, truthy flag, doctor jq, requirements (C2,C5,D7,F6,F9) (#963) (2026-06-27)
- #962 — docs(audit): fix VNX_AUTOPILOT flag + SECURITY email + CONTRIBUTING marker + PyPI metadata (audit #16/D1,O1,O2,O5) (#962) (2026-06-27)
- #961 — fix(governance): enforce report contract + idempotency ordering + side-door CI (audit high #2,#3,#10) (#961) (2026-06-27)
- #960 — test(audit): fix the bare-FAIL test cluster (audit high #11-#15) (#960) (2026-06-27)
- #959 — fix(test): teach docs-command validation about the pip-only CLI surface (#959) (2026-06-27)
- #958 — fix(dx): worker-CLI doctor probe + dispatch preflight + prerequisites docs (audit high #6,#7,#8) (#958) (2026-06-27)
- #957 — refactor(demo): retire the demo command + demo mode (audit high #9) (#957) (2026-06-27)
- #956 — fix(install): add missing pyyaml dep + align pip-install docs with README (audit high #4,#5) (#956) (2026-06-27)
- #955 — fix(dashboard): default loopback bind + CSRF/token guard on mutating POSTs (audit critical #1) (#955) (2026-06-27)
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

## Active features

_No active features._

## Completed

### F1
All PRs merged. (#976 + #977)

### F6
All PRs merged. (#963)

## Planned (from ROADMAP.yaml)

### example-feature — Example feature — replace with your own roadmap entries
Status: planned

