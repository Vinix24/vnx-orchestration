<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_pr_queue.py -->

# PR Queue — VNX 1.0 launch (DERIVED VIEW)

> Authoritative source: `ROADMAP.yaml` (`launch_state` + `features[].pr_queue`).
> Live PR state: `gh pr list`. Do not hand-edit this file.

## Progress Overview
Launch status: **pre-launch** (version 1.0.0)
Last verified: 2026-06-10 against origin/main@bb8f6159
Merged launch PRs: 49 | Queued: 0

## Status

### Merged
- #756 — chore(wave0): untrack .venv + scratch, strip emoji/buzzwords [feature=launch-wave0-hygiene]
- #759 — refactor(rename): _vN files -> canonical names + compat shims [feature=launch-renames]
- #758 — chore(scrub): remove residual private-project artifacts [feature=launch-scrub]
- #757 — docs(readme): control-plane repositioning + ADR section + arch diagram [feature=launch-readme]
- #760 — feat(wave1b): role-based manager block + auto-inject T0-action footer [feature=launch-governance-core]
- #761 — feat(wave2): provider-aware intelligence + receipt token/cost [feature=launch-provider-intel]
- #763 — fix(kimi): robust spawn + fail-loud on empty extraction [feature=launch-kimi-robust]
- #764 — fix(worker): scope capabilities — drop --dangerously-skip-permissions, empty MCP [feature=launch-capability-interim]
- #765 — feat(providers): governed DeepSeek-harness lane [feature=launch-deepseek-lane]
- #751 — docs(adr): ADR-020 parallel multi-track execution contract [feature=launch-adr020-contract]
- #766 — fix(tmux-spawn): reuse skill+intelligence enrichment + emit unified_report [feature=launch-tmux-spawn-enrichment]
- #767 — feat(dispatch): route default Claude lane through enriched tmux-spawn [feature=launch-tmux-default-routing]
- #769 — docs(readme): honesty pass on tmux-lane maturity, lane uniformity, OpenRouter scope [feature=launch-readme-honesty]
- #771 — feat(tmux): PREPARE structural completion (tmux lane) [feature=tmux-lane-structural-refactor]
- #772 — feat(tmux): GOVERN structural completion (tmux lane) [feature=tmux-lane-structural-refactor]
- #773 — feat(tmux): RECEIPT structural completion (tmux lane) [feature=tmux-lane-structural-refactor]
- #774 — feat(tmux): delivery structural completion (tmux lane) [feature=tmux-lane-structural-refactor]
- #775 — feat(tmux): CAPTURE structural completion (tmux lane) [feature=tmux-lane-structural-refactor]
- #776 — feat(tmux): uniform-receipts structural completion (tmux lane) [feature=tmux-lane-structural-refactor]
- #779 — feat(ops): opt-in DB maintenance for quality_intelligence.db (GAP 3a) [feature=log-rotation]
- #789 — feat(dispatch): PR-1 flag-gated dispatch envelope (codex lane, VNX_UNIFIED_ENVELOPE, legacy default) [feature=opt-a-envelope-extraction]
- #780 — feat(intelligence): install nightly intelligence pipeline cron (GAP 4 — reactivate self-learning loop) [feature=gap4-self-learning-reactivation]
- #781 — fix(tmux): bounded guarded-retry + adaptive settle for submit reliability (GAP 6) [feature=gap6-submit-reliability]
- #782 — feat(doctor): structural-doctor for tracks-layer activation (dry-run default, backup-on-apply) [feature=tracks-layer-activation]
- #787 — feat(planning): Phase 1 — tracks seeder + horizon + deliverables view + vnx objective list [feature=planning-future-state-layer]
- #790 — feat(planning): Phase 2 — deliverable plane + proposed->ready human gate (vnx deliverable add/list/promote) [feature=planning-future-state-layer]
- #791 — feat(dashboard): planning kanban — objectives/deliverables/open-items by horizon (reuses design system) [feature=planning-future-state-layer]
- #793 — feat(planning): Phase 3 — advisory rollup reconciler (derived_status, idempotent, never auto-writes ROADMAP) [feature=planning-future-state-layer]
- #786 — feat(receipts): per-append hash-chain wiring, flag-gated VNX_CHAIN_RECEIPTS (GAP 3b) [feature=receipt-hashchain-wire]
- #785 — docs: provider-lanes USP doc + README hash-chain honesty fix [feature=provider-lanes-doc-pass]
- #788 — feat(governance): unified_report as universal interface — AGENTS.md report-contract + generic report->receipt conversion (no hooks) [feature=report-to-receipt-converter]
- #792 — fix(intelligence): repair 4 regressed nightly pipeline phases (OI-2331) [feature=oi-2331-intelligence-repair]
- #794 — fix(db-maintenance): atomic prune transaction (OI-2328) [feature=oi-2328-atomic-prune]
- #836 — fix(scrub): remove client project data from wheel + docs index path (LB-4) [feature=launch-sweep-blockers]
- #840 — fix(audit-chain): distinguish unchained from broken ledger in verify (LB-5) [feature=launch-sweep-blockers]
- #835 — fix(init): wire governance hooks block into scaffolded settings.json (LB-6) [feature=launch-sweep-blockers]
- #839 — docs(changelog): add [1.0.0] release entry (LB-7) [feature=launch-sweep-blockers]
- #838 — fix(nightly): python3.9 compat (from __future__ import annotations) + version guard [feature=nightly-pipeline-python-compat]
- #842 — fix(schema-init): view-ordering on legacy DBs unblocks v22/v23 (phase-0 failure mode 2) [feature=nightly-pipeline-python-compat]
- #837 — fix(digest): event-type filter + canonical outcome vocabulary (fix A + event_type fallback) [feature=outcome-normalization]
- #847 — feat(outcome): dispatch_metadata backfill tool + contract_invalid vocab sync (gate-F2) [feature=outcome-normalization]
- #833 — feat(dashboard): lane-aware agent stream (incl. ops-completion commit 54b96194) [feature=dashboard-ops-completion]
- #849 — feat(tracks): oi-lifecycle-closure — track done, oi-close, dispatch linkage backfill, status --tracks [feature=oi-lifecycle-closure]
- #843 — fix(observability): unify state/events path resolution + events_path receipt pointer (H2, item 1) [feature=sweep-hardening-batch1]
- #844 — fix(routing): resolve kimi lane/constraint conflict + generalize raw-spawn guard to provider CLIs (items 2+7) [feature=sweep-hardening-batch1]
- #845 — fix(tmux-lane): truthful completion receipts + extra-flags quoting (H3/H5, items 3+4) [feature=sweep-hardening-batch1]
- #848 — fix(dispatch): broker atomicity (orphan window, claim TOCTOU) + adapter pipe hygiene (H1/H6, items 5+6) [feature=sweep-hardening-batch1]
- #846 — docs: truth-pass for 1.0 launch — version labels, ADR amendments, surface sync, provenance [feature=roadmap-governance-hygiene]
- #850 — fix(selflearn): injection-history-aware suppression breaks duplicate dominance (93% dup root-cause) [feature=injection-history-suppression]

## Remaining Launch Blockers
- LB-3 (operator): PyPI publish go (artifacts verified + staged) -> pip install from PyPI check -> git tag v1.0.0 + GitHub release. Optional: central-install refresh + TestPyPI dry-run first. LB-4..LB-7 resolved 2026-06-10 (#833/#835/#836/#839/#840, see launch-sweep-blockers).
