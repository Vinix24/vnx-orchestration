# ADR-013 — Worker Pool Size as Configuration, Not Constant (workers = N)

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Wave 4.5 prerequisite + PRD-VNX-UH-002 §6 FR-12 + inventory finding "Workers are hardcoded T0..T3 across 25+ files" (`claudedocs/2026-05-09-vnx-replan-inventory.md`)
**Cross-references:** ADR-010 (subprocess adapter), ADR-011 (manager+worker hierarchy), ADR-012 (hybrid interactive + headless)

## Context

VNX shipped with a fixed four-terminal layout: one master orchestrator (T0) and three workers (T1, T2, T3). That layout is encoded as a constant across the runtime, not as configuration. A repository-wide audit done as part of the 2026-05-09 strategic replan inventory found the literal terminal identifiers hardcoded in **77 files** under `scripts/`, with the most architecturally significant occurrences being:

- `scripts/lib/runtime_facade.py:49` — `CANONICAL_TERMINALS = ("T0", "T1", "T2", "T3")` (the canonical authority used by the rest of the runtime).
- `scripts/lib/runtime_facade.py:199` — `ids = list(CANONICAL_TERMINALS if terminal_ids is None else terminal_ids)` (default fallback in the lease-issuing path).
- `scripts/lib/decision_executor.py:31-33` — terminal-to-track mapping `{"T1": "A", "T2": "B", "T3": "C"}` literally encodes "three tracks, three workers."
- `scripts/lib/vnx_doctor_checks.py:492` — `expected_terminals = {"T0", "T1", "T2", "T3"}` causes the doctor to alarm on any deployment that does not match the four-terminal shape.
- `scripts/lib/tmux_session_profile.py:40` — `HOME_TERMINALS = ("T0", "T1", "T2", "T3")` (tmux session profile hardcodes the same four).
- `scripts/lib/tmux_adapter.py:681` — explicit `for tid in ("T0", "T1", "T2", "T3"):` iteration in the tmux adapter.
- `scripts/lib/receipt_terminal_detection.sh:16-40` — bash `case` statement maps tracks `A/B/C` to `T1/T2/T3` in three separate branches.

Beyond those structural sites, the same identifiers appear in `build_t0_state.py`, `build_t0_quality_digest.py`, `pr_queue_manager.py`, `runtime_coordination_init.py`, `lease_heartbeat.py`, `cost_tracker.py`, `intelligence_daemon.py`, `headless_orchestrator.py`, `pane_config.sh`, `vnx_doctor.py`, `shadow_mode_runner.py`, `gather_intelligence.py`, `update_progress_state.py`, `vnx_init.py`, `vnx_worktree_setup.sh`, `t0_intelligence_aggregator.py`, `query_t0_brief.sh`, `dispatcher_v8_minimal.sh`, `smart_tap_v7_json_translator.sh`, and a long tail of test fixtures and digest builders. The inventory's "25+ files" figure undercounts the actual blast radius; the real number is closer to 77 once shell scripts and tests are included.

This is fine for the present four-worker setup. It is not fine for two upcoming realities:

1. The operator already runs **four parallel VNX deployments** (sales-copilot, mission-control, SEOcrawler-v2, and the autopilot worktree itself). Each deployment has different throughput and review-budget needs. Forcing every deployment into "exactly three workers + one orchestrator" wastes hardware on lighter projects (sales-copilot may need only 2 workers) and starves heavier ones (mission-control may want 6).
2. PRD-VNX-UH-002 §6 FR-12 explicitly requires that "Worker pool size MUST be config-driven (workers = N), not hardcoded T0..T3." That FR is gated on this ADR per the FR table in the PRD.

The four-terminal limit is not a tested boundary (no scaling test asserts it; no SLA depends on it). It is an artifact of the original prototype that solidified into 77 implicit dependencies. Removing it does not introduce new scaling claims; it removes an accidental ceiling.

## Decision

**Worker terminal identity becomes config-driven.** Pool size, identifier, role, and adapter mode are declared per orchestrator in a new `vnx_workers.yaml` (located under `.vnx/`) and resolved through a single `WORKER_REGISTRY` accessor in `scripts/lib/runtime_facade.py`. The literal `CANONICAL_TERMINALS` tuple is removed. The existing four-worker layout becomes the **default config**, shipped as `vnx_workers.default.yaml`, and is no longer special at the code level — the four entries `T0..T3` are loaded the same way an operator-specific six-worker entry would be.

Concrete contract:

- `vnx_workers.yaml` schema: a list of records with fields `terminal_id` (string, required, unique within the file), `role` (string from `validate_skill.py --list`), `adapter` (`tmux` | `subprocess`, default `subprocess` per ADR-010), `model_pin` (optional, e.g. `sonnet`), and `aliases` (optional list of legacy identifiers).
- The orchestrator identifier (default `T0`) is a special record with `role: orchestrator` and is always present.
- Worker identifiers are arbitrary strings; the `T1..Tn` shape is a convention, not a requirement. An operator who wants `track-a-1`, `track-a-2`, `gate-runner` may use those names. The four-worker default ships with `T0..T3` for backward compatibility.
- Existing references to `T1..T3` continue to resolve via the `aliases` field in the default config — old dispatches, old receipts, old NDJSON archives are not rewritten.
- The 77 hardcoded sites are migrated to read from `WORKER_REGISTRY.list_workers()` / `WORKER_REGISTRY.by_id(...)` / `WORKER_REGISTRY.by_role(...)`. The track-letter map in `decision_executor.py:31-33` becomes a config-driven projection; the `vnx_doctor_checks.py:492` "expected terminals" set is computed from the registry at check time.
- Per-orchestrator pool size (which matters once ADR-011 sub-orchestrators land) reads its own scoped `vnx_workers.yaml` from the orchestrator's working directory; the global default applies otherwise.

## Reasoning

1. **The single-host operator wants flexibility.** A solo developer on a laptop running one project at a time is well-served by a 1-worker setup (T0 + T1). A team-of-one running four parallel VNX deployments needs four different worker counts. The current code denies both — every deployment looks like the original prototype. Config-driven worker identity is the smallest change that unblocks both extremes.

2. **Multi-orchestrator hierarchy (ADR-011) requires per-sub-orchestrator pool sizes.** ADR-011 says "T0..Tn architecture with depth>1 stays canonical … manager-of-managers patterns are explicitly in scope." A sub-orchestrator that itself dispatches sub-workers cannot reuse a global `CANONICAL_TERMINALS`; its pool is per-instance. Without the registry abstraction in this ADR, ADR-011's depth>1 future would force every sub-orchestrator to cosplay as `T1..T3`, which is incoherent.

3. **PRD-UH-002 ICP demands per-deployment N.** The ICP in §3 of the PRD is "operator running 4+ VNX deployments." Four deployments × different worker budgets = a 1×N config matrix. The PRD writes FR-12 explicitly to tee up this ADR; this ADR redeems it.

4. **The four-terminal ceiling is implicit, not load-tested.** No benchmark asserts that "VNX breaks at 5+ workers." No SLA, no capacity test, no operational alarm. The constant exists because the prototype shipped that way. Removing the constant does not add new performance claims — it removes an artificial ceiling that was never argued for.

5. **Decouples worker identity from physical pane / subprocess slot.** Today `T1` means simultaneously "the second pane in the tmux session," "the second subprocess slot," "track A," and "the worker Sonnet-pinned for backend work." The registry separates these concerns: `terminal_id` is identity, `adapter` is delivery mechanism, `role` is responsibility. That decoupling is a prerequisite for Wave 5 sandbox-per-worker (one container per `terminal_id`, regardless of T-letter).

6. **Backwards compatibility is cheap.** Aliases let every legacy script, log line, and receipt continue to mean what they meant. The migration is a refactor, not a breaking change. Existing `t0_receipts.ndjson` archives stay readable; new receipts use whatever `terminal_id` is configured.

7. **Doctor and supervisor checks become deployment-aware.** Today `vnx_doctor_checks.py:492` flags any deployment that lacks T2 as broken — even when the operator deliberately ran a 2-worker deployment. After this ADR, the doctor checks "every configured worker is reachable," which is the correct invariant.

## Consequences

### Accepted

- A new `.vnx/vnx_workers.default.yaml` ships with the existing T0..T3 entries and is loaded if no per-deployment override exists.
- A new `WORKER_REGISTRY` module (`scripts/lib/worker_registry.py`) wraps `vnx_workers.yaml` parsing, alias resolution, and `by_id` / `by_role` / `list_workers` accessors.
- The 77 hardcoded sites are migrated incrementally. The migration is gated on three entry points being switched first: `runtime_facade.CANONICAL_TERMINALS`, `decision_executor.TERMINAL_TO_TRACK`, and `vnx_doctor_checks.expected_terminals`. Once those three reach the registry, the long tail can land in subsequent PRs without breaking the runtime.
- Aliases preserve every legacy identifier. `T1..T3` continue to work; operator scripts referencing those names do not break.
- The doctor and supervisor compute their expected-shape sets from the registry at runtime.
- New `vnx_workers.yaml` examples ship for a 1-worker setup, a 6-worker setup, and a multi-orchestrator depth-2 setup — to make the contract concrete for the operator's four deployments.
- Receipts, NDJSON events, and lease records all carry `terminal_id` strings unchanged. The format does not change; only the source of truth for "which IDs are valid" changes.

### Rejected

- **Removing T0..T3 entirely.** Rejected — backward compatibility for existing deployments, log archives, and operator muscle memory matters. The four-worker default stays.
- **Renaming canonical roles.** Rejected — `architect`, `backend-developer`, `quality-engineer`, etc. are stable per `validate_skill.py --list`. Roles are orthogonal to terminal IDs.
- **Making N dynamic per dispatch.** Rejected — pool size is a deployment-level decision, not a per-dispatch decision. Per-dispatch elasticity adds scheduling complexity without operator-visible benefit at VNX's scale (per ADR-001's "we are not Twitter" reasoning). Operators who need different N for different feature chains can run a second deployment.
- **Auto-spawning workers on demand.** Rejected — the operator approves the deployment shape up front; runtime-spawned workers would create implicit consent surface inconsistent with ADR-006's mandatory human gate.
- **Requiring a registry rewrite to add a single worker.** Rejected — adding a worker is one new YAML record; no schema migration, no DB rebuild.

## Implementation note

- The migration is a Wave 4.5 task per `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §5. It is gated on Wave 4 completion (subprocess adapter feature parity) but does not block earlier waves.
- The migration order is: (1) ship `worker_registry.py` + `vnx_workers.default.yaml`, (2) switch `runtime_facade.CANONICAL_TERMINALS` to call the registry, (3) switch `decision_executor.TERMINAL_TO_TRACK` to a config-driven projection, (4) switch `vnx_doctor_checks.expected_terminals` to runtime computation, (5) migrate the long tail (intelligence, build_t0_state, pr_queue_manager, etc.) in subsequent PRs. Each step is independently mergeable.
- The `aliases` field is the seam that lets the migration be incremental — files that have not yet been refactored continue to see the legacy IDs they expect.

## See also

- ADR-010 — Subprocess adapter (the spawn mechanism per registered worker)
- ADR-011 — Manager+worker hierarchy with depth>1 (consumes registry for sub-orchestrator pools)
- ADR-012 — Hybrid interactive + headless (each registered worker can be tmux- or subprocess-routed)
- `claudedocs/PRD-VNX-UH-002-v1.0-DRAFT.md` §6 FR-12 — the functional requirement this ADR redeems
- `claudedocs/2026-05-09-vnx-replan-inventory.md` — the inventory that surfaced the 77-file blast radius
- `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §5 (Wave 4.5) — the wave this ADR unlocks
- `scripts/lib/runtime_facade.py:49,199` — the canonical authority being replaced
- `scripts/lib/decision_executor.py:31-33` — the track-letter mapping being projected from config
- `scripts/lib/vnx_doctor_checks.py:492` — the expected-terminals set being recomputed at runtime
- `scripts/lib/tmux_session_profile.py:40` — the tmux home-terminal tuple being driven by registry
- `scripts/lib/receipt_terminal_detection.sh:16-40` — the bash track-to-T mapping being reformulated
