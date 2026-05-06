# T0 Kickoff Prompt — paste this as your first message after /clear

---

@t0-orchestrator

You are T0 in `/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt`. The previous session designed 17 phases of work and persisted everything to disk. **Read these files now in this exact order before doing anything else:**

1. `.vnx-data/strategy/ROADMAP.md` — full canonical roadmap, 17 phases, 99 sub-PRs
2. `.vnx-data/strategy/roadmap.yaml` — machine-readable wave list with deps + statuses + open OD decisions
3. `.vnx-data/strategy/backlog.yaml` — universal queue of unscheduled work (16 items)
4. `.vnx-data/state/PROJECT_STATE_DESIGN.md` — Layer 1 + Layer 2 design rationale
5. `.vnx-data/state/t0_state.json` — runtime state (auto-built by SessionStart hook)
6. `roadmap/features/phase-00-operator-ux/FEATURE_PLAN.md` — the FIRST feature to execute

After reading those 6 files, you have full strategic + tactical context. Confirm by stating in 3 lines: "current phase, in-flight PRs, recommended first action."

---

## Mission for this session

**Execute Phase 0 (operator UX quick wins) autonomously to completion, then advance to Phase 1.**

Phase 0 is 5 waves (W-UX-1 through W-UX-5). All have full FEATURE_PLAN.md specs. No operator decisions block Phase 0. Total ~340 LOC of source work + tests.

Sequence:
1. **W-UX-1** is essentially DONE — strategic state folder bootstrap landed via PR #398 (currently open, awaiting gemini-gate). Once that merges, W-UX-1 is closed automatically. If #398 hasn't merged yet, retry the gemini gate (gemini quota should have recovered today). Codex quota also recovered as of 2026-05-05; you can replay the ~75 codex re-audit OIs as a batch in parallel.
2. **W-UX-2** (current_state.md projector, ~150 LOC, Sonnet T2) — dispatch via `init_feature_batch`. Plan already at `.vnx-data/strategy/dispatch_plans/W-UX-2-current-state-projector.md`.
3. **W-UX-3** (vnx status CLI, ~80 LOC, Sonnet T3) — depends on W-UX-2.
4. **W-UX-4** (GC retention in build_t0_state.py, ~30 LOC, Sonnet) — independent of W-UX-2/3, can run parallel.
5. **W-UX-5** (vnx init bootstrap, ~80 LOC, Opus T1) — depends on W-UX-1 + W-UX-2.

After all 5 land: Phase 0 complete. Auto-advance to Phase 1 (open work cleanup — merge any straggler PRs, replay codex audits batched).

---

## Execution rules (operator's standing instructions)

**Gate strategy:**
- Per-PR: gemini_review only (every PR; cheap, available)
- Per-FEATURE end (last PR before merge wave): codex_gate (saves codex usage)
- High-risk features (per FEATURE_PLAN.md review_stack field): add claude_github_optional

**Model strategy:**
- Each PR's `Requires-Model` field in FEATURE_PLAN.md is authoritative. Default Sonnet; Opus only where the FEATURE_PLAN explicitly says so (Opus assigned for: foundational schemas, security-sensitive code, concurrency-sensitive code, anything with high blast radius).
- Haiku not used in Phase 0.

**Dispatch flow:**
- Use `python3 scripts/pr_queue_manager.py init_feature_batch --feature-plan roadmap/features/phase-00-operator-ux/FEATURE_PLAN.md` to materialize all 5 waves as dispatches in staging + auto-create open items per Quality Gate.
- Promote dispatches one at a time per the dependency graph.
- Each worker uses `subprocess_dispatch.py --terminal-id T1/T2/T3 --model <sonnet|opus> --role backend-developer --instruction "$(cat <wave>/dispatch.json instruction)"`.
- After each worker completes, retrieve receipt → run gate (gemini per PR, codex if feature-end) → merge → close OIs → next wave.

**Stale-lease check before first dispatch:**
```
for T in T1 T2 T3; do
  python3 scripts/runtime_core_cli.py check-terminal --terminal $T --dispatch-id <new-id>
done
```
If any shows `lease_expired_not_cleaned`, release before promoting.

**Memory:**
- Operator preferences in `~/.claude/projects/.../memory/feedback_*.md` apply (auto-loaded). Standing rules: never raw tmux send-keys, mandatory triple-gate where specified, dispatch via pending/, no manager block for staged dispatches.

**Open operator decisions (Phase 9+ only — not blocking now):**
- OD-1, OD-2, OD-4, OD-5, OD-6 in roadmap.yaml `operator_decisions[]`. Defaults are pre-recommended in PRD §10. They're only blocking from Phase 9 (W10 cap-tokens) onwards. Phase 0 through 8 don't need answers.

**Fail-forward policy:**
- If a worker fails, retry once with same instructions. If failure persists, dispatch a fix-forward worker with explicit error context. Do NOT revert merged PRs unless operator explicitly requests.
- If gemini gate fails transiently (exit_nonzero / stall), retry up to 3× with quota-recovery delays.
- If a PR's gemini gate fails on a finding that's pre-existing (not introduced by the PR), file a follow-up OI and merge anyway with the gate-skip rationale documented in the PR comment (per the precedent from the 2026-05-01 sprint).

---

## Mission acceptance (when to stop this session)

Stop when ANY of:
1. Phase 0 fully complete (5 waves merged + Phase 1 cleanup advanced)
2. Operator interrupts
3. A blocker emerges that requires an OD answer that wasn't pre-recommended
4. Gate quotas are exhausted on multiple providers simultaneously and retries don't help

End-of-session protocol:
- Write a session summary to `.vnx-data/state/SESSION_<date>.md`
- Update `.vnx-data/strategy/decisions.ndjson` with any decisions made (will be properly automated in W-state-2; manual NDJSON entries OK for now)
- Verify `t0_state.json` is fresh

---

## Where to find context if anything is unclear

- Full PRD: `claudedocs/PRD-VNX-UH-001-universal-headless-orchestration-harness.md` (1145 lines, internal — gitignored, only on operator's laptop)
- Multi-orchestrator research: `claudedocs/2026-05-01-multi-orchestrator-research.md`
- Universal harness research: `claudedocs/2026-05-01-universal-harness-research.md`
- Single-system migration plan: `claudedocs/2026-04-30-single-vnx-migration-plan.md`
- ADR-001 No Redis: `docs/governance/decisions/ADR-001-no-external-redis.md` (in PR #395, may not be on main yet)
- ADR-002 F43 packaging: `docs/governance/decisions/ADR-002-f43-context-rotation-packaging.md` (in PR #395)
- Yesterday's sprint context: `git log --since="2026-04-28" --oneline | head -50` (~30 PRs merged 2026-05-01)

---

## Begin

Read the 6 files. Confirm context. Then start with W-UX-2 (since W-UX-1 is in PR #398 already). Be autonomous. Be safe (cap-tokens land in Phase 9; until then, trust is operator-monitored). Be fast (don't over-plan; the FEATURE_PLAN.md's are the plan).

Operator may not respond between waves. That's fine — keep going through Phase 0. Schedule wakeups for ~15-min checkpoints when workers are in flight.
