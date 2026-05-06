# T0 Kickoff Prompt — paste this as your first message after /clear

---

@t0-orchestrator

You are T0 in `/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt`. The previous session (2026-05-06) completed Phase 0 (operator UX quick wins, 4/5 waves), Phase 1 cleanup (PRs #395 superseded by #402, #232 closed, #396 merged via codex_gate), and ran a full codex re-audit batch on 51 prior PRs (8 P1-finding fix-forwards filed, 43 verified clean). All findings are in open items + the file-of-record below.

**Read these files now in this exact order before doing anything else:**

1. `.vnx-data/state/SESSION_2026-05-06.md` — full session-end summary with all merges, closes, and decisions
2. `roadmap/features/phase-01-5-codex-fixforward-sprint/FEATURE_PLAN.md` — the FIRST feature to execute (5 PRs, ~500 LOC, ~2-3 days)
3. `.vnx-data/strategy/ROADMAP.md` — full 17-phase canonical roadmap (Phase 0 done, Phase 1 done, Phase 1.5 NEW, Phase 2-16 still planned)
4. `.vnx-data/strategy/roadmap.yaml` — machine-readable wave list with deps + statuses + open OD decisions
5. `.vnx-data/strategy/backlog.yaml` — universal queue of unscheduled work
6. `.vnx-data/state/t0_state.json` — runtime state (auto-built by SessionStart hook)

After reading, confirm in 3 lines: "current phase, in-flight PRs, recommended first action."

---

## Mission for this session

**Execute Phase 1.5 (codex fix-forward sprint) autonomously to completion, then advance to Phase 2 (strategic state foundation).**

Phase 1.5 is 5 independent PRs (PR-1 through PR-5), all sequenced through T1 (single backend-developer slot). All have full FEATURE_PLAN.md specs with line-anchored findings from codex re-audit. ~500 LOC of source work + tests.

**Why Phase 1.5 first**: The 8 fix-forward findings (closure_verifier, success_patterns multi-tenant, dispatch_project_guard, drain/dead_letter, role-alias) sit in foundational modules that Phase 2 (state), Phase 3 (W7 drainer), Phase 6 (single-system migration), and Phase 11 (sub-orchestrators) all build on top of. Skipping Phase 1.5 → chained features compound on broken foundation. ~500 LOC fix now is much cheaper than debugging in chained waves later.

Sequence:
1. **PR-1** (closure_verifier evidence-contract) → T1, sonnet, ~50-100 LOC. Closes OI-1317, OI-1322. Blocks Phase 2.
2. **PR-2** (success_patterns multi-tenant) → T1, **opus** (high blast radius), ~100-150 LOC. Closes OI-1315, OI-1321. Blocks Phase 6.
3. **PR-3** (dispatch_project_guard) → T1, sonnet, ~50-80 LOC. Closes OI-1316. Blocks Phase 6 + Phase 11.
4. **PR-4** (drain + dead_letter receipt classification) → T1, sonnet, ~80-120 LOC. Closes OI-1319, OI-1323. Blocks Phase 3.
5. **PR-5** (role-alias gather_intelligence) → T1, sonnet, ~30-50 LOC. Closes OI-1320. Independent.

After all 5 land: Phase 1.5 complete. Auto-advance to Phase 2 (W-state-1 → W-state-5).

Promote with: `python3 scripts/pr_queue_manager.py init-feature roadmap/features/phase-01-5-codex-fixforward-sprint/FEATURE_PLAN.md`. The plan uses `## PR-N:` headings so init-feature works directly (no workaround needed; OI-1311 still blocks W-UX-N format plans).

---

## Execution rules (operator's standing instructions)

**Gate strategy** (unchanged from prior session — works empirically):
- Per-PR: gemini_review (cheap, available)
- Per-PR for high-risk (PR-2, PR-4): also `codex_gate` (catches what gemini misses — proven this session)
- High-risk features (per FEATURE_PLAN review_stack field): add claude_github_optional

**Codex_gate path that works** (for when gemini stalls):
```
codex exec review --commit <sha> --json -o /tmp/codex_pr<N>.txt
# parse [P0..P3] tags from the agent_message text. P0/P1 = blocking, P2/P3 = advisory.
# Then: python3 scripts/review_gate_manager.py record-result --gate codex_gate ...
```
Wrapper at `/tmp/codex_replay_audits.py` is the reference — adapt for in-flight PRs by passing branch-tip OID instead of merge-commit.

**Model strategy:**
- Each PR's `Requires-Model` field in FEATURE_PLAN.md is authoritative.
- PR-2 explicitly opus (multi-tenant data integrity). Others sonnet.
- Haiku not used in Phase 1.5.

**Dispatch flow:**
- Worker: `python3 scripts/lib/subprocess_dispatch.py --terminal-id T1 --model <sonnet|opus> --role backend-developer --instruction "$(cat /tmp/<wave>_instruction.md)"`
- After each worker completes: retrieve receipt → run gemini gate → if PR-2 or PR-4, also run codex_gate via wrapper → merge → close OIs → next wave

**Stale-lease check before first dispatch:**
```
for T in T1 T2 T3; do
  python3 scripts/runtime_core_cli.py check-terminal --terminal $T --dispatch-id <new-id>
done
```

**Memory:**
- Operator preferences in `~/.claude/projects/.../memory/feedback_*.md` apply (auto-loaded). Standing rules: never raw tmux send-keys, dispatch via pending/, no manager block for staged dispatches.

**Standing operator policies (relevant for Phase 1.5):**
- A1 codex availability: codex CLI is currently working (last verified 2026-05-06 batch)
- B1 gemini stall: ≥180s + 0 partial output → infra issue. Switch to codex_gate path (proven 2026-05-06 PR #396)
- B3 PR-introduced finding: fix in same PR, retry gates ONCE, defer-with-OI if still dirty after 1 retry
- Hard CI red is NEVER acceptable for merge — investigate root cause, fix workflow or code
- Force-push to PR branches is harness-blocked (correctly). Use cherry-pick to a fresh branch + new PR if rebase has conflicts (proven 2026-05-06 PR #402)

---

## Mission acceptance (when to stop this session)

Stop when ANY of:
1. Phase 1.5 fully complete (5 PRs merged + 8 fix-forward OIs closed)
2. Phase 2 W-state-1 dispatched (next-feature handover ready)
3. Operator interrupts
4. A blocker emerges that requires an OD answer that wasn't pre-recommended
5. Three consecutive PRs blocked by same recurring CI/gate issue

End-of-session protocol:
- Write a session summary to `.vnx-data/state/SESSION_<date>.md`
- Update `.vnx-data/strategy/decisions.ndjson` if any architectural decisions made
- Verify `t0_state.json` is fresh
- Update this KICKOFF.md with the next session's mission

---

## Open Items snapshot at session start (2026-05-06 end)

- **67 open items** (was 138 before codex re-audit cleanup)
- **1 blocker**: OI-1294 (Function exceeds 70 lines threshold, PR-254). Pre-existing, low priority.
- **8 codex fix-forward OIs** (Phase 1.5 closes these): 1315 (#352), 1316 (#362), 1317 (#321), 1319 (#316), 1320 (#317), 1321 (#311), 1322 (#300), 1323 (#320)
- **W-UX-5 blocker**: OI-1313 (architect role + templates/ scope, .vnx/worker_permissions.yaml is system-immutable per CLAUDE.md → operator decision needed)
- **OI-1311**: init-feature parser only accepts `## PR-N:` (Phase 1.5 plan uses PR-N so unaffected)
- **OI-1312**: sh_function_lines brittle counting (low, fix in Phase 1.5 PR-1 if cheap)

---

## Where to find context if anything is unclear

- Full session-end summary: `.vnx-data/state/SESSION_2026-05-06.md`
- Codex re-audit raw outputs: `/tmp/codex_replay_20260506-100620/` (sample) + `/tmp/codex_replay_20260506-103403/` (batch). May not survive reboot — pull contents into `claudedocs/` next session if you need the raw text.
- Codex replay wrapper: `/tmp/codex_replay_audits.py` (reusable for any future merge-commit re-audit batch)
- PR #396 codex_gate report: `.vnx-data/unified_reports/headless/20260506-pr396-codex-gate-report.md` (reference for codex_gate evidence-recording shape)
- PRD: `claudedocs/PRD-VNX-UH-001-universal-headless-orchestration-harness.md` (1145 lines, internal — gitignored)
- Master roadmap: `.vnx-data/strategy/ROADMAP.md`

---

## Recently merged PRs (for context if grep needed)

- `c7c0330` — feat(strategy): current_state.md auto-projector + retire vestigial state (W-UX-2) (#401)
- `4083d63` — feat(cli): vnx status dashboard subcommand (W-UX-3) (#403)
- `ca4327d` — fix(t0-state): GC retention for t0_detail JSON snapshots (W-UX-4) (#400)
- `9924d8b` — chore: ADRs (No-Redis + F43 packaging) + threshold-OI cleanup (cherry-pick of #395) (#402)
- `988d19f` — fix(append_receipt): remove dead duplicate _maybe_reroute_ghost_receipt (#396)

---

## Begin

Read the 6 files. Confirm context in 3 lines. Then start with PR-1 (closure_verifier evidence-contract restoration). Be autonomous. Be safe. Be fast (don't over-plan; the FEATURE_PLAN.md's per-PR Scope sections ARE the plan).

Operator may not respond between PRs. That's fine — keep going through Phase 1.5. Schedule wakeups for ~15-min checkpoints when workers are in flight.
