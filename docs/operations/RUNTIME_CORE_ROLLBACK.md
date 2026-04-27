# VNX Runtime Core — Rollback Guide

## Overview

PR-5 promoted the runtime core (broker + canonical lease + tmux adapter) from shadow mode to primary coordination path. This document describes how to roll back to the legacy `terminal_state_shadow`-only path if a production regression is detected.

## When to Roll Back

Roll back if you observe any of the following after PR-5 cutover:

- Dispatches not reaching workers (broker registration loop, lease conflicts)
- `terminal_state.json` diverging from expected state (canonical lease state mismatch)
- Receipt processor failing to correlate `dispatch_id` to broker attempts
- Python import errors in `scripts/lib/runtime_core.py` or its dependencies
- Unexpected `InvalidTransitionError` or `BrokerError` blocking dispatches

## Quick Rollback (30 seconds)

```bash
# Step 1: Disable runtime core (writes VNX_RUNTIME_PRIMARY=0 to .env_override)
python scripts/rollback_runtime_core.py rollback

# Step 2: Restart the VNX system to pick up new env flags
bin/vnx stop
bin/vnx start

# Step 3: Verify legacy path is active
python scripts/rollback_runtime_core.py status
```

Expected output from `status`:
```
Mode: LEGACY ONLY (rollback — terminal_state_shadow path)
```

## What Rollback Changes

| Flag | Normal (PR-5) | Rollback |
|------|--------------|---------|
| `VNX_RUNTIME_PRIMARY` | `1` | `0` |
| `VNX_BROKER_SHADOW` | `0` | `1` |
| `VNX_CANONICAL_LEASE_ACTIVE` | `1` | `1` (unchanged) |

When `VNX_RUNTIME_PRIMARY=0`:
- `dispatcher_v8_minimal.sh` skips all `rc_*` function calls
- `load_runtime_core()` returns `None` — no broker/lease operations
- `terminal_state_shadow.py` remains the sole ownership path (unchanged)
- Existing dispatcher lock mechanism (`acquire_terminal_claim`) operates as before

## What Rollback Does NOT Change

- Receipts: `dispatch_id` is embedded in the markdown metadata block independent of the broker. Receipt processing is unaffected.
- T0 governance: completion authority was never moved to the broker. T0 review + receipt processor still control `completed` state.
- Dispatch bundles: existing bundles in `.vnx-data/dispatches/` remain on disk but are not consulted by the legacy path.
- tmux operator workflow: unchanged in both modes.

## Re-enabling Runtime Core

After fixing the root cause:

```bash
# Re-enable
python scripts/rollback_runtime_core.py enable

# Restart
bin/vnx stop
bin/vnx start

# Validate compatibility
python scripts/runtime_cutover_check.py --gate gate_pr5_runtime_core_cutover
```

## Manual Rollback (Environment Variable)

If `bin/vnx` is unavailable, set the flag in your shell before starting the dispatcher:

```bash
export VNX_RUNTIME_PRIMARY=0
bash scripts/dispatcher_v8_minimal.sh
```

Or in `.vnx-data/.env_override`:

```bash
echo "export VNX_RUNTIME_PRIMARY=0" >> .vnx-data/.env_override
```

## Compatibility Check

To validate the runtime core is functioning before cutover or after re-enable:

```bash
python scripts/runtime_cutover_check.py
```

Exit 0 = all components healthy. Exit 1 = one or more components failed (see output).

## Runtime Core Feature Flags Reference

| Variable | Default (PR-5) | Description |
|----------|---------------|-------------|
| `VNX_RUNTIME_PRIMARY` | `1` | Master switch: enables broker + canonical lease |
| `VNX_BROKER_ENABLED` | `1` | Broker on/off (broker module default) |
| `VNX_BROKER_SHADOW` | `0` | `0`=broker is authoritative, `1`=shadow mode only |
| `VNX_CANONICAL_LEASE_ACTIVE` | `1` | Canonical lease active; suppresses blind GC in terminal_state_shadow |
| `VNX_TMUX_ADAPTER_ENABLED` | `1` | tmux adapter on/off |
| `VNX_ADAPTER_PRIMARY` | `1` | `1`=load-dispatch path, `0`=legacy paste-buffer |

## Architecture Rule Compliance

| Rule | Status in rollback |
|------|-------------------|
| A-R9 — Legacy transport degrades safely | ✅ Legacy path operates unchanged when `VNX_RUNTIME_PRIMARY=0` |
| G-R4 — T0 completion authority unchanged | ✅ Unaffected by rollback; broker never owned completion |
| G-R7 — Receipt linkage survives transport changes | ✅ `dispatch_id` in receipt markdown is independent of broker |
