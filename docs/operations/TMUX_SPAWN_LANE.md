# Tmux-Spawn Dispatch Lane

## Overview

`scripts/lib/tmux_interactive_dispatch.py` is the leaseless ephemeral dispatch lane VNX uses as default for parallel and independent feature work. Each dispatch spawns a fresh unique tmux session, drives an interactive `claude` worker on the subscription (the 15-juni billing escape), waits for the receipt, and tears down. No reuse, no warm-open, no terminal pin.

This lane runs Claude workers on the **subscription** (interactive `claude`, never `claude -p`) — that keeps the dispatcher off API-credit billing. It complements `subprocess_dispatch.py` (Wave 5 smart-context + terminal pinning) rather than replacing it.

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
export VNX_STATE_DIR=.vnx-data/state VNX_DATA_DIR=.vnx-data VNX_DISPATCH_DIR=.vnx-data/dispatches

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
- t0-orchestrator skill §9.2 — full dispatch-routing decision rule (canonical T0 reference)
- ADR-006 — staging→pending→promote gate enforcement
