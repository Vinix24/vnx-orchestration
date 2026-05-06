# W-UX-3 — vnx status CLI dashboard (Sonnet 4.6)

**Wave-id:** w-ux-3 (Phase 0)
**Estimated LOC:** ~80
**Branch:** `feat/vnx-status-cli`
**Risk:** low
**Depends on:** w-ux-2 (current_state.md projector exists)

## Goal

A single CLI command `vnx status` that gives the operator a terminal-friendly summary of the project: strategic state from `current_state.md` + live runtime data (open PRs, idle terminals, blocking OIs). One command, one screen, <2 seconds.

## Hard constraints

1. **One screen.** Output fits in 50 rows × 120 cols on a terminal.
2. **Fast.** <2 seconds total runtime including JSON loads.
3. **Read-only.** No state mutation; just rendering.
4. **Reuses existing readers.** `t0_state.json` already has runtime data; `current_state.md` already has strategic data. CLI is composition only.
5. **Color but degradable.** ANSI colors when stdout is a TTY; plain text when piped.

## Workflow

1. Worktree:
   ```
   cd /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt
   git worktree add -b feat/vnx-status-cli ../vnx-w-ux-3
   cd ../vnx-w-ux-3
   ```

2. Implement `scripts/commands/status.sh` (entry point — bash wrapper, ~20 LOC) that calls `scripts/lib/vnx_status_renderer.py` (~60 LOC).

3. `vnx_status_renderer.py` reads:
   - `.vnx-data/state/t0_state.json` (runtime: terminals, leases, queues)
   - `.vnx-data/strategy/current_state.md` (strategic: waves, decisions)
   - `gh pr list --state open --json number,title --limit 10` (live PR list)

4. Render to stdout in this layout:
   ```
   ━━━ VNX STATUS ━━━ 2026-05-02 12:34:56
   Project: vnx-roadmap-autopilot · branch: main · 0 uncommitted

   ▸ Active phase: 0 (Operator UX quick wins)
   ▸ Next wave:    w-ux-3 — vnx status CLI dashboard (planned, no blockers)
   ▸ Decisions:    1 closed (OD-3), 5 open (OD-1, 2, 4, 5, 6)

   ━━━ OPEN PRS ━━━
     #395  chore: ADRs + threshold cleanup            (gemini-gate stalled)
     #396  fix(append_receipt): UR-001                (gemini-gate retry)
     #232  fix(pane-manager) cross-project leak       (open since 2026-04-20)

   ━━━ TERMINALS ━━━
     T1  idle    no lease    track A
     T2  idle    no lease    track B
     T3  idle    no lease    track C

   ━━━ BLOCKERS ━━━
     OI-1294  Function exceeds blocking threshold (76 lines, max 70)

   ━━━ RECENT (last 24h) ━━━
     30 PRs merged (consolidation sprint)
     66 OIs closed
     PRD v1.3 finalised (claudedocs/PRD-VNX-UH-001-...)
     Strategy folder bootstrapped (.vnx-data/strategy/)

   ▸ Recommended: start W-UX-2 (current_state.md projector) — no blockers
   ━━━━━━━━━━━━━━━━━━━━━━━━
   ```

5. Add subcommand `vnx status --json` for machine-readable output (just dump combined dict).

6. Wire into `vnx` CLI dispatch (in `scripts/vnx.sh` or wherever the CLI router is):
   ```bash
   case "$1" in
     status) shift; exec bash "$VNX_HOME/scripts/commands/status.sh" "$@" ;;
     ...
   esac
   ```

7. Tests in `tests/test_vnx_status_cli.py` (~30 LOC):
   - Mock t0_state.json + current_state.md → assert output contains expected sections
   - `--json` flag produces valid JSON
   - Runtime <2s on a clean repo

8. Commit + push + PR:
   ```
   git add -A
   git commit -m "feat(cli): vnx status command (W-UX-3)" -m "Single-screen dashboard combining strategic + runtime state. <2s, color-aware, --json mode for scripts." -m "Dispatch-ID: 20260502-w-ux-3-vnx-status-cli"
   git push -u origin feat/vnx-status-cli
   gh pr create --base main --title "feat(cli): vnx status command (W-UX-3)" --body "Phase 0 quick-win. Operator's one-command project dashboard."
   ```

## Reject if

- Runtime >5s on the operator's laptop.
- Output overflows 50 rows × 120 cols on a normal terminal.
- Doesn't degrade gracefully when piped (no ANSI escape codes in non-TTY).
- Any state mutation happens (this is read-only).
