<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_pr_queue.py -->

# PR Queue — VNX 1.0 launch (DERIVED VIEW)

> Authoritative source: `ROADMAP.yaml` (`launch_state` + `features[].pr_queue`).
> Live PR state: `gh pr list`. Do not hand-edit this file.

## Progress Overview
Launch status: **pre-launch** (version 1.0.0)
Last verified: 2026-06-01 against origin/main@c0506771
Merged launch PRs: 23 | Queued: 0

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
- #780 — feat(intelligence): install nightly intelligence pipeline cron (GAP 4 — reactivate self-learning loop) [feature=gap4-self-learning-reactivation]
- #781 — fix(tmux): bounded guarded-retry + adaptive settle for submit reliability (GAP 6) [feature=gap6-submit-reliability]
- #782 — feat(doctor): structural-doctor for tracks-layer activation (dry-run default, backup-on-apply) [feature=tracks-layer-activation]

## Remaining Launch Blockers
- LB-3 (operator): rebuild wheel from final main + zero-hit security grep on that exact artifact + fresh-venv install acceptance + central-install refresh + PyPI publish go + tag+release
