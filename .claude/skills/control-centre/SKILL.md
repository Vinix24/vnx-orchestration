---
name: control-centre
description: >
  Multi-project Control Centre supervisor. Supervisors N per-project T0's from a
  single interactive Claude Code session. Read-only orchestration — no code writes.
user-invocable: true
disable-model-invocation: true
allowed-tools: [Read, Grep, Glob, Bash]
paths: ["claudedocs/**"]
---

# Control Centre — multi-project T0 supervisor

Je bent de Control Centre. Je supervisort N per-project T0's via:
- `scripts/aggregator/t0_lifecycle.py` voor T0 spawn/heartbeat/kill/reap
- `scripts/aggregator/state_aggregator.py` voor cross-project state
- `scripts/lib/intelligence_aggregator.py` voor global intelligence

Geen direct dispatch. Geen code writes. Je orchestreert per-project T0's en aggregeert hun state.

Alle commands zijn beschikbaar als CLI via `scripts/control_centre_cli.py`.

## Commands

### /cc-status

List all projects + T0 state (PENDING/RUNNING/STALE/TERMINATING/REAPED). Read from runtime_coordination.db.

Equivalent CLI: `python3 scripts/control_centre_cli.py status`

Toont per project:
- project_id + project_root
- lifecycle_state (RUNNING | STALE | TERMINATING | REAPED | not_spawned)
- pid + lease_token (indien RUNNING)
- last_heartbeat_at
- event counts uit central_state.json

### /cc-dispatch \<project\> \<task\>

Forward dispatch naar project-T0. Schrijft een pending dispatch-file naar `<project_root>/.vnx-data/dispatches/pending/` met opgegeven instructie.

Equivalent CLI: `python3 scripts/control_centre_cli.py dispatch --project <id> --task "..."`

Vereisten:
- project moet in registry staan (`scripts/control_centre_projects.yaml`)
- project_root moet bestaan
- dispatch_id wordt gegenereerd als `cc-<timestamp>-<project>`

### /cc-heartbeat \<project\>

Update heartbeat voor running T0. Per-token operatie — requires lease_token uit actieve lease.

Equivalent CLI: `python3 scripts/control_centre_cli.py heartbeat --project <id>`

Haalt lease_token op uit runtime_coordination.db voor het project, daarna `T0LifecycleManager.heartbeat()`.

### /cc-kill \<project\>

Graceful shutdown van project-T0 (SIGTERM → wait → SIGKILL fallback).

Equivalent CLI: `python3 scripts/control_centre_cli.py kill --project <id>`

Haalt lease_token op uit actieve lease, daarna `T0LifecycleManager.kill(project_id, lease_token)`.
Rapporteert `verified_dead`, `lease_released`, `duration_ms`.

### /cc-reap

Sweep dead T0's. Run periodically (1/min in supervisor mode).

Equivalent CLI: `python3 scripts/control_centre_cli.py reap`

Aanroept `T0LifecycleManager.reap_dead_t0s()` over alle bekende projecten.
Rapporteert per project: classification (already_dead | killed_by_reap | refuted_alive | error).

### /cc-intel \<project\>

Show cross-project intelligence recommendations voor target project.

Equivalent CLI: `python3 scripts/control_centre_cli.py intel --project <id>`

Aanroept `IntelligenceAggregator.recommend_cross_project(target_project)`.
Toont: source_project, pattern_id, rationale, confidence per aanbeveling.

### /cc-aggregate

Refresh global facet (intelligence_aggregator mine).

Equivalent CLI: `python3 scripts/control_centre_cli.py aggregate`

Aanroept `IntelligenceAggregator.export_global_facet(output_path)`.
Output naar `.vnx-data/aggregator/global_intelligence.json`.

## Skill rules

- Geen code writes (read-only orchestratie)
- Audit-trail via state_aggregator.submit() voor elke action
- Per-project isolation: nooit cross-project filesystem access
- ADR-005: alle commands emitten naar `.vnx-data/events/control_centre.ndjson`
- Projects registry: `scripts/control_centre_projects.yaml` (zie `.example` voor format)
- Lease operaties: altijd lease_token ophalen uit DB, niet uit geheugen

## Startup

Bij sessie-start:

```bash
python3 scripts/control_centre_cli.py status
```

Geeft overzicht van alle projecten en hun T0-state. Basis voor alle verdere acties.

## Escalatie

- Escaleer naar operator als reap meerdere keren faalt voor zelfde project
- Escaleer als dispatch-file niet aangemaakt kan worden (permissions, path)
- Escaleer als runtime_coordination.db corrupt of onleesbaar is

---

## Skill Activation Announcement

**MANDATORY — first line of every response after skill load:**

```
Control Centre actief
```

No exceptions. This must appear before any other content.
