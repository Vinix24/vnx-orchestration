# Operations

**Status**: Active
**Last Updated**: 2026-09-27
**Owner**: T-MANAGER
**Purpose**: Entry point for running, monitoring, and troubleshooting the VNX system.

---

## Canonical Docs

**Dispatch, receipts, and lanes**
- Receipt pipeline: `RECEIPT_PIPELINE.md`
- Event streams: `EVENT_STREAMS.md`
- Runtime rollback (broker + canonical lease + tmux adapter cutover): `RUNTIME_CORE_ROLLBACK.md`
- Subprocess adapter flag (per-terminal adapter selection): `SUBPROCESS_ADAPTER_FEATURE_FLAG.md`
- tmux-spawn lane (removed 2026-09-18, kept as history): `TMUX_SPAWN_LANE.md`
- Provider API keys (non-Claude lanes via the LiteLLM bridge, ADR-015): `PROVIDER_API_KEYS.md`
- Forge gate runbook (gate-enforcement-to-forge; GitHub refuses a merge to `main` without a published gate verdict on the PR head): `FORGE_GATE.md`

**T0 and worker operations**
- T0 runbooks (operational recipes for the `t0-orchestrator` role): `T0_RUNBOOKS.md`
- T0 handoff contract (`handoff.md` file, `vnx handoff` CLI): `CONTEXT_ROTATION.md`
- T0 context rotation enforced at 500K (guard hook, spawner, handoff contract): `T0_CONTEXT_ROTATION.md`
- Worker permissions (scoped worker-mode default, `VNX_WORKER_BLANKET_SKIP=1` opt-out): `WORKER_PERMISSIONS.md`
- Control Centre (one interactive session supervises N per-project T0s): `CONTROL_CENTRE.md`
- Unified supervisor (auto-respawn, lease sweep, runtime supervision): `UNIFIED_SUPERVISOR.md`
- Elastic worker pool management (`vnx pool`): `POOL_OPERATIONS.md`
- Producer freshness monitor (silent-failure detection): `PRODUCER_FRESHNESS_MONITOR.md`

**State and reconciliation**
- State aggregator (write-path for multi-project state, per-project facet files): `STATE_AGGREGATOR.md`
- Objective reconcile (`vnx objective reconcile`, batch git-grounded auto-close loop): `OBJECTIVE_RECONCILE.md`

**Install, build, and migration**
- Central install runbook (multi-project shared system install): `install-central-runbook.md`
- Package build (local dev builds, editable installs, packaging smoke tests): `PACKAGE_BUILD.md`
- Migration rollback runbook (Wave 2a rollback chain, migrations 0010+): `migration-rollback-runbook.md`

> **Note**: `MONITORING_GUIDE.md` was retired. Runtime monitoring is now available
> via the dashboard server (`dashboard/serve_dashboard.py`) at `/api/health`.
> `MULTI_MODEL_GUIDE.md`, `AUTONOMOUS_PRODUCTION_GUIDE.md`, and `RECEIPT_PROCESSING_FLOW.md`
> were archived 2026-09-27 (docs-verversing sweep) — see `../_archive/ARCHIVED_MANIFEST.md`.

## Public Operations Scope

Dispatch policy, queue behavior, and governance flow are documented in:

- `../DISPATCH_GUIDE.md`
- `../core/00_VNX_ARCHITECTURE.md`
- `../core/DISPATCH_RULES.md`
- `../contracts/`

## Archive

Historical operational notes and one-off fix reports are archived under `../_archive/`.
