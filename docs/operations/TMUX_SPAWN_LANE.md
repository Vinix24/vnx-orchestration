# Tmux-Spawn Dispatch Lane

## Overview

`scripts/lib/tmux_interactive_dispatch.py` is the leaseless ephemeral dispatch lane VNX uses as default for parallel and independent feature work. Each dispatch spawns a fresh unique tmux session, drives an interactive `claude` worker on the subscription (the 15-juni billing escape), waits for the receipt, and tears down. No reuse, no warm-open, no terminal pin.

This lane runs Claude workers on the **subscription** (interactive `claude`, never `claude -p`) — that keeps the dispatcher off API-credit billing. It complements `subprocess_dispatch.py` (Wave 5 smart-context + terminal pinning), which runs headless `claude -p` and, after the June 15, 2026 billing change, bills API credits — so the subprocess lane is opt-in and blocked by default (`claude-headless` constraint; `VNX_OVERRIDE_CLAUDE_HEADLESS=1` to open it).

## When to use which lane

| Task | Lane | Why |
|---|---|---|
| Parallel / independent feature work | **tmux_interactive_dispatch.py** (default) | Leaseless ephemeral, isolated worktree per dispatch, subscription-safe |
| Terminal-pinned single-worker PR | subprocess_dispatch.py | Wave 5 +30pp quality lift, lease management, triple-gate contract_hash binding |
| Long-running worker (>30 min expected) | subprocess_dispatch.py | tmux-spawn has receipt-deadline failures on long workers |
| Burn-in measurement work | subprocess_dispatch.py | Wave 1 shadow logging pinned to terminal |
| PR review gate (codex_gate, gemini_review) | scripts/review_gate_manager.py | Creates canonical review_gates request/result artifacts |
| Utility / ad-hoc claude-as-utility | direct Bash `claude --print` | No PR outcome, no Wave 1/5 contribution |

## Canonical command

```bash
# VNX paths (VNX_STATE_DIR / VNX_DATA_DIR / VNX_DISPATCH_DIR) resolve centrally
# via the vnx runtime — do NOT hardcode .vnx-data/ literals here. A repo-local
# pin forks state from the central store (~/.vnx-data/<project>) = split-brain.

python3 scripts/lib/tmux_interactive_dispatch.py \
  --dispatch-id "$(date +%Y%m%d-%H%M%S)-<slug>" \
  --role backend-developer \
  --model sonnet \
  --dispatch-paths "<comma-separated-paths>" \
  --from-staging-id "<dispatch-id>" \
  --deadline-seconds 2400 \
  --instruction "<inline instruction text>"
```

Required flags: `--dispatch-id`, `--instruction`. Defaults: `--isolated-worktree` on, `--model sonnet`, `--base-ref origin/main`. Staging gate enforced via `--from-staging-id` (per ADR-006).

`--model sonnet` above is illustrative of the claude-lane CLI shape only. Since worker-provider-kimi-flip (2026-07-23), T1/T2/T3 are pinned to `kimi-k3` (`workers-kimi-pinned` constraint, loaded unconditionally by `dispatch_cli._load_model_pins_from_yaml()`), and that pin applies to the `provider` field at staging time, not this tmux-spawn CLI — `tmux_interactive_dispatch.py`/`--model sonnet` only runs for an explicit `provider=claude` build-worker override, and the door's registry check now correctly REJECTS a claude-lane T1/T2/T3 dispatch (the kimi-k3 pin is not a valid Claude model) rather than silently resolving it to `claude-sonnet-5`. T0 stays on Opus.

The detached spawn also defaults to blanket `--dangerously-skip-permissions` (#1016) — the isolated per-dispatch worktree already bounds blast radius, so the scoped posture is opt-in via `VNX_WORKER_SCOPED=1` rather than the default. See `docs/operations/WORKER_PERMISSIONS.md`.

## Concurrency

The lane's account-level serial lock (`scripts/lib/dispatch_serialization.py`) defaults to `N=1` (fully serial — the historically subscription-safe behavior for a shared Claude account). Set `VNX_TMUX_MAX_CONCURRENT=<N>` to run up to `N` tmux-spawn workers concurrently; this is an explicit operator opt-in, not a default the lane creeps towards, and it trades serialization safety for throughput against the account's real session cap. A stale lock (any slot) is cleared with `vnx dispatch --force-release-lock claude-tmux`.

## Known reliability gaps

- **Receipt-deadline failures on long workers.** If a worker runs longer than the `--deadline-seconds` budget, the lane reaps the session with `worktree_state=clean` and no commits land. Mitigation: budget generously for unknown work, or use `subprocess_dispatch.py` for work expected to exceed 30 min. Tracked in memory `tmux-spawn-regression-dogfood-gap`.
- **`.claude/skills/` meta-path edits silently no-op.** Workers in this lane (and the subprocess lane) fail silently when asked to edit files inside `.claude/skills/` — the skill the worker just loaded. Observed 2026-06-03 (OI-188). Edit those files manually from T0 or the operator until worker permission handling is investigated.

## Implementation

- Entry point: `scripts/lib/tmux_interactive_dispatch.py`
- Isolated worktree creation: `scripts/lib/tmux_worktree.py`
- Receipt flow: writes report under `$VNX_DATA_DIR/unified_reports/<dispatch-id>.md` → receipt processor → `t0_receipts.ndjson`
- Billing safety: only `subprocess.Popen(["tmux", ...])` and an interactive `claude` binary are invoked; no Anthropic SDK import anywhere in the module

## Related

- `docs/operations/SUBPROCESS_ADAPTER_FEATURE_FLAG.md` — the headless `claude -p` adapter for terminal-pinned workers
- `docs/operations/WORKER_PERMISSIONS.md` — the `VNX_WORKER_SCOPED` opt-in, `.vnx/worker_permissions.yaml`, and the `working_tree_only` fail-closed rule
- `docs/core/DISPATCH_RULES.md` §6 — the N-slot serial lock (`VNX_TMUX_MAX_CONCURRENT`) and `--force-release-lock`
- t0-orchestrator skill §9.2 — full dispatch-routing decision rule (canonical T0 reference)
- ADR-006 — staging→pending→promote gate enforcement
