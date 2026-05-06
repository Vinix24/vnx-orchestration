# Feature: Phase 08 — W9 Universal WorkerProvider Protocol Refactor

**Status**: Draft
**Priority**: P0
**Branch**: `feat/w9-worker-provider-protocol`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate

Primary objective:
Replace the four bespoke per-vendor adapter modules (Claude, Codex, Gemini, Ollama) with a single `WorkerProvider` Protocol so every worker runtime is reachable through one contract. This is the structural prerequisite for capability tokens (W10), workers=N (W11), sub-orchestrators (W12), and the memory layer (W-MEM-1..4). Drives PRD-VNX-UH-001 §FR-1.

## Dependency Flow
```text
PR-0 (no dependencies)
PR-0 -> PR-1
PR-0 -> PR-2
PR-0 -> PR-3
PR-0 -> PR-4
PR-1, PR-2, PR-3, PR-4 -> PR-5
```

## PR-0: WorkerProvider Protocol Definition And Capability Surface
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Dependencies**: []

### Description
Define the `WorkerProvider` Protocol (typing.Protocol) that every adapter must satisfy: `dispatch()`, `stream_events()`, `cancel()`, `health_probe()`, `capabilities()`. Capability surface enumerates supported features (streaming, tool-use, context-rotation, embedding-only, headless-only, tmux-only) so the dispatcher can match dispatches to capable providers. Add a `WorkerCapability` enum and `ProviderRegistry` skeleton (no adapters registered yet — that comes in PR-1..PR-4).

### Scope
- `scripts/lib/worker_provider.py` — Protocol class, dataclasses for `DispatchSpec`, `DispatchResult`, `ProviderEvent`, `WorkerCapability` enum
- `scripts/lib/provider_registry.py` — registry skeleton, lookup by capability, lookup by name
- Type stubs and Protocol runtime check (`@runtime_checkable`)
- Documentation block describing migration contract for sub-PRs PR-1..PR-4

### Success Criteria
- `WorkerProvider` Protocol importable; mypy/pyright clean against it
- `ProviderRegistry.lookup(capability=...)` returns deterministic ordering
- No live adapter behavior change yet (registry empty)
- Protocol covers every operation currently performed by the four bespoke adapters

### Quality Gate
`gate_pr0_protocol_definition`:
- [ ] Protocol matches every method called on existing adapters (audit grep)
- [ ] `WorkerCapability` enum covers streaming, tool-use, headless, tmux, embedding, context-rotation
- [ ] Registry returns deterministic order under multiple-provider conditions
- [ ] Protocol is `@runtime_checkable` and rejects malformed mocks in unit test
- [ ] Doc block in module specifies semantic contract for adapter authors

## PR-1: Claude Adapter Migration To WorkerProvider
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 0.75 day
**Dependencies**: [PR-0]

### Description
Refactor `scripts/lib/subprocess_adapter.py` (Claude headless) to implement `WorkerProvider`. Mechanical migration — preserve every existing event shape, every existing receipt shape, every existing log line. Register adapter in registry under name `"claude_subprocess"`.

### Scope
- Refactor `SubprocessAdapter` class to satisfy Protocol
- Map existing methods onto Protocol surface (no behavior change)
- Register in `ProviderRegistry`
- Backwards-compat shim: existing call sites continue to import old name and get the migrated class

### Success Criteria
- `isinstance(SubprocessAdapter(...), WorkerProvider) is True`
- Existing T1 dispatch path (subprocess) produces byte-identical event NDJSON output (golden test)
- No new dependencies introduced

### Quality Gate
`gate_pr1_claude_adapter`:
- [ ] Existing T1 dispatch produces byte-identical event NDJSON
- [ ] Receipt schema unchanged
- [ ] Backwards-compat shim covers all known import paths (grep audit)

## PR-2: Codex Adapter Migration To WorkerProvider
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 0.75 day
**Dependencies**: [PR-0]

### Description
Refactor the Codex gate executor (`scripts/lib/codex_*.py`, codex CLI invocation modules) to implement `WorkerProvider`. Codex is currently gate-only; expose `WorkerCapability.GATE_ONLY` so the dispatcher does not route worker dispatches to it (until OD-1 closes recommending worker mode).

### Scope
- Extract Codex execution into `CodexProvider(WorkerProvider)` class
- Capability flags: `GATE_ONLY` initially
- Register in registry under name `"codex_cli"`
- Preserve existing gate report structure (no schema break)

### Success Criteria
- Codex gate runs through `ProviderRegistry.get("codex_cli").dispatch(...)`
- Existing review-gate result records unchanged
- `WorkerCapability.GATE_ONLY` blocks accidental worker routing

### Quality Gate
`gate_pr2_codex_adapter`:
- [ ] Existing codex gate runs unchanged (golden output diff)
- [ ] Routing test: dispatcher refuses to send a worker task to GATE_ONLY provider
- [ ] Receipt structure unchanged

## PR-3: Gemini Adapter Migration To WorkerProvider
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 0.75 day
**Dependencies**: [PR-0]

### Description
Refactor Gemini review-gate executor to implement `WorkerProvider`. Capability: `GATE_ONLY` plus `REVIEW`. Register under `"gemini_cli"`. Preserve normalized headless report writing path.

### Scope
- Extract Gemini execution into `GeminiProvider(WorkerProvider)` class
- Register in registry
- Preserve normalized headless report directory and file format
- Capability flags: GATE_ONLY, REVIEW

### Success Criteria
- Gemini review gate runs through registry lookup
- Normalized report file path and structure unchanged
- Result JSON contract_hash field still populated

### Quality Gate
`gate_pr3_gemini_adapter`:
- [ ] Existing gemini gate runs unchanged (golden diff)
- [ ] Normalized headless report path matches existing path conventions
- [ ] `contract_hash` populated in result JSON (regression of past failure)

## PR-4: Ollama Adapter Migration To WorkerProvider
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 0.75 day
**Dependencies**: [PR-0]

### Description
Refactor Ollama local-LLM adapter to implement `WorkerProvider`. Capability: `EMBEDDING`, `LOCAL_ONLY`, optionally `WORKER` (for nomic-embed-text and local-Sonnet-equivalents). Critical for memory layer prerequisite (W-MEM-1 needs a callable embedding provider).

### Scope
- Extract Ollama HTTP client into `OllamaProvider(WorkerProvider)` class
- Capability flags: EMBEDDING, LOCAL_ONLY, optional WORKER
- Register under `"ollama_local"`
- Embedding-only fast path for `nomic-embed-text`

### Success Criteria
- `OllamaProvider.embed(text)` returns 768-dim vectors for nomic-embed-text
- Health probe returns OK when daemon is up, FAIL otherwise (no exception leak)
- Adapter registered in registry

### Quality Gate
`gate_pr4_ollama_adapter`:
- [ ] Embedding pipeline returns correct vector dimensions
- [ ] Health probe failure produces structured error not exception
- [ ] Adapter is discoverable via `ProviderRegistry.lookup(EMBEDDING)`

## PR-5: Provider Registration, Discovery, And End-To-End Integration Tests
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Dependencies**: [PR-1, PR-2, PR-3, PR-4]

### Description
Wire the dispatcher and the review-gate runner to use `ProviderRegistry` instead of bespoke if/else adapter selection. Add provider-failover hook (preserves W7.5 behavior). Add discovery via env var `VNX_PROVIDER_REGISTRY_PATH` for additional providers. Comprehensive end-to-end test suite.

### Scope
- Replace bespoke adapter selection in dispatcher with `ProviderRegistry.resolve(dispatch)`
- Replace bespoke adapter selection in review-gate runner with same
- Plug-in discovery via env var
- E2E integration tests across all four providers
- Receipt processor verification: receipts still produce identical NDJSON

### Success Criteria
- Dispatcher routes T1 Claude → CodexProvider gate → GeminiProvider review → all via registry
- Provider-failover hook preserved (W7.5 contract intact)
- Receipt NDJSON unchanged byte-for-byte vs main pre-feature
- Plug-in discovery loads a stub provider from VNX_PROVIDER_REGISTRY_PATH and routes correctly

### Quality Gate
`gate_pr5_registry_integration`:
- [ ] Full T1 dispatch end-to-end via registry succeeds
- [ ] Codex gate end-to-end via registry succeeds
- [ ] Gemini gate end-to-end via registry succeeds
- [ ] Receipt diff vs main: zero changes (golden tests)
- [ ] Failover from Claude→Codex during streamed dispatch still works (W7.5 contract)
- [ ] Plug-in stub provider loads from env-var path
- [ ] CODEX GATE on this PR is mandatory feature-end gate

## Test Plan (Phase-Level)

### Unit Tests
- Protocol conformance: every adapter `isinstance(p, WorkerProvider)`
- Capability lookup determinism (sorted by name when multiple match)
- Registry singleton thread-safety (concurrent register/lookup)

### Integration Tests
- T1 Claude subprocess dispatch via registry → byte-identical event NDJSON
- Codex gate via registry → identical gate report
- Gemini review via registry → identical normalized report + result JSON
- Ollama embedding via registry → correct vector dimensions
- Mixed mission: Claude dispatches → Codex gates → Gemini reviews → all routed via registry

### Regression Tests
- Provider-failover (W7.5): Claude provider dies mid-stream → Codex picks up → cap-token chain (after W10) intact, mission continues
- Health-probe gates dispatcher: registry refuses to route to a provider whose health_probe returns FAIL

### Load / Soak Tests
- 50 concurrent dispatches across 2 providers — no registry corruption
- Registry hot-reload (plug-in path mutation) — no crashes, no leaked references

### Golden File Tests
- Capture pre-refactor event NDJSON for one Claude T1 dispatch and one Gemini review; assert byte-identical post-refactor
