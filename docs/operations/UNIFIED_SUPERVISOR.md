# Unified Supervisor — Operator Guide

> **Prerequisites:** SUP-PR1 (`cleanup_worker_exit`), SUP-PR2 (`lease_sweep`),
> SUP-PR3 (`runtime_supervise`), SUP-PR4 (`receipt_processor_supervisor`) must
> all be merged before enabling `VNX_SUPERVISOR_MODE=unified`.
>
> Background: see `claudedocs/2026-04-29-unified-supervisor-research.md`.

---

## What it is

The unified supervisor is a thin wiring layer that activates three existing-but-idle
components — `LeaseManager.expire_stale()`, `RuntimeSupervisor.supervise_all()`, and
`cleanup_worker_exit.py` — on a periodic schedule inside the dispatcher's main loop,
and wraps both long-running daemons (dispatcher + receipt processor) in respawn loops
with exponential backoff. In **legacy mode** (the default) none of these components
fire automatically, so existing deployments are unaffected. In **unified mode** the
daemons become self-healing and stale leases clear without operator intervention.

The MC outage on 2026-04-28 had three independent causes:

1. **No periodic TTL sweep** — `terminal_leases.state='leased'` rows accumulated when
   workers exited abnormally.  `LeaseManager.expire_stale()` existed but had no caller.
2. **No dispatcher restart** — `dispatcher_v8_minimal.sh` exited via `set -euo pipefail`
   on an unhandled error.  `dispatcher_supervisor.sh` existed but was not running.
3. **Stale `pending/` re-pickup** — when a worker crashed before the dispatch file was
   moved out of `pending/`, the next dispatcher poll re-picked it, causing duplicate
   runs.  `cleanup_worker_exit.py` (SUP-PR1) is the single-owner fix.

---

## When to enable

Enable `VNX_SUPERVISOR_MODE=unified` when you observe any of these signals:

- The dispatcher exits silently and you need to restart it manually.
- `terminal_leases` rows are left in `state='leased'` after a worker crash.
- A dispatch runs more than once (duplicate re-pickup from `pending/`).
- You need `ps aux | grep dispatcher` to confirm the dispatcher is still alive.

The flag is per-project — enable it in one project without affecting others.

---

## How to enable per project

### Step 1 — set the flag in `bin/vnx`

Open `bin/vnx` in your project root and add:

```bash
export VNX_SUPERVISOR_MODE=unified
```

This is the only required change to enable the in-loop ticks (`expire_stale` every 30s,
`supervise_all` every 60s, `cleanup_worker_exit` on every worker exit).

### Step 2 — stop the bare dispatcher

```bash
kill $(cat .vnx-data/pids/dispatcher_v8_minimal.pid)
```

Wait for the process to exit (`ps aux | grep dispatcher_v8_minimal` returns nothing).

### Step 3 — start dispatcher under the supervisor wrapper

```bash
nohup bash scripts/dispatcher_supervisor.sh \
  > .vnx-data/logs/dispatcher_supervisor.log 2>&1 &
```

The supervisor enforces its own singleton; starting it twice is safe (the second
instance exits immediately).

### Step 4 — start receipt processor under its supervisor wrapper

```bash
nohup bash scripts/receipt_processor_supervisor.sh \
  > .vnx-data/logs/receipt_processor_supervisor.log 2>&1 &
```

Both wrappers use the same respawn pattern: exponential backoff from 2 s → 60 s,
reset after 60 s of stable runtime, SIGTERM → wait 10 s → SIGKILL on stop.

---

## Verification

After starting both supervisors, confirm the system is healthy:

```bash
# Wrapper processes are alive
ps aux | grep dispatcher_supervisor | grep -v grep
ps aux | grep receipt_processor_supervisor | grep -v grep

# Lease sweep firing every ~30 s
cat .vnx-data/state/.last_lease_sweep_ts

# Runtime supervision firing every ~60 s
cat .vnx-data/state/.last_runtime_supervise_ts

# Lease sweep activity
tail -f .vnx-data/logs/lease_sweep.log

# Supervisor restart events (should be rare after initial start)
tail -f .vnx-data/logs/dispatcher_supervisor.log
```

Expected steady-state: `.last_lease_sweep_ts` advances every ~30 s, `.last_runtime_supervise_ts`
advances every ~60 s, and no `RESTARTING` entries appear in `dispatcher_supervisor.log`
unless you deliberately kill the dispatcher.

---

## Rollback

To revert to legacy bare-dispatcher mode:

```bash
# 1. Remove the flag from bin/vnx (or unset it in your shell)
unset VNX_SUPERVISOR_MODE

# 2. Stop supervisor wrappers
kill $(pgrep -f dispatcher_supervisor)
kill $(pgrep -f receipt_processor_supervisor)

# 3. Restart bare daemons as before
nohup bash scripts/dispatcher_v8_minimal.sh > /dev/null 2>&1 &
nohup bash scripts/receipt_processor_v4.sh  > /dev/null 2>&1 &
```

No database schema changes are involved — rollback is instant.

---

## Troubleshooting

### Stale lock prevents supervisor from starting

Symptom: `dispatcher_supervisor.sh` exits immediately with
`another instance is already running`.

```bash
# Check if the PID in the lock is still alive
cat .vnx-data/pids/dispatcher_supervisor.pid
kill -0 $(cat .vnx-data/pids/dispatcher_supervisor.pid) 2>&1

# If the PID is dead, remove the stale lock and PID file
rm -f .vnx-data/locks/dispatcher_supervisor.lock
rm -f .vnx-data/pids/dispatcher_supervisor.pid
```

Then restart the supervisor. The `_clear_stale_dispatcher_lock` function inside
`dispatcher_supervisor.sh` handles this automatically on the next restart attempt
as long as the wrapper itself is running.

### Supervisor PID file exists but wrapper is not respawning

Symptom: `.vnx-data/pids/dispatcher_supervisor.pid` exists, but `ps aux` shows no
matching process, and the dispatcher is also not running.

```bash
# Remove stale state and restart cleanly
rm -f .vnx-data/pids/dispatcher_supervisor.pid
rm -f .vnx-data/locks/dispatcher_supervisor.lock
nohup bash scripts/dispatcher_supervisor.sh \
  > .vnx-data/logs/dispatcher_supervisor.log 2>&1 &
```

### `lease_sweep.log` shows repeated failures

Symptom: `LEASE_SWEEP_ERROR` entries in `.vnx-data/logs/lease_sweep.log`.

Check that `scripts/lib/lease_sweep.py` and `scripts/lib/project_root.py` are
present (require SUP-PR2 merged). Then inspect the error detail:

```bash
tail -40 .vnx-data/logs/lease_sweep.log
python3 scripts/lib/lease_sweep.py --state-dir .vnx-data/state --dry-run
```

### Dispatcher restarts in a tight loop (backoff not resetting)

Symptom: `dispatcher_supervisor.log` shows `RESTARTING (backoff=60s)` repeatedly.

The dispatcher is crashing before `BACKOFF_STABLE` (60 s) elapses. Check the
dispatcher's own log:

```bash
tail -80 .vnx-data/logs/dispatcher_v8_minimal.log
```

The most common cause is a pending dispatch file that triggers an error on every
poll. Inspect `pending/` and move or delete the offending file.

---

## Architecture summary

```
bin/vnx
  └── export VNX_SUPERVISOR_MODE=unified

scripts/dispatcher_supervisor.sh           ← wrapper (backoff + respawn)
  └── scripts/dispatcher_v8_minimal.sh     ← inner loop
        ├── _maybe_expire_stale_leases()   ← every 30s → scripts/lib/lease_sweep.py
        └── _maybe_runtime_supervise()     ← every 60s → scripts/lib/runtime_supervise.py

scripts/receipt_processor_supervisor.sh    ← wrapper (backoff + respawn)
  └── scripts/receipt_processor_v4.sh     ← inner poll loop

scripts/lib/cleanup_worker_exit.py         ← called by both adapters on worker exit
  ├── LeaseManager.release()
  ├── WorkerStateManager.transition() → exited_clean / exited_bad
  └── dispatch file → completed/ or rejected/
```

State files: `.vnx-data/state/.last_lease_sweep_ts`,
`.vnx-data/state/.last_runtime_supervise_ts`.

Logs: `.vnx-data/logs/dispatcher_supervisor.log`,
`.vnx-data/logs/receipt_processor_supervisor.log`,
`.vnx-data/logs/lease_sweep.log`.

Full design rationale: `claudedocs/2026-04-29-unified-supervisor-research.md`.
