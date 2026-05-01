# Subprocess Adapter Feature Flag

## Overview

The VNX dispatcher supports per-terminal adapter selection via environment variables.
This allows individual terminals to be routed through a headless `SubprocessAdapter`
instead of the default tmux send-keys delivery.

## Configuration

Set a per-terminal env var before starting the dispatcher:

```bash
# Route T0 through SubprocessAdapter (headless orchestrator)
export VNX_ADAPTER_T0=subprocess

# Route T1 through SubprocessAdapter
export VNX_ADAPTER_T1=subprocess

# Route T2 through SubprocessAdapter
export VNX_ADAPTER_T2=subprocess

# Route T3 through SubprocessAdapter
export VNX_ADAPTER_T3=subprocess

# Explicit tmux (same as default/unset)
export VNX_ADAPTER_T1=tmux
```

Supported values:
- `subprocess` — delivers via `SubprocessAdapter` (headless `claude -p` subprocess)
- `tmux` — delivers via tmux send-keys (existing behavior)
- unset — T0 defaults to `tmux`; T1/T2/T3 default to `subprocess` (set `VNX_ADAPTER_Tx=tmux` to opt-out)

## Four-Mode Combinations

| Mode | VNX_ADAPTER_T0 | VNX_ADAPTER_T1/T2/T3 | Launch command |
|------|----------------|----------------------|----------------|
| 1. All interactive | tmux (unset) | tmux | `vnx start` |
| 2. Interactive T0 + headless workers | tmux (unset) | subprocess | `VNX_ADAPTER_T1=subprocess VNX_ADAPTER_T2=subprocess VNX_ADAPTER_T3=subprocess vnx start` |
| 3. All headless | subprocess | subprocess | `VNX_ADAPTER_T0=subprocess VNX_ADAPTER_T1=subprocess VNX_ADAPTER_T2=subprocess VNX_ADAPTER_T3=subprocess python3 scripts/headless_orchestrator.py` |
| 4. Headless T0 + interactive workers | subprocess | tmux | `VNX_ADAPTER_T0=subprocess vnx start` |

**Mode 2** is the recommended daily-driver: operator approves dispatches from a live T0 pane while workers execute silently in the background.

**Mode 3** is designed for CI/cron and autonomous overnight chains. Use `scripts/headless_orchestrator.py` as the entry point — it handles T0 decision loop, file-watcher triggers, and silence watchdogs without requiring a tmux session.

## Implementation

The routing check lives in `deliver_dispatch_to_terminal()` in `scripts/lib/dispatch_deliver.sh`:

```bash
# T0: default tmux. T1/T2/T3: default subprocess (headless workers) since F32.
local adapter_var="VNX_ADAPTER_${terminal_id}"
local adapter_type="${!adapter_var}"  # resolved by caller per terminal

if [[ "$adapter_type" == "subprocess" ]]; then
    _ddt_subprocess_delivery ...
    return $?
fi
# default: tmux delivery path
```

The subprocess delivery helper is `scripts/lib/subprocess_dispatch.py`, which calls
`SubprocessAdapter.deliver()` and exits 0 on success, 1 on failure.

## Billing Safety

The subprocess path only calls `subprocess.Popen(["claude", ...])`.
**No Anthropic SDK is used.**

## Related Files

- `scripts/lib/subprocess_dispatch.py` — thin Python helper for subprocess delivery
- `scripts/lib/subprocess_adapter.py` — SubprocessAdapter implementation (F28 PR-2/PR-3)
- `scripts/lib/dispatch_deliver.sh` — routing logic added in F28 PR-4
- `scripts/headless_orchestrator.py` — all-headless entry point for Mode 3 (CI/cron)
- `tests/test_subprocess_dispatch_integration.py` — integration tests

## See Also

- [docs/manifesto/HEADLESS_TRANSITION.md](../manifesto/HEADLESS_TRANSITION.md) — architectural narrative, mode decision guide, and migration path
- [docs/operations/EVENT_STREAMS.md](EVENT_STREAMS.md) — per-terminal NDJSON structure produced by headless workers
