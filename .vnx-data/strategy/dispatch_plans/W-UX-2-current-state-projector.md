# W-UX-2 — current_state.md auto-projector + retire vestigial state files (Sonnet 4.6)

**Wave-id:** w-ux-2 (Phase 0)
**Estimated LOC:** ~150
**Branch:** `feat/strategy-current-state-projector`
**Risk:** low
**Depends on:** w-ux-1 (strategy/ folder bootstrap, done)

## Goal

Generate `.vnx-data/strategy/current_state.md` automatically from underlying truth sources (roadmap.yaml + open PRs + receipts + open items + recent decisions) so the operator and T0 always have a one-pager that reflects reality without manual upkeep.

Retire 4 vestigial state files (`.vnx-data/state/STATE.md`, `PROJECT_STATUS.md`, `HANDOVER_2026-04-28.md`, `HANDOVER_2026-04-28-evening.md`) by archiving them to `.vnx-data/state/_archive/` with deprecation notice.

## Hard constraints

1. **One file output.** `.vnx-data/strategy/current_state.md` — markdown, ≤200 lines, scannable in <30 seconds.
2. **Idempotent.** Running the projector twice in a row produces identical output (no timestamps that change).
3. **Fast.** <2 seconds end-to-end on the operator's laptop.
4. **No new dependencies.** stdlib + already-imported deps (PyYAML for roadmap.yaml).
5. **No code changes outside the projector + hook wiring + archive moves.**

## Workflow

1. Create new worktree:
   ```
   cd /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt
   git worktree add -b feat/strategy-current-state-projector ../vnx-w-ux-2
   cd ../vnx-w-ux-2
   ```

2. Implement `scripts/build_current_state.py` (~100 LOC). Reads:
   - `.vnx-data/strategy/roadmap.yaml` — phases, waves, statuses, OD blockers
   - `gh pr list --state open --json number,title,branch,createdAt --limit 20`
   - `.vnx-data/state/t0_receipts.ndjson` (last 10 entries)
   - `.vnx-data/state/open_items.json` — top 5 blockers + warn-level count
   - `.vnx-data/strategy/decisions.ndjson` if exists (last 5 decisions)

   Writes `.vnx-data/strategy/current_state.md` with sections:
   - **Where we are** (1 paragraph)
   - **Open work** (table: PR # / title / status / waiting on)
   - **Active waves** (table from roadmap.yaml: wave_id / status / blocked_on)
   - **Recent decisions** (last 5 from decisions.ndjson)
   - **Open OD count + IDs**
   - **Recommended next move** (computed: first wave where status==planned AND all depends_on are completed AND no blocked_on)
   - **Last updated** timestamp (only one timestamp in whole doc; rest derived)

3. Wire to SessionEnd hook in `.claude/settings.json`:
   ```json
   "hooks": {
     "SessionEnd": [
       {"command": "python3 $VNX_HOME/scripts/build_current_state.py"}
     ]
   }
   ```

4. Wire to post-merge hook in `scripts/lib/receipt_processor/rp_dispatch.sh` (after lease release):
   ```bash
   python3 "$VNX_HOME/scripts/build_current_state.py" 2>/dev/null || true
   ```

5. Archive vestigial files:
   ```bash
   mkdir -p .vnx-data/state/_archive
   git mv .vnx-data/state/STATE.md .vnx-data/state/_archive/STATE.md.deprecated
   git mv .vnx-data/state/PROJECT_STATUS.md .vnx-data/state/_archive/PROJECT_STATUS.md.deprecated
   git mv .vnx-data/state/HANDOVER_2026-04-28.md .vnx-data/state/_archive/
   git mv .vnx-data/state/HANDOVER_2026-04-28-evening.md .vnx-data/state/_archive/
   ```
   (Note: `.vnx-data/` is gitignored so `git mv` won't actually move tracked files. Use `mv` directly for these.)

6. Add deprecation notice file `.vnx-data/state/_archive/README.md`:
   ```markdown
   # Archived state files

   These files were the canonical strategic-state surface before W-UX-2 (2026-05-02).
   They have been replaced by `.vnx-data/strategy/current_state.md` (auto-projected
   from roadmap.yaml + receipts + open PRs + decisions).

   Do not write to these files. The `current_state.md` projector ignores them.
   ```

7. Smoke test:
   ```bash
   python3 scripts/build_current_state.py
   wc -l .vnx-data/strategy/current_state.md
   # Expect: ~150-200 lines
   diff <(python3 scripts/build_current_state.py && cat .vnx-data/strategy/current_state.md) \
        <(python3 scripts/build_current_state.py && cat .vnx-data/strategy/current_state.md)
   # Expect: empty diff (idempotent)
   ```

8. Tests in `tests/test_build_current_state.py` (~50 LOC):
   - Empty roadmap.yaml → projector outputs sensible "no plan" message
   - Roadmap with all completed waves → projector reports "all done"
   - Roadmap with one in_progress wave + blocking OD → projector surfaces it in "Recommended next move"
   - Idempotence test (run twice, diff is empty)

9. Commit + push + PR:
   ```
   git add -A
   git commit -m "feat(strategy): current_state.md auto-projector (W-UX-2)" -m "Auto-generates the operator-facing one-pager from roadmap.yaml + receipts + open PRs + decisions. Retires vestigial STATE.md/PROJECT_STATUS.md/HANDOVER_*.md (archived). SessionEnd + post-merge hooks." -m "Dispatch-ID: 20260502-w-ux-2-current-state-projector"
   git push -u origin feat/strategy-current-state-projector
   gh pr create --base main --title "feat(strategy): current_state.md auto-projector (W-UX-2)" --body "Phase 0 quick-win. Retires 4 vestigial state files. Idempotent. <2s runtime."
   ```

## Reject if

- Output not idempotent (running twice yields different content beyond timestamp).
- Runtime >5s on operator's laptop.
- Hook wiring breaks SessionEnd or post-merge in a way that affects unrelated paths.
- Any of the 4 archived files is referenced by tracked code (do `grep -rn STATE.md scripts/ docs/` before archiving).
