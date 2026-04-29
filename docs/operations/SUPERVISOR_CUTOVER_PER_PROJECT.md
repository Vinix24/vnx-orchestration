# Supervisor Cutover — Per-Project Steps

Full guide: `docs/operations/UNIFIED_SUPERVISOR.md`.

This file provides concrete, copy-paste steps for each known project.
Run these steps only after SUP-PR1..PR4 are merged to `main`.

---

## vnx-roadmap-autopilot-wt

**Profile:** Interactive terminals (T1/T2/T3 Sonnet-pinned). Currently runs
`dispatcher_v8_minimal.sh` directly via `nohup`, plus `vnx_supervisor_simple.sh`
from the `vnx-manager` skill for non-dispatcher daemons.

```bash
# 1. Pull latest main (must include SUP-PR1..PR4)
cd /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt
git fetch origin && git merge --ff-only origin/main

# 2. Set the flag
grep -q VNX_SUPERVISOR_MODE bin/vnx \
  && echo "already set" \
  || echo 'export VNX_SUPERVISOR_MODE=unified' >> bin/vnx

# 3. Stop bare dispatcher (check PID file exists first)
kill $(cat .vnx-data/pids/dispatcher_v8_minimal.pid) 2>/dev/null || true

# 4. Start dispatcher under supervisor wrapper
nohup bash scripts/dispatcher_supervisor.sh \
  > .vnx-data/logs/dispatcher_supervisor.log 2>&1 &

# 5. Start receipt processor under supervisor wrapper
nohup bash scripts/receipt_processor_supervisor.sh \
  > .vnx-data/logs/receipt_processor_supervisor.log 2>&1 &

# 6. Verify
ps aux | grep -E "dispatcher_supervisor|receipt_processor_supervisor" | grep -v grep
cat .vnx-data/state/.last_lease_sweep_ts        # should update within 30s
cat .vnx-data/state/.last_runtime_supervise_ts  # should update within 60s
```

**Rollback:**

```bash
unset VNX_SUPERVISOR_MODE
# Remove the export line from bin/vnx
kill $(pgrep -f dispatcher_supervisor) 2>/dev/null || true
kill $(pgrep -f receipt_processor_supervisor) 2>/dev/null || true
nohup bash scripts/dispatcher_v8_minimal.sh > /dev/null 2>&1 &
nohup bash scripts/receipt_processor_v4.sh  > /dev/null 2>&1 &
```

---

## SEOcrawler_v2

**Profile:** Interactive workers. Separate VNX install (`.vnx/` is a distinct
clone, not symlinked from roadmap-autopilot-wt). The `bin/vnx` file in this
project sets project-specific environment variables.

```bash
# 1. Pull latest vnx-system main into SEOcrawler's .vnx directory
cd /path/to/SEOcrawler_v2/.vnx
git fetch origin && git merge --ff-only origin/main

# 2. Back in project root — set the flag
cd /path/to/SEOcrawler_v2
grep -q VNX_SUPERVISOR_MODE bin/vnx \
  && echo "already set" \
  || echo 'export VNX_SUPERVISOR_MODE=unified' >> bin/vnx

# 3. Stop bare dispatcher
kill $(cat .vnx-data/pids/dispatcher_v8_minimal.pid) 2>/dev/null || true

# 4. Start under wrapper
nohup bash .vnx/scripts/dispatcher_supervisor.sh \
  > .vnx-data/logs/dispatcher_supervisor.log 2>&1 &
nohup bash .vnx/scripts/receipt_processor_supervisor.sh \
  > .vnx-data/logs/receipt_processor_supervisor.log 2>&1 &

# 5. Verify
ps aux | grep -E "dispatcher_supervisor|receipt_processor_supervisor" | grep -v grep
tail -f .vnx-data/logs/lease_sweep.log
```

> **Note on script path:** SEOcrawler uses `.vnx/scripts/` (not `scripts/`
> directly). Confirm your project's VNX install path before copying commands.

---

## MC project

**Profile:** Mixed-mode — some terminals use `VNX_ADAPTER_T{n}=subprocess`
(headless). This is the project where the 2026-04-28 outage occurred.

**Additional context:** The outage was caused by `dispatcher_v8` exiting silently,
leaving `terminal_leases.state='leased'` stale. The operator had to run
`UPDATE terminal_leases SET state='idle'` manually 6+ times. In unified mode,
`cleanup_worker_exit.py` handles the subprocess exit path and `expire_stale()`
runs every 30 s as a backstop — neither condition can accumulate silently.

```bash
# 1. Pull latest vnx-system main
cd /path/to/mc-project/.vnx   # or scripts/ depending on install layout
git fetch origin && git merge --ff-only origin/main
cd /path/to/mc-project

# 2. Set flag (keep existing VNX_ADAPTER_T{n}=subprocess lines as-is)
grep -q VNX_SUPERVISOR_MODE bin/vnx \
  && echo "already set" \
  || echo 'export VNX_SUPERVISOR_MODE=unified' >> bin/vnx

# 3. Stop existing bare dispatcher
kill $(cat .vnx-data/pids/dispatcher_v8_minimal.pid) 2>/dev/null || true

# 4. Confirm no stale leases before restart (clean slate)
python3 scripts/lib/lease_sweep.py --dry-run

# 5. Start under wrappers
nohup bash scripts/dispatcher_supervisor.sh \
  > .vnx-data/logs/dispatcher_supervisor.log 2>&1 &
nohup bash scripts/receipt_processor_supervisor.sh \
  > .vnx-data/logs/receipt_processor_supervisor.log 2>&1 &

# 6. Verify subprocess-mode lease cleanup is wired
#    (requires SUP-PR1 merged — cleanup_worker_exit.py called from subprocess_dispatch.py)
grep -n "cleanup_worker_exit" scripts/lib/subprocess_dispatch.py

# 7. Monitor for 24 h
watch -n 30 "cat .vnx-data/state/.last_lease_sweep_ts && \
  python3 scripts/lib/lease_sweep.py --dry-run"
```

**Success criteria for MC cutover:** Zero manual `UPDATE terminal_leases` SQL
commands required over a 24-hour window with at least one subprocess worker
dispatch completing normally.

---

## Common checklist (all projects)

Before cutover:
- [ ] SUP-PR1..PR4 merged to `main` and pulled
- [ ] `scripts/lib/cleanup_worker_exit.py` present
- [ ] `scripts/lib/lease_sweep.py` present
- [ ] `scripts/lib/runtime_supervise.py` present
- [ ] `scripts/receipt_processor_supervisor.sh` present
- [ ] No bare `dispatcher_v8_minimal.sh` or `receipt_processor_v4.sh` still running

After cutover (verify within 5 minutes):
- [ ] `ps aux | grep dispatcher_supervisor` shows wrapper PID
- [ ] `ps aux | grep receipt_processor_supervisor` shows wrapper PID
- [ ] `.vnx-data/state/.last_lease_sweep_ts` updating every ~30 s
- [ ] `.vnx-data/state/.last_runtime_supervise_ts` updating every ~60 s
- [ ] `tail .vnx-data/logs/dispatcher_supervisor.log` shows no CRASH loops
