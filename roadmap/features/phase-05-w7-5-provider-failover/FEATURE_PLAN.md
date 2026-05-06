# Feature: Phase 5 — W7.5 Provider Failover At Orchestrator Level

**Status**: Draft
**Priority**: P0
**Branch**: `feature/phase-05-w7-5-provider-failover`
**Risk-Class**: high (orchestrator runtime semantics)
**Merge-Policy**: human
**Review-Stack**: gemini_review (per PR); codex_gate on the feature-end PR (W7.5-E)
**Source**: PRD-VNX-UH-001 §5 FR-11 + §7.7; ROADMAP.md Phase 5

Primary objective:
When the primary provider (Opus) is down, the orchestrator restarts on the next available fallback (Codex -> Gemini -> ...). Trust chain survives via persistent ed25519 keys (cap-tokens may not exist yet at integration time; we mock the signature surface and freeze the schema). Mission state survives via a summary checkpoint. Live conversation context is acceptably lost.

Wave breakdown follows the FR-11 §11.8 LOC budget but is split into independently-deployable sub-PRs so each ships at <300 LOC and is reviewable in isolation.

## Dependency Flow
```text
W7.5-A (provider_health probe)
  -> W7.5-B (provider_chain resolver)
       -> W7.5-C (checkpoint_writer)
            -> W7.5-D (receipt schema additions)
                 -> W7.5-E (failover integration + tests)  [feature-end]
```

W7.5-A and W7.5-B are independent libraries; W7.5-C writes the artifact that W7.5-E consumes; W7.5-D extends the receipt envelope so failover events are durable; W7.5-E wires it all together inside the dispatcher and ships the failover scenario test.

## PR-W7.5-A: Provider Health Probe
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 day
**Dependencies**: []

### Description
Per-provider health-check probe + ring buffer. Runs every `health_check_interval` (default 60s) and writes results to `.vnx-data/state/provider_health.ndjson`. Library only — no failover decisions yet. Sonnet is sufficient: well-specified per-provider probes from PRD §11.2.

### Scope
- New `scripts/lib/provider_health.py` (~120 LOC):
  - `class ProviderHealthProbe` with per-provider command tables (PRD §11.2 matrix).
  - Probe commands: claude `--version` + `-p --dry-run "ping"`; codex `--version` + cached `auth status`; gemini `--version` + `--dry-run`; ollama `curl /api/tags`; litellm `--health-check`.
  - Ring buffer + archive: mirror `events/T{n}.ndjson` pattern; live file at `.vnx-data/state/provider_health.ndjson`, archive at `.vnx-data/state/provider_health/archive/{date}.ndjson`.
  - Threshold tracking: `consecutive_failures_for(provider) -> int`.

### Files to Create/Modify
- Create: `scripts/lib/provider_health.py`
- Create: `.vnx-data/state/provider_health/` directory in `.gitignore`
- Tests: `tests/unit/test_provider_health.py`, `tests/integration/test_health_probe_real_subprocess.py`

### Success Criteria
- Probe records one health entry per `(provider, interval)` tick.
- Ring buffer truncates after archival; archive grows per day.
- `consecutive_failures_for("claude")` returns the count since last success.

### Test Plan
- **Unit**: Probe each provider with mocked `subprocess.run`; assert structured health entry shape; assert consecutive-failure counter advances and resets correctly.
- **Integration (boots real subprocess)**: Run `claude --version`, `codex --version`, `gemini --version`, and `curl localhost:11434/api/tags` (skip if not installed); assert real entries land in the ring buffer.
- **Negative**: Mock unreachable provider; assert failure entry written with structured error reason; counter advances.
- **Smoke**: Spin probe daemon for 3s with 1s interval; assert ≥3 entries; clean shutdown on SIGTERM.

### Quality Gate
`gate_pr_w7_5_a_health_probe`:
- [ ] All five provider probes implemented per PRD §11.2.
- [ ] Ring buffer + archive pattern matches existing `events/` convention.
- [ ] Consecutive-failure counter is correct under fail/recover sequences.
- [ ] No subprocess hangs past `chunk_timeout` even when provider is unreachable.

## PR-W7.5-B: Provider Chain Resolver
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Dependencies**: [PR-W7.5-A]

### Description
Pure-function chain resolver: given a `runtime.yaml` (PRD §11.1) and the current health-probe state, decide whether to flip to a fallback. No I/O; library function only. Sonnet is sufficient: stateless decision logic with clear inputs.

### Scope
- New `scripts/lib/provider_chain.py` (~100 LOC):
  - `parse_runtime_yaml(path) -> ProviderChainConfig`.
  - `resolve_active_provider(config, health_state, last_active, now) -> ResolveDecision` returning `(provider, reason)` where `reason ∈ {primary_healthy, primary_failed_threshold, in_cooldown, fallback_active, all_unavailable}`.
  - Cooldown enforcement (`cooldown_before_reflip`, default 300s).
  - Threshold enforcement (`consecutive_failures_before_flip`, default 3).

### Files to Create/Modify
- Create: `scripts/lib/provider_chain.py`
- Tests: `tests/unit/test_provider_chain.py`

### Success Criteria
- Resolver returns `primary_healthy` when probe shows 0 consecutive failures.
- Resolver returns `primary_failed_threshold` when probe shows ≥`consecutive_failures_before_flip`.
- Cooldown blocks flip-back even after primary recovers.
- `all_unavailable` returned when no provider in chain is healthy.

### Test Plan
- **Unit**: 12 table-driven cases covering: 0/1/2/3+ consecutive failures, cooldown active/expired, fallback healthy/down, fallback exhausted, manual-only trigger, every state-handoff strategy.
- **Integration**: Compose with W7.5-A probe; simulate provider down via mocked health entries; assert resolver flips at threshold.
- **Smoke**: `parse_runtime_yaml` against the example YAML in PRD §11.1 produces a valid config object.
- **Negative**: Malformed runtime.yaml produces structured validation error, not exception.

### Quality Gate
`gate_pr_w7_5_b_chain_resolver`:
- [ ] All 12 table-driven decision cases pass.
- [ ] Resolver is pure (no I/O; takes health state as input).
- [ ] Cooldown logic correct under flip / recover / re-flip sequences.
- [ ] Malformed config produces typed errors, not exceptions.

## PR-W7.5-C: Summary Checkpoint Writer
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 day
**Dependencies**: []

### Description
Writes `.vnx-data/missions/<mission_id>/checkpoints/<timestamp>.md` summary checkpoints for orchestrators. The checkpoint is the only artifact that crosses the failover boundary, so its design choices determine how lossy failover is. Opus assigned per operator instruction: checkpoint design is the load-bearing decision in the whole feature.

### Scope
- New `scripts/lib/checkpoint_writer.py` (~80 LOC):
  - `write_checkpoint(mission_id, summary_md, ts) -> Path`.
  - `latest_checkpoint(mission_id) -> Path | None`.
  - Checkpoint cadence trigger: every N turns or every M minutes (whichever first; PRD default 10 turns / 5 minutes).
  - Atomic write: `tempfile + rename` to avoid mid-write reads on failover.
- Schema for the checkpoint markdown body (must round-trip):
  - YAML frontmatter: `mission_id`, `orchestrator_id`, `provider_at_write`, `turn_count`, `cap_token_id` (mock if cap-tokens not yet implemented; clearly noted).
  - Body sections: "Mission objective", "Decisions so far", "Open items", "Last user message", "Next action expected".

### Files to Create/Modify
- Create: `scripts/lib/checkpoint_writer.py`
- Create: `.vnx-data/missions/` (gitignored)
- Tests: `tests/unit/test_checkpoint_writer.py`, `tests/integration/test_checkpoint_round_trip.py`

### Success Criteria
- `write_checkpoint` is atomic (no partial files visible to readers).
- `latest_checkpoint` returns the most recent file by lexical timestamp.
- Frontmatter parses back via PyYAML round-trip; body sections detected by header regex.
- Checkpoint at `t` is always readable at `t+epsilon` (no race window).

### Test Plan
- **Unit**: Atomic-write test (concurrent writer+reader; reader never sees partial content). Lexical-order test for `latest_checkpoint`. Frontmatter round-trip test. Section-detection regex test.
- **Integration**: Write 5 checkpoints with 100ms intervals; assert all 5 land; assert `latest_checkpoint` returns the 5th.
- **Smoke**: Frontmatter validates against documented schema; body has all 5 required sections.
- **Negative**: Write to a read-only directory; assert structured error, not silent loss.

### Quality Gate
`gate_pr_w7_5_c_checkpoint_writer`:
- [ ] Atomic writes verified under concurrent access.
- [ ] `latest_checkpoint` is correct under tight write cadence.
- [ ] Frontmatter + body schema documented + validated.
- [ ] cap_token_id field present (mocked OK; explicitly noted in module docstring).
- [ ] Triggers on N-turns OR M-minutes whichever first.

## PR-W7.5-D: Receipt Schema Additions
**Track**: A
**Priority**: P0
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Dependencies**: []

### Description
Extend the receipt envelope with `provider_chain_at_dispatch`, `active_provider_at_completion`, and `failover_events` fields per PRD §11.7. Sonnet is sufficient: schema additions only.

### Scope
- Modify `scripts/lib/receipt_envelope.py` (or equivalent):
  - Optional `provider_chain_at_dispatch: list[str]` (e.g. `["claude/opus", "codex/gpt-5.3-codex"]`).
  - Optional `active_provider_at_completion: str`.
  - Optional `failover_events: list[FailoverEvent]` where `FailoverEvent` carries `ts`, `from`, `to`, `trigger`, `checkpoint_path`.
- Add receipt-validator support so processors don't reject receipts that include these fields.
- Backward-compat: receipts without these fields validate exactly as before.

### Files to Create/Modify
- Modify: `scripts/lib/receipt_envelope.py`
- Modify: `scripts/lib/receipt_processor.py` validator (accept new optional fields)
- Tests: `tests/unit/test_receipt_failover_fields.py`

### Success Criteria
- Receipts with the new fields validate.
- Receipts without the new fields still validate (backward compat).
- Receipt processor surfaces failover events into NDJSON audit trail.

### Test Plan
- **Unit**: Construct receipt with empty `failover_events` -> validates. Construct receipt with one failover event -> validates and round-trips. Construct receipt without any new fields -> validates (backward compat).
- **Integration**: Push a receipt carrying a failover event through `receipt_processor.py`; assert NDJSON entry contains the failover surface.
- **Smoke**: All existing receipt fixtures in `tests/` still validate post-change.

### Quality Gate
`gate_pr_w7_5_d_receipt_schema`:
- [ ] New fields are all optional (no existing receipt rejected).
- [ ] FailoverEvent shape matches PRD §11.7 exactly.
- [ ] Receipt processor surfaces failover into NDJSON.
- [ ] All existing receipt fixtures still validate.

## PR-W7.5-E: Failover Integration + Scenario Tests
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1.5 days
**Dependencies**: [PR-W7.5-A, PR-W7.5-B, PR-W7.5-C, PR-W7.5-D]

### Description
Wire health probe + chain resolver + checkpoint writer + receipt schema into the dispatcher's orchestrator-spawn path. End-to-end integration. This is the feature-end PR for Phase 5 — carries `codex_gate` per operator policy. Sonnet sufficient: all heavy design lives in the prior waves; this glues them together with explicit branching.

### Scope
- Modify dispatcher / orchestrator-spawn path:
  - At dispatch start, call `provider_chain.resolve_active_provider(...)` to pick provider.
  - Record `provider_chain_at_dispatch` on the receipt envelope.
  - On health-probe-detected failure during a running mission, call `checkpoint_writer.write_checkpoint(...)` then restart orchestrator on the resolved fallback with `--instruction "$(cat latest_checkpoint.md)"`.
  - Append a `FailoverEvent` to the receipt's `failover_events` list.
- Honor `state_handoff` strategy from runtime.yaml: `summary_checkpoint` (default), `fresh`, `warn_operator`.
- Cap-token chain: signature mock used in this wave (real cap-tokens land in W10). The mock must produce a deterministic signature so the failover scenario test can assert chain-unbroken without signing infrastructure.

### Files to Create/Modify
- Modify: dispatcher / `subprocess_dispatch.py` orchestrator entry path
- Modify: `scripts/lib/runtime_facade.py` (load runtime.yaml at orchestrator boot)
- Create or extend: `scripts/lib/cap_token.py` (mock signature stub clearly marked TEMPORARY pending W10)
- Tests: `tests/integration/test_failover_scenario.py`, `tests/integration/test_state_handoff_strategies.py`, `tests/integration/test_failover_no_data_loss.py`

### Success Criteria
- Killing the primary provider mid-mission causes the orchestrator to resume on the next healthy fallback within ≤1 health-check interval after threshold trip.
- Mission state survives via the latest summary checkpoint.
- Receipt records both `provider_chain_at_dispatch` and the `failover_events` list.
- `state_handoff: fresh` aborts cleanly with a structured failure receipt.
- `state_handoff: warn_operator` blocks until operator acknowledges.
- Cap-token mock chain validates pre/post failover (mock signature stable across the boundary).

### Test Plan
- **Unit**: Honor each `state_handoff` strategy in isolation against a mock orchestrator. Assert correct action: write_checkpoint+restart, abort, or pause.
- **Integration — failover scenario (mandatory, the feature's signature test)**: Spawn an orchestrator dispatch using a fake primary-provider subprocess that exits non-zero after 5s. Assert: (1) probe detects failure within `health_check_interval`; (2) chain resolver returns `primary_failed_threshold` after `consecutive_failures_before_flip`; (3) checkpoint_writer writes a summary; (4) orchestrator restarts on the configured fallback; (5) restart subprocess receives the checkpoint as its first instruction; (6) cap-token mock chain still validates after the flip; (7) receipt envelope contains `failover_events` with one entry whose `from`/`to`/`checkpoint_path` are correct.
- **Integration — `state_handoff: fresh`**: Same scenario with `fresh`; assert mission aborts, no restart attempted, receipt records abort reason.
- **Integration — `state_handoff: warn_operator`**: Same scenario; assert mission goes to `paused`, no restart until a manual ack file is dropped, receipt records `paused`.
- **Negative — all-unavailable**: All providers in chain return failed health; assert resolver returns `all_unavailable`, dispatcher emits `failure` receipt with structured reason, no infinite restart loop.
- **No-data-loss**: During failover, the in-flight tool result must be in the checkpoint or in the prior receipt's archive. Assert at least one of the two paths preserves the artifact.
- **Smoke**: Single-provider runtime.yaml (no fallbacks) behaves identically to pre-W7.5 dispatcher.

### Quality Gate
`gate_pr_w7_5_e_failover_integration`:
- [ ] Failover scenario test passes end-to-end (kill primary -> restart on fallback with checkpoint).
- [ ] All three `state_handoff` strategies (`summary_checkpoint`, `fresh`, `warn_operator`) implemented and tested.
- [ ] Cap-token mock chain remains valid across failover boundary.
- [ ] No-data-loss invariant verified: in-flight tool results preserved in checkpoint OR archive.
- [ ] `all_unavailable` produces structured failure receipt, no restart loop.
- [ ] Single-provider config (no fallbacks) is byte-identical to pre-W7.5 behavior.
- [ ] Codex gate verdict: pass (final mode).
- [ ] Cap-token mock is documented as TEMPORARY with explicit pointer to W10.
