# Control Centre — multi-project T0 supervisor

> Wave 5 D5: one interactive Claude Code session supervises N per-project T0s.

---

## Overview

The Control Centre is the operator interface for running multiple VNX projects from a single terminal session. Instead of opening a separate Claude Code session per project, you register projects in a YAML file, then use `control_centre_cli.py` to dispatch tasks, query state, and monitor lifecycle across all of them simultaneously.

Each project keeps its own isolated T0 process, lease token, and NDJSON ledger. The Control Centre coordinates via state files — no shared process, no cross-project coupling at runtime.

---

## Quick start

```bash
# 1. Register your projects
cp scripts/control_centre_projects.yaml.example scripts/control_centre_projects.yaml
# Edit: set id, root, and optionally coord_db / intel_db per project

# 2. Set provider keys for non-Claude lanes (optional)
cp vnx.env.example vnx.env
# Uncomment and fill DEEPSEEK_API_KEY, MOONSHOT_API_KEY, etc.

# 3. Verify status across all projects
python3 scripts/control_centre_cli.py status

# 4. Dispatch a task to a specific project
python3 scripts/control_centre_cli.py dispatch \
  --project sales-copilot \
  --task "Fix lint errors in src/utils/"

# 5. Track the dispatch lifecycle (blocks until receipt or timeout)
python3 scripts/control_centre_cli.py track \
  --project sales-copilot \
  cc-20260516-093012-123456-sales-copilot
```

---

## Registry format

The registry is a YAML file at `scripts/control_centre_projects.yaml` (gitignored; tracked example at `.yaml.example`):

```yaml
projects:
  - id: vnx-dev          # lowercase alphanum + hyphens, 2-32 chars
    root: /path/to/vnx-dev
    coord_db: .vnx-data/state/runtime_coordination.db   # optional
    intel_db: .vnx-data/state/quality_intelligence.db   # optional

  - id: seocrawler-v2
    root: /path/to/seocrawler-v2
```

`coord_db` and `intel_db` default to `resolve_state_dir(root)` paths when omitted. Use explicit paths only when the project uses a non-standard layout.

---

## Commands

### `status`

List all registered projects with T0 lifecycle state.

```bash
python3 scripts/control_centre_cli.py status
```

Output columns: `PROJECT`, `STATE`, `PID`, `LAST_HEARTBEAT`, event counts (first 3 types).

```
PROJECT              STATE          PID      LAST_HEARTBEAT
----------------------------------------------------------------------------
vnx-dev              RUNNING        14823    2026-05-16T09:30:12
sales-copilot        not_spawned    -        -
seocrawler-v2        RUNNING        14901    2026-05-16T09:29:58
```

`not_spawned` means no active lease in `runtime_coordination.db`. Either the T0 was never started or the lease expired.

---

### `dispatch`

Write a dispatch instruction to a project's pending queue.

```bash
python3 scripts/control_centre_cli.py dispatch \
  --project <project-id> \
  --task "Instruction text here"
```

Output (stdout): the generated dispatch ID (`cc-<timestamp>-<project-id>`).

The dispatch is written to `<project-root>/.vnx-data/dispatches/pending/<dispatch_id>/`. The project's T0 picks it up via the standard VNX promote-gate flow.

---

### `track`

Block until a dispatch produces a receipt, or until timeout.

```bash
python3 scripts/control_centre_cli.py track \
  --project <project-id> \
  --timeout 600 \
  <dispatch-id>
```

Default timeout: 600 seconds. Exit code 0 = receipt received. Exit code 2 = timed out.

Uses `ReceiptTail` to watch the project's receipt stream. Emits a status line every poll cycle.

---

### `heartbeat`

Update the T0 heartbeat timestamp for a project.

```bash
python3 scripts/control_centre_cli.py heartbeat --project <project-id>
```

Used by long-running T0 sessions to keep the lease alive. Normally called by the T0 itself; operator can call manually to extend a lease during a slow dispatch.

---

### `kill`

Request graceful T0 shutdown for a project.

```bash
python3 scripts/control_centre_cli.py kill --project <project-id>
```

Sets the T0 lease state to `kill_requested`. The running T0 detects this on its next lifecycle tick and exits cleanly. Does not force-kill the process.

---

### `reap`

Sweep all projects for expired leases and release them.

```bash
python3 scripts/control_centre_cli.py reap
```

A lease is expired when `last_heartbeat_at` is older than the lease TTL. Reap marks these as `released` so the terminal becomes available for a new T0 spawn. Run this after a crashed T0 to unblock the next dispatch.

---

### `intel`

Query cross-project intelligence recommendations for a specific project.

```bash
python3 scripts/control_centre_cli.py intel --project <project-id>
```

Reads from both the project's local `quality_intelligence.db` and the global intelligence facet. Returns success patterns, antipatterns, and recent injection recommendations ranked by weight.

---

### `aggregate`

Refresh the global intelligence facet from all registered projects.

```bash
python3 scripts/control_centre_cli.py aggregate
```

Reads `quality_intelligence.db` from every registered project and materializes cross-project patterns into the central aggregator. Run before `intel` when you want fresh cross-project data.

---

## Multi-project supervision model

The Control Centre uses **state-mediated coordination**: no long-running orchestrator process exists. Each `control_centre_cli.py` invocation is stateless; it reads the current state from files and databases, acts, emits an audit event, and exits.

The persistent substrate is:
- `runtime_coordination.db` per project (lease state, heartbeats)
- `.vnx-data/dispatches/pending/` per project (dispatch queue)
- `.vnx-data/events/control_centre.ndjson` in the Control Centre repo (audit ledger)

Per-project isolation is enforced via composite lease keys (`project_id` + `terminal_id`) introduced in schema v12 (PR-5.3). Two projects can both have a T1 lease without collision.

References: ADR-013 (Workers=N configuration), ADR-017 (Control Centre product-shape), `claudedocs/wave5-control-centre-architecture.md`.

---

## Operator workflows

### Workflow 1: monitor three projects in parallel

```bash
# Check all at once
python3 scripts/control_centre_cli.py status

# Refresh global intelligence
python3 scripts/control_centre_cli.py aggregate

# Query cross-project patterns for the project that just had a failure
python3 scripts/control_centre_cli.py intel --project vnx-dev
```

No terminal switching required. All output goes to stdout in the single operator session.

---

### Workflow 2: dispatch hotfix to a specific project

```bash
# 1. Dispatch the fix instruction
DISPATCH_ID=$(python3 scripts/control_centre_cli.py dispatch \
  --project seocrawler-v2 \
  --task "Fix TypeError in src/extractors/meta_extractor.py line 142" \
  2>/dev/null)

echo "Dispatch created: $DISPATCH_ID"

# 2. Monitor until done (or timeout at 10 minutes)
python3 scripts/control_centre_cli.py track \
  --project seocrawler-v2 \
  --timeout 600 \
  "$DISPATCH_ID"

# 3. Verify final state
python3 scripts/control_centre_cli.py status
```

---

### Workflow 3: cross-project intelligence query

When a bug in one project resembles something you've seen in another:

```bash
# Ensure intelligence DBs are fresh
python3 scripts/control_centre_cli.py aggregate

# Query the affected project — gets both local + cross-project patterns
python3 scripts/control_centre_cli.py intel --project sales-copilot
```

The `intel` command uses the global facet populated by `aggregate`. Patterns from `seocrawler-v2` and `vnx-dev` appear in the recommendations if they have relevant success/antipattern signals.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `status` shows no projects listed | Registry file missing or empty | Check `scripts/control_centre_projects.yaml` exists and has `projects:` key |
| `status` shows `not_spawned` for all | No active T0 leases | Normal state if no T0 is running. Spawn a T0 in the project's own session first |
| `dispatch` returns `Project not found` | Project ID not in registry | Verify `id:` field in YAML matches `--project` argument exactly |
| `track` exits with code 2 | No receipt within timeout | The T0 may not have picked up the dispatch yet. Check `<project-root>/.vnx-data/dispatches/pending/` and the project's `dispatcher.log` |
| `reap` releases no leases | No expired leases | Leases are not expired yet, or no projects have active leases |
| `intel` returns empty | No intelligence DB at path | Run `aggregate` first, or verify `intel_db` path in registry points to an existing DB |
| Intel shows stale patterns | `aggregate` not run recently | Run `aggregate` before `intel` when cross-project freshness matters |
| Stale lease blocks new dispatch | Crashed T0 left lease open | Run `reap` to release the expired lease, then retry |

---

## Reference

- `ADR-013` — Workers as configuration (per-project T0 pools)
- `ADR-017` — Control Centre as agent role
- `scripts/control_centre_projects.yaml.example` — registry template
- `vnx.env.example` — provider key configuration
- `claudedocs/wave5-control-centre-architecture.md` — full design document
- `docs/operations/UNIFIED_SUPERVISOR.md` — supervisor mode guide
- `scripts/control_centre/dispatch_lifecycle_tracker.py` — dispatch status state machine
- `scripts/control_centre/receipt_tail.py` — receipt stream watcher
