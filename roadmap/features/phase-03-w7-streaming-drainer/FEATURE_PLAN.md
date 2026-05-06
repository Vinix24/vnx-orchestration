# Feature: Phase 3 — Universal Harness W7 Streaming Drainer

**Status**: Draft
**Priority**: P0
**Branch**: `feature/phase-03-w7-streaming-drainer`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Source**: PRD-VNX-UH-001 §5 FR-2, FR-3, §8.2; ROADMAP.md Phase 3; `claudedocs/2026-05-01-universal-harness-research.md` §2

Primary objective:
Close the codex+gemini observability gap so that every provider's per-event stream lands in `events/T{n}.ndjson` and `events/archive/{worker_id}/{dispatch_id}.ndjson` live (not post-hoc). Introduce `CanonicalEvent` schema with `observability_tier`, ship a shared `_streaming_drainer.py` mixin, migrate Codex / Gemini-CLI / Ollama adapters, add `LiteLLMAdapter` proof-of-concept, and gate dispatches by minimum observability tier per governance variant.

## Dependency Flow
```text
W7-A (CanonicalEvent + EventStore API)
  -> W7-B (StreamingDrainer mixin)
       -> W7-C (Codex adapter migration)
       -> W7-D (Gemini-CLI adapter migration)
       -> W7-E (LiteLLMAdapter PoC)
       -> W7-F (OllamaAdapter audit + refactor)
            -> W7-G (tier labeling + governance gating)  [feature-end]
```

W7-A is foundational and gates W7-B. W7-B is a mixin that all four adapter waves compose. W7-C/D/E/F can run in parallel after W7-B lands. W7-G is the wrap-up wave that tags receipts with `observability_tier` and enforces `min_observability_tier` from governance variants.

## PR-W7-A: CanonicalEvent Schema + EventStore API
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 day
**Dependencies**: []

### Description
Introduce the `CanonicalEvent` dataclass per PRD FR-3 plus the public `EventStore` API methods that all adapters will call. Foundational — every later wave reuses this contract. Opus is required because the schema is reused everywhere; subtle field choices ripple across receipts, NDJSON consumers, and SSE.

### Scope
- `scripts/lib/canonical_event.py` (new): `CanonicalEvent` dataclass with `timestamp`, `dispatch_id`, `worker_id`, `sequence`, `type`, `provider`, `raw`, `normalized`, `observability_tier` fields.
- Extend `scripts/lib/event_store.py`: ensure `append(canonical_event)` accepts the new shape and writes to ring buffer + archive atomically.
- Backward-compat shim: legacy `dict` events accepted via `CanonicalEvent.from_legacy(provider, dict)`.
- Add `observability_tier: Literal[1, 2, 3]` field on receipt envelope schema (typed but optional in this wave; enforced in W7-G).

### Files to Create/Modify
- Create: `scripts/lib/canonical_event.py`
- Modify: `scripts/lib/event_store.py`
- Modify: `scripts/lib/receipt_envelope.py` (add optional `observability_tier`)
- Tests: `tests/unit/test_canonical_event.py`, `tests/unit/test_event_store_canonical.py`

### Success Criteria
- `CanonicalEvent.to_dict()` round-trips through `EventStore.append` and `from_legacy()` for every event type currently produced by `subprocess_adapter.py`.
- Existing T0/T1 dispatches continue to emit valid NDJSON with no schema regression.
- Receipt envelopes accept optional `observability_tier` without breaking existing receipts.

### Test Plan
- **Unit**: Construct one `CanonicalEvent` per type literal; assert serialization stable; assert `from_legacy` accepts the existing seven dashboard types (`init/thinking/tool_use/tool_result/text/result/error`); assert `observability_tier` validates to {1,2,3}.
- **Integration**: Run an existing T1 subprocess dispatch end-to-end; verify the produced `events/T1.ndjson` lines parse as `CanonicalEvent` and the receipt parses with optional `observability_tier`.
- **Smoke**: `python3 scripts/build_t0_state.py` after one dispatch; confirm `t0_state.json` reads receipts unchanged.

### Quality Gate
`gate_pr_w7_a_canonical_event`:
- [ ] `CanonicalEvent` dataclass instantiates and round-trips for all 8 type literals.
- [ ] `EventStore.append` writes a canonical event to ring buffer AND archive in one atomic step.
- [ ] Legacy dict events convert via `from_legacy` without data loss.
- [ ] Receipt envelopes parse with `observability_tier` present or absent.
- [ ] Existing Claude (T1) dispatch produces unchanged user-visible behavior.

## PR-W7-B: StreamingDrainer Mixin
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
**Dependencies**: [PR-W7-A]

### Description
Extract Claude's line-by-line drain pattern (`subprocess_adapter.py:416-526`) into a reusable mixin `_streaming_drainer.py`. Concurrency-sensitive: must handle stall detection, partial lines across chunks, EOF, process termination, and signal interruption without dropping events or deadlocking. Opus assigned because subtle bugs in the drainer become cross-adapter footguns.

### Scope
- New `scripts/lib/adapters/_streaming_drainer.py` (~120 LOC): `StreamingDrainerMixin` with `read_events_with_timeout()`, parameterized by `event_normalizer: Callable[[dict], list[CanonicalEvent]]` and `provider_name: str`.
- Stall detection: configurable `chunk_timeout` and `total_deadline`.
- Crash safety: drainer reports terminated subprocess via a final `error` event when exit code is non-zero AND no `result` event was emitted.
- Per-event `EventStore.append` call (writes during streaming, not post-hoc).

### Files to Create/Modify
- Create: `scripts/lib/adapters/_streaming_drainer.py`
- Modify: `scripts/lib/subprocess_adapter.py` (refactor to compose the mixin; behavior unchanged for Claude path)
- Tests: `tests/unit/test_streaming_drainer.py`, `tests/integration/test_drainer_claude_parity.py`

### Success Criteria
- `subprocess_adapter.py` Claude path delivers identical event stream pre/post refactor (golden-file diff is empty).
- Drainer correctly handles partial JSON lines split across stdout chunks.
- Drainer emits a synthetic `error` event when subprocess exits non-zero before `result`.
- No deadlock when subprocess produces no output for `chunk_timeout` seconds.

### Test Plan
- **Unit**: Feed simulated stdout streams (split lines, partial JSON, EOF mid-line, malformed line); assert drainer recovers cleanly. Mock subprocess that exits with no output; assert synthetic `error` event. Mock subprocess that hangs; assert `chunk_timeout` raises a stall.
- **Integration**: Run real Claude T1 dispatch; capture `events/T1.ndjson`; diff against pre-refactor golden file (must match byte-for-byte modulo timestamps).
- **Smoke**: Cancel a running drainer mid-stream via SIGTERM to subprocess; assert no orphan threads, EventStore is consistent, archive file flushed.

### Quality Gate
`gate_pr_w7_b_streaming_drainer`:
- [ ] Mixin handles partial JSON lines without dropping events.
- [ ] Subprocess crash mid-stream emits synthetic `error` event with non-zero exit code in `raw`.
- [ ] Claude T1 path produces byte-identical event stream pre/post refactor (timestamps masked).
- [ ] No deadlock under `chunk_timeout` exhaustion.
- [ ] EventStore writes happen DURING streaming (verify via `tail -f` while dispatch runs).

## PR-W7-C: Codex Adapter Migration to Streaming
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
**Dependencies**: [PR-W7-B]

### Description
Migrate `scripts/lib/adapters/codex_adapter.py` from buffered post-hoc parse (lines 90-184) to live streaming via the W7-B mixin. Closes the 115-LOC observability gap documented in `claudedocs/2026-05-01-universal-harness-research.md` §2.2. Sonnet is sufficient: well-specified migration with a known-good reference (Claude path).

### Scope
- Replace `_drain_with_stall_detection` + `_parse_ndjson` with `StreamingDrainerMixin.read_events_with_timeout(event_normalizer=_normalize_codex_event)`.
- New `_normalize_codex_event()` mapping (~50 LOC):
  - `thread.started` -> `init`
  - `item.completed[type=agent_message]` -> `text`
  - `item.{started,updated,completed}[type=command_execution]` -> `tool_use` / `tool_result`
  - `error` -> `error`
  - `turn.completed` -> `result` with `token_count` payload
- True `stream_events()` that yields during execution (not after).
- Tier label: Codex emits Tier-1 events post-migration.

### Files to Create/Modify
- Modify: `scripts/lib/adapters/codex_adapter.py` (~115 LOC change)
- Tests: `tests/unit/test_codex_event_normalizer.py`, `tests/integration/test_codex_streaming.py`, `tests/integration/test_codex_crash_negative.py`

### Success Criteria
- Live `events/T{n}.ndjson` accumulates Codex events during a dispatch (verified via `tail -f` mid-run).
- Codex events appear in `events/archive/{worker_id}/{dispatch_id}.ndjson` post-completion.
- Existing Codex gate path (`gate_runner`) continues to work — verdict extraction unchanged.
- Token usage telemetry is emitted incrementally, not as a single post-hoc value.

### Test Plan
- **Unit**: Feed each documented Codex NDJSON event type into `_normalize_codex_event`; assert correct `CanonicalEvent` produced. Cover all five `item.*` subtypes, `turn.completed` token totals, `error` malformed-tool case.
- **Integration (boots real subprocess)**: Run `codex exec --json` on a known fixture prompt; assert `events/T{n}.ndjson` accumulates events live; assert event count matches a manual NDJSON line count of the same run; assert observability_tier=1 on every event.
- **Negative (crash mid-stream)**: Spawn `codex exec --json` then `kill -9` mid-run; assert drainer reports synthetic `error` event, no data loss in archive, exit code recorded in receipt, no orphan EventStore handles.
- **Cross-adapter parity**: Same dispatch instruction via Claude vs Codex; assert event count differs <20% and both produce the same set of `type` values (`init`, `tool_use`, `tool_result`, `text`, `result`).
- **Smoke**: Existing Codex gate dispatch completes; `gate_runner` verdict unchanged.

### Quality Gate
`gate_pr_w7_c_codex_streaming`:
- [ ] Live Codex events visible in `events/T{n}.ndjson` during dispatch (not only after).
- [ ] All Codex NDJSON event types map to a `CanonicalEvent` type with no unknown drops.
- [ ] Codex archive file is non-empty after every dispatch.
- [ ] Crash mid-stream produces a recoverable receipt (no data loss).
- [ ] Codex gate verdict path is byte-identical to pre-migration.

## PR-W7-D: Gemini-CLI Adapter Migration to Streaming
**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Dependencies**: [PR-W7-B, PR-W7-C]

### Description
Mirror W7-C for Gemini-CLI. Switch flag from `--output-format json` to `--output-format stream-json` and reuse the mixin. Gated behind `VNX_GEMINI_STREAM=1` until v0.11+ is proven on the operator's machine. Sonnet is sufficient: this is a near-mechanical mirror of W7-C with a smaller event-mapping table.

### Scope
- Modify `scripts/lib/adapters/gemini_adapter.py` (~55 LOC):
  - Add env-gated flag flip: `--output-format stream-json` when `VNX_GEMINI_STREAM=1`, else current `--output-format json` behavior.
  - New `_normalize_gemini_event()` mapping for `init / message / tool_use / tool_result / result` event types.
  - Compose `StreamingDrainerMixin` only on the streaming branch.
- Tier label: Gemini-CLI emits Tier-1 in streaming branch, Tier-3 (final-only) in legacy branch.

### Files to Create/Modify
- Modify: `scripts/lib/adapters/gemini_adapter.py`
- Tests: `tests/unit/test_gemini_event_normalizer.py`, `tests/integration/test_gemini_streaming_gated.py`, `tests/integration/test_gemini_crash_negative.py`

### Success Criteria
- With `VNX_GEMINI_STREAM=0` (default), behavior is byte-identical to pre-migration.
- With `VNX_GEMINI_STREAM=1`, live events accumulate in `events/T{n}.ndjson`.
- Vertex AI REST path (`vertex_ai_runner.py`) is untouched.

### Test Plan
- **Unit**: Each Gemini stream-json event type maps to `CanonicalEvent`. Default-off path delegates to legacy `_parse_response`.
- **Integration (boots real subprocess)**: With `VNX_GEMINI_STREAM=1`, run `gemini --output-format stream-json` against fixture prompt; assert live event accumulation; assert observability_tier=1.
- **Negative**: Kill `gemini` subprocess mid-stream; assert synthetic `error` event, archive flushed, no data loss.
- **Cross-adapter parity**: Same dispatch via Claude / Codex / Gemini-streaming emits comparable event count (within 30%) and same set of canonical types.
- **Default-off**: `VNX_GEMINI_STREAM` unset; assert pre-migration behavior preserved (one synthetic `result` event with Tier-3 label).

### Quality Gate
`gate_pr_w7_d_gemini_streaming`:
- [ ] `VNX_GEMINI_STREAM=0` default preserves existing behavior.
- [ ] `VNX_GEMINI_STREAM=1` emits live events into `events/T{n}.ndjson`.
- [ ] Crash mid-stream produces recoverable receipt.
- [ ] Vertex AI REST path unchanged.
- [ ] Cross-adapter parity test passes (Claude / Codex / Gemini comparable).

## PR-W7-E: LiteLLMAdapter Proof-of-Concept
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 day
**Dependencies**: [PR-W7-B]

### Description
New adapter routing through a local `litellm` shim subprocess to reach Bedrock / Mistral / Vertex / Azure / Groq via one OpenAI-shaped surface. Proof-of-concept scope only — does NOT replace any existing adapter. Sonnet is sufficient: well-specified new adapter following the established adapter contract.

### Scope
- New `scripts/lib/adapters/litellm_adapter.py` (~150 LOC):
  - Spawn `litellm-cli` (or `python -m litellm` if no CLI) with OpenAI-shaped streaming.
  - Compose `StreamingDrainerMixin` with `_normalize_litellm_event()` (OpenAI SSE chunks -> CanonicalEvent).
  - Provider chain string format: `litellm/<provider>/<model>` (e.g. `litellm/bedrock/claude-sonnet-4-6`).
- Capability declaration: `CODE`, `REVIEW` (no `ORCHESTRATE` for v0).
- Health probe stub: `litellm --health-check`.

### Files to Create/Modify
- Create: `scripts/lib/adapters/litellm_adapter.py`
- Modify: `scripts/lib/provider_adapter.py` (register adapter)
- Tests: `tests/unit/test_litellm_event_normalizer.py`, `tests/integration/test_litellm_bedrock_smoke.py` (skip if no creds), `tests/integration/test_litellm_crash_negative.py`

### Success Criteria
- A `provider: litellm/bedrock/claude-sonnet-4-6` dispatch reaches Bedrock and emits live events.
- Adapter cleanly skips with a structured "credentials missing" error if Bedrock/Vertex creds absent.
- Tier-1 observability when streaming SSE works; Tier-2 when only `data: [DONE]` is reachable.

### Test Plan
- **Unit**: OpenAI SSE chunk parser handles `data: {...}\n\n`, `data: [DONE]`, malformed chunks, and `[DONE]` without prior content.
- **Integration (boots real subprocess, optional)**: With Bedrock creds available, run a Sonnet dispatch via litellm; assert events stream live. Mark test `pytest.mark.requires_aws` so CI skips when no creds.
- **Negative**: Run litellm subprocess against an unreachable endpoint; assert structured error event, no hang past `chunk_timeout`.
- **Cross-adapter parity**: With creds, same dispatch via Claude direct vs `litellm/anthropic/claude-sonnet-4-6` produces comparable event count + types.
- **Smoke**: Without creds, assert adapter raises a typed exception that `provider_chain.py` (W7.5 future) can catch as `unavailable`.

### Quality Gate
`gate_pr_w7_e_litellm`:
- [ ] Adapter parses OpenAI-shaped SSE into `CanonicalEvent`.
- [ ] Bedrock smoke test passes when creds present, skips cleanly when absent.
- [ ] Crash / unreachable endpoint produces structured error.
- [ ] Tier label correctly set per streaming capability.
- [ ] Provider chain string format `litellm/<provider>/<model>` accepted by registry.

## PR-W7-F: OllamaAdapter Audit + Streaming Refactor
**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Dependencies**: [PR-W7-B]

### Description
Audit existing `scripts/lib/adapters/ollama_adapter.py` for streaming completeness and refactor onto the W7-B mixin. Ollama already supports `stream: true` over its HTTP API; adapter likely buffers today. Sonnet is sufficient: routine refactor with HTTP-streaming reference docs.

### Scope
- Audit `ollama_adapter.py` for buffered vs streaming reads.
- If buffered: refactor to compose `StreamingDrainerMixin` (HTTP-line variant, not stdout) with `_normalize_ollama_event()` mapping `{message, done, eval_count}` to `CanonicalEvent`.
- Tier label: Ollama emits Tier-2 (text-only streaming, no tool_use parity for non-tool-trained local models); Tier-1 if model supports OpenAI tool-use shape (e.g. llama3.1-tools).

### Files to Create/Modify
- Modify: `scripts/lib/adapters/ollama_adapter.py` (~70 LOC change)
- Tests: `tests/unit/test_ollama_event_normalizer.py`, `tests/integration/test_ollama_streaming.py`, `tests/integration/test_ollama_crash_negative.py`

### Success Criteria
- Ollama dispatch emits live events into `events/T{n}.ndjson` (Tier-2 baseline; Tier-1 when tool-use detected).
- Existing Ollama paths (digest worker, local fallback) keep working.

### Test Plan
- **Unit**: Each Ollama HTTP-stream chunk type maps to a `CanonicalEvent`. `done: true` produces `result` with `eval_count` token telemetry.
- **Integration (boots real subprocess)**: With local Ollama running and a small model pulled (e.g. `qwen2.5-coder:0.5b`), run a known prompt; assert live events; assert observability_tier=2 unless tool-use is detected.
- **Negative**: Kill Ollama daemon mid-stream; assert synthetic error event; assert connection-refused handled.
- **Cross-adapter parity**: Same dispatch via Claude / Ollama; event count differs (Ollama smaller, OK), but both produce `result` with token telemetry.
- **Smoke**: `ollama list` reachable on localhost:11434; if not, integration tests skip gracefully.

### Quality Gate
`gate_pr_w7_f_ollama`:
- [ ] Ollama produces live events when daemon is reachable.
- [ ] Tier labeling differentiates tool-use-capable vs text-only models.
- [ ] Daemon-down case produces structured error, no hang.
- [ ] Existing Ollama callers (digest worker if any) unchanged.

## PR-W7-G: Tier Labeling + Governance Gating
**Track**: B
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 0.5 day
**Dependencies**: [PR-W7-C, PR-W7-D, PR-W7-E, PR-W7-F]

### Description
Wrap-up wave: tag every receipt with the producing adapter's `observability_tier` and enforce the `min_observability_tier` field declared in each governance variant. This is the feature-end PR for Phase 3, so it carries `codex_gate` per operator policy. Risk class is medium because it gates dispatch admission. Sonnet is sufficient: the heavy semantics are in the prior waves; this wires them up.

### Scope
- Add receipt-write hook that copies the dispatch's effective tier (resolved from adapter capability + governance variant) into receipt envelope `observability_tier`.
- Implement `gate_stack_resolver.py`-adjacent logic that rejects a dispatch when `governance.yaml.min_observability_tier > adapter.observability_tier`.
- CLI sanity command: `vnx observability tiers` lists each registered adapter and its current tier.
- Update `coding-strict` variant default to `min_observability_tier: 1`; `business-light` default to `min_observability_tier: 2`.

### Files to Create/Modify
- Modify: `scripts/lib/adapters/_streaming_drainer.py` (declare `provider_observability_tier`)
- Modify: each adapter (set `observability_tier` constant)
- Modify: `scripts/lib/receipt_envelope.py` (require `observability_tier`)
- Modify: `scripts/lib/gate_stack_resolver.py` or equivalent dispatch admission point
- Create: `scripts/vnx_observability_cli.py`
- Tests: `tests/unit/test_observability_gating.py`, `tests/integration/test_tier_blocked_dispatch.py`

### Success Criteria
- Every receipt produced post-merge carries a non-null `observability_tier` field.
- A dispatch routed to a Tier-2 adapter under a `min_observability_tier: 1` variant is rejected with a structured error.
- `vnx observability tiers` lists Claude=1, Codex=1, Gemini-streaming=1/legacy=3, LiteLLM=1or2, Ollama=1or2.

### Test Plan
- **Unit**: Tier resolution given (adapter_tier=2, variant_min=1) returns `reject`; (adapter_tier=1, variant_min=1) returns `allow`.
- **Integration**: Trigger a dispatch under `coding-strict` to a deliberately-Tier-2 mock adapter; assert dispatcher refuses to spawn and writes a structured rejection receipt.
- **Smoke**: `vnx observability tiers` returns a populated table; matches the adapter constants.
- **Cross-adapter parity (final)**: Run identical instruction across Claude / Codex / Gemini-stream / Ollama; receipts all carry `observability_tier`; receipt schema validates uniformly.

### Quality Gate
`gate_pr_w7_g_tier_gating`:
- [ ] Every adapter declares `observability_tier` constant.
- [ ] Receipts carry `observability_tier` (required, not optional).
- [ ] Min-tier violation produces structured rejection BEFORE subprocess spawn.
- [ ] CLI lists all adapters with tiers.
- [ ] Default governance variants ship with sensible `min_observability_tier`.
- [ ] Cross-adapter parity test green (all four adapters emit comparable canonical streams).
