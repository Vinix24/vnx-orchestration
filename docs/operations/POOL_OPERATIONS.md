# Pool Operations Guide

Operator reference for elastic worker pool management via `vnx pool`.

See ADR-018 for the elastic pool architecture and Wave 6 design decisions.

---

## Quick start

Always run `status` first to understand the current pool state before making changes.

```bash
vnx pool status --project my-project
```

Example output:
```
Pool: default
Project: my-project
Current: 2 / policy=queue_depth_v1 (min=1, max=6)
Queue depth: 0
Active workers:
  my-project-P12345-0 (claude) joined=5min ago
  my-project-P12346-1 (claude) joined=4min ago
```

With `--json` for machine-readable output:

```bash
vnx pool status --project my-project --json
```

```json
{
  "pool_id": "default",
  "project_id": "my-project",
  "current": 2,
  "min": 1,
  "max": 6,
  "policy": "queue_depth_v1",
  "queue_depth": 0,
  "members": [
    {"terminal_id": "my-project-P12345-0", "provider": "claude"},
    {"terminal_id": "my-project-P12346-1", "provider": "claude"}
  ]
}
```

---

## Subcommands

### `vnx pool status`

Show the current state of a pool.

```
vnx pool status [--project <id>] [--pool-id <pool>] [--json]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--project` | `default` | Project identifier |
| `--pool-id` | `default` | Pool name within the project |
| `--json` | off | Emit JSON instead of human-readable output |

**Example:**
```bash
vnx pool status --project seocrawler
vnx pool status --project seocrawler --json | jq .current
```

---

### `vnx pool scale`

Force the pool to a specific worker count, bypassing the normal scaling policy.

```
vnx pool scale --project <id> --to <N> [--pool-id <pool>]
```

The target `N` is validated against the pool's configured `min` and `max`. Scale requests outside this range are rejected.

**Example — scale up to 4 workers:**
```bash
vnx pool scale --project seocrawler --to 4
```

```
Decision: scale_up delta=2
Spawned: 2 ['seocrawler-P98765-0', 'seocrawler-P98765-1']
Reaped: 0 []
```

**Example — scale down to 1 worker:**
```bash
vnx pool scale --project seocrawler --to 1
```

---

### `vnx pool config`

Update pool configuration. Changes take effect on the next tick.

```
vnx pool config --project <id> [--min N] [--max N] [--policy <name>] [--cooldown <s>]
```

| Flag | Description |
|------|-------------|
| `--min N` | Minimum worker floor |
| `--max N` | Maximum worker ceiling |
| `--policy <name>` | Scaling policy: `fixed`, `queue_depth_v1`, `cost_aware_v1` |
| `--cooldown <s>` | Seconds to wait between scaling events |

**Example — raise the ceiling and lower cooldown:**
```bash
vnx pool config --project seocrawler --max 8 --cooldown 30
```

```
Updated config for 'default': {'max_workers': 8, 'cooldown_seconds': 30.0}
```

Verify the change took effect:
```bash
vnx pool status --project seocrawler
```

---

### `vnx pool reap`

Identify and remove stale or stuck workers.

Default behavior (without `--force`) is a **dry-run** that shows candidates without removing them.

```
vnx pool reap --project <id> [--pool-id <pool>] [--force]
```

**Dry-run (default):**
```bash
vnx pool reap --project seocrawler
```

```
WARN: --force not specified. Reap candidates (dry-run):
  would reap: seocrawler-P11111-0 reason=heartbeat_stale=250s>180s
```

**Actually reap:**
```bash
vnx pool reap --project seocrawler --force
```

```
Reaped 1 workers
  seocrawler-P11111-0 (heartbeat_stale=250s>180s)
```

---

## Troubleshooting

### Project ID typo

If `status` fails with a RuntimeError about missing `pool_config` rows, the project ID is wrong or the pool has not been bootstrapped. Run the migration from ADR-018 and insert a `pool_config` row for your project.

```bash
# Check which projects have pool_config rows
sqlite3 .vnx-data/state/runtime_coordination.db \
  "SELECT project_id, pool_id, min_workers, max_workers FROM pool_config;"
```

### Config drift after manual DB edits

If pool behavior does not match the expected config, verify with:

```bash
vnx pool status --project <id> --json
```

Then reconcile via `vnx pool config --project <id> --min ... --max ...`.

### Workers not being reaped

Run `vnx pool reap --project <id>` (dry-run) to see candidates. If the list is empty but you expect stale workers, check that their `last_heartbeat_at` in `terminal_leases` is actually missing or old. The reap threshold is 180s by default.

If candidates appear but `--force` is not cleaning them, check the pool event log:

```bash
tail -f .vnx-data/events/pool_events.ndjson | python3 -m json.tool
```

### Force-reap followed by status shows old workers

`reap_dead()` marks members as `released_at IS NOT NULL`. The next `status` call queries only unreleased members, so old entries will not appear. If they do, there may be duplicate membership rows — investigate with:

```bash
sqlite3 .vnx-data/state/runtime_coordination.db \
  "SELECT * FROM worker_pool_membership WHERE released_at IS NULL;"
```

---

## Reference

- ADR-018: Elastic Worker Pool design
- Wave 6 architecture: `claudedocs/wave6-workers-n-architecture.md`
- Pool event log: `.vnx-data/events/pool_events.ndjson`
- Schema: `schemas/migrations/0020_elastic_worker_pool.sql`
