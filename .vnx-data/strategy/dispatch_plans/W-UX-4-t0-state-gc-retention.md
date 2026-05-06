# W-UX-4 — GC retention policy in build_t0_state.py (Sonnet 4.6)

**Wave-id:** w-ux-4 (Phase 0)
**Estimated LOC:** ~30
**Branch:** `fix/t0-state-gc-retention`
**Risk:** low
**Depends on:** w-ux-1 (parallel-OK with w-ux-2 and w-ux-3)

## Goal

Prune historical noise from `t0_state.json`. Today `feature_state.dispatches` has 76 entries (most weeks old) and `feature_state.pr_status` has 47 entries (most already-merged). Operator's mental model of "what's open" is muddied. Retention policy: keep only items still relevant.

## Hard constraints

1. **Configurable cutoff.** Default 14 days for dispatches, 30 days for PRs. Operator can override via env var.
2. **Preserve in-flight items.** A dispatch in any open PR or open mission stays regardless of age.
3. **Operator-pinned items always survive.** Add a `pinned_dispatches.json` config (initially empty) that `build_t0_state.py` reads to preserve named items.
4. **No data loss.** GC removes from `t0_state.json` projection only; `dispatch_register.ndjson` is the truth source and stays append-only.
5. **Backward-compat.** Schema unchanged; just shorter lists.

## Workflow

1. Worktree:
   ```
   cd /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt
   git worktree add -b fix/t0-state-gc-retention ../vnx-w-ux-4
   cd ../vnx-w-ux-4
   ```

2. Edit `scripts/build_t0_state.py` — add a `_gc_feature_state(feature_state, config)` function (~25 LOC):
   ```python
   def _gc_feature_state(feature_state: dict, config: dict) -> dict:
       """Prune historical noise from feature_state.dispatches + pr_status.

       Keep:
       - Items younger than retention window
       - Items referenced by any open PR
       - Items in any open mission
       - Items in pinned_dispatches.json
       """
       now = datetime.now(timezone.utc)
       dispatch_window = timedelta(days=int(os.environ.get('VNX_GC_DISPATCH_DAYS', '14')))
       pr_window = timedelta(days=int(os.environ.get('VNX_GC_PR_DAYS', '30')))

       open_pr_dispatch_refs = _get_open_pr_dispatch_refs()
       pinned = _load_pinned_dispatches()

       def keep_dispatch(dispatch_id: str, info: dict) -> bool:
           if dispatch_id in pinned: return True
           if dispatch_id in open_pr_dispatch_refs: return True
           ts_str = info.get('last_event_at') or info.get('first_seen_at')
           if not ts_str: return True  # safety: keep on missing timestamp
           ts = _parse_iso(ts_str)
           return (now - ts) < dispatch_window

       feature_state['dispatches'] = {
           did: info for did, info in feature_state.get('dispatches', {}).items()
           if keep_dispatch(did, info)
       }

       def keep_pr(pr_num: str, info: dict) -> bool:
           if info.get('state') == 'OPEN': return True
           merged_at = info.get('merged_at')
           if not merged_at: return True
           ts = _parse_iso(merged_at)
           return (now - ts) < pr_window

       feature_state['pr_status'] = {
           pr: info for pr, info in feature_state.get('pr_status', {}).items()
           if keep_pr(pr, info)
       }

       return feature_state
   ```

3. Wire into the existing `build_t0_state` flow — call `_gc_feature_state` after the existing dispatch+pr_status assembly, before write.

4. Add `_get_open_pr_dispatch_refs()` — reads `.vnx-data/state/dispatch_register.ndjson` for `pr_opened` events whose `pr_number` is still in `gh pr list --state open` (~15 LOC, reuse existing register reader).

5. Add `_load_pinned_dispatches()` — reads `.vnx-data/state/pinned_dispatches.json` (creates empty `[]` if missing) (~10 LOC).

6. Tests in `tests/test_build_t0_state_gc.py` (~30 LOC):
   - Old dispatch (15 days), no open PR ref, not pinned → pruned
   - Old dispatch but in open PR → kept
   - Old dispatch but in pinned list → kept
   - Recent dispatch (5 days) → kept
   - Old PR (35 days, merged) → pruned
   - Old PR but state=OPEN → kept

7. Verify on real data:
   ```bash
   python3 scripts/build_t0_state.py
   python3 -c "
   import json
   d = json.load(open('.vnx-data/state/t0_state.json'))
   print(f'dispatches: {len(d[\"feature_state\"][\"dispatches\"])} (was 76)')
   print(f'pr_status:  {len(d[\"feature_state\"][\"pr_status\"])} (was 47)')
   "
   # Expect: significant reductions to dozens, not 70+
   ```

8. Commit + push + PR:
   ```
   git add -A
   git commit -m "fix(t0-state): GC retention policy for feature_state (W-UX-4)" -m "Prune dispatches >14 days and merged PRs >30 days from t0_state.json projection unless still in an open PR or operator-pinned. dispatch_register.ndjson stays untouched (truth source)." -m "Dispatch-ID: 20260502-w-ux-4-t0-state-gc-retention"
   git push -u origin fix/t0-state-gc-retention
   gh pr create --base main --title "fix(t0-state): GC retention policy for feature_state (W-UX-4)" --body "Phase 0 quick-win. Operator mental model now matches reality."
   ```

## Reject if

- Any non-historical dispatch is pruned (regression on safety logic).
- `dispatch_register.ndjson` is touched (must stay append-only truth source).
- Build runtime increases >20% (GC should be O(N), not require re-reading the register fully).
- Tests missing for the "kept because in open PR" branch.
