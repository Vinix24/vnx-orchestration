# Subprocess Adapter Feature Flag

## Overview

The VNX dispatcher supports per-terminal adapter selection via environment variables.
This allows individual terminals to be routed through a headless `SubprocessAdapter`
instead of the default tmux send-keys delivery.

## Configuration

Set a per-terminal env var before starting the dispatcher:

```bash
# Route T1 through SubprocessAdapter
export VNX_ADAPTER_T1=subprocess

# Route T2 through SubprocessAdapter
export VNX_ADAPTER_T2=subprocess

# Explicit tmux (same as default/unset)
export VNX_ADAPTER_T1=tmux
```

Supported values:
- `subprocess` — delivers via `SubprocessAdapter` (headless `claude -p` subprocess)
- `tmux` — delivers via tmux send-keys (existing behavior)
- unset — T0 defaults to `tmux`; T1/T2/T3 default to `subprocess` (set `VNX_ADAPTER_Tx=tmux` to opt-out)

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
- `tests/test_subprocess_dispatch_integration.py` — integration tests
