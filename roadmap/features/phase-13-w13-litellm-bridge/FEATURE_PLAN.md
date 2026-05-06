# Feature: Phase 13 / W13 — LiteLLM Bridge + Ollama Parity + 1st PyPI Live

**Status**: Draft
**Priority**: P0
**Branch**: `feat/w13-litellm-ollama-pypi`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Roadmap Wave**: w13
**Depends-On (roadmap)**: w9 (universal `WorkerProvider`), w6d (PyPI publish module)

Primary objective:
Promote the W7-E `LiteLLMAdapter` proof-of-concept into a production-grade provider bridge (Bedrock, Vertex, Mistral, Groq reachable via a thin `litellm-cli` subprocess shim), close the final Ollama parity gap, and use that capability to coordinate-test the W6D PyPI publish of `headless-context-rotation` as the first community module live.

R9 dependency-bloat constraint: LiteLLM transitive deps MUST stay outside VNX's main dep tree. The shim is a subprocess invocation only.

## Dependency Flow
```text
PR-A (litellm-cli-shim)              <- foundation, no deps
PR-A -> PR-B (litellm-adapter-promotion)
PR-B -> PR-C (ollama-parity-final-pass)
PR-B -> PR-D (bedrock-validation)
PR-B -> PR-E (vertex-validation)
PR-B -> PR-F (mistral-validation)
PR-B -> PR-G (ci-integration-tests)
PR-C, PR-D, PR-E, PR-F -> PR-H (pypi-headless-context-rotation-publish)
PR-A..PR-H -> PR-I (tests + multi-provider end-to-end)
```

## PR-A: litellm-cli Subprocess Shim
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
**Estimated LOC**: ~150
**Dependencies**: []

### Description
Introduce `scripts/lib/litellm_cli/` — a minimal subprocess shim that wraps `litellm.completion(...)` behind a stdin/stdout JSON-line protocol. The shim runs in its own venv (or pipx-installed env) so LiteLLM's transitive dependencies never reach VNX core. No SDK is imported in VNX itself; only `subprocess.Popen(["litellm-cli", ...])`.

### Scope
- `litellm-cli` shim entrypoint (separate isolated env / pipx-style)
- JSON-line stdin/stdout protocol: `{"op":"complete","model":"...","messages":[...]}` -> `{"event":"chunk"|"done"|"error",...}`
- Health-check probe: `litellm --health-check` integration per PRD §FR-11 health table
- Streaming-mode support (parity with `_streaming_drainer.py`)

### Success Criteria
- Shim starts in <500ms when invoked
- VNX `pip list` shows zero `litellm` transitive deps in the core env
- Streaming chunks flow through the shim without buffering
- Shim exits cleanly on EOF/SIGTERM; no zombie processes

### Test Plan
- **Unit**: shim parses well-formed JSON-line input, rejects malformed lines with `{"event":"error","reason":"protocol"}`
- **Unit**: shim survives a `KeyboardInterrupt` mid-stream and returns control with a `done`-with-`partial=true` event
- **Integration**: spawn shim from VNX harness, send a tiny mock-provider request, assert chunks arrive within 2s
- **Dependency-isolation test**: run `python -c "import litellm"` inside the VNX core venv -> must `ImportError` (proves no leak)
- **Process-hygiene test**: spawn 50 sequential shim calls, assert RSS in core process does not grow >5 MB
- **Health-check test**: `litellm --health-check` returns expected JSON on a configured backend; mocked when no creds present

### Quality Gate
`gate_pra_litellm_cli_shim`:
- [ ] Shim subprocess interface stable
- [ ] Zero transitive deps leak to core
- [ ] Streaming parity with internal drainer
- [ ] Tests green in CI

## PR-B: LiteLLM Adapter Promotion (W7-E POC -> Production)
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
**Estimated LOC**: ~80
**Dependencies**: [PR-A]

### Description
Promote the W7-E `LiteLLMAdapter` proof-of-concept (~150 LOC POC) to production by replacing its in-process `import litellm` calls with the `litellm-cli` shim from PR-A. Wire the adapter into the `WorkerProvider` Protocol (W9 from Phase 8). Register `litellm/<provider>/<model>` style identifiers in the provider registry.

### Scope
- Replace POC's direct `litellm` imports with subprocess shim handle
- Implement `WorkerProvider.spawn()/deliver()/stream_events()/stop()` over the shim
- Provider-id parser: `litellm/anthropic/claude-sonnet-4-6`, `litellm/bedrock/anthropic.claude-3-5-sonnet`, etc.
- Receipt enrichment: `provider_chain_at_dispatch` records `litellm/<backend>` and shim PID
- `observability_tier` field set per the W7-G tiering (LiteLLM = Tier-2)

### Success Criteria
- A dispatch with `provider: litellm/anthropic/claude-sonnet-4-6` round-trips successfully
- Adapter no longer references `litellm` Python module directly
- Receipts contain shim subprocess metadata

### Test Plan
- **Unit**: adapter constructs the correct shim argv per provider-id form
- **Unit**: adapter handles shim crash by emitting a structured failure event + clean lease release
- **Integration**: end-to-end dispatch via the in-repo subprocess-dispatch harness reaches the shim and returns events
- **Receipt-shape test**: parse receipt NDJSON; assert `provider_chain_at_dispatch[0]` matches expected pattern
- **Backward-compat test**: existing Anthropic-direct dispatches continue to work (no regression in `ClaudeAdapter`)

### Quality Gate
`gate_prb_litellm_adapter_promotion`:
- [ ] Adapter conforms to `WorkerProvider` Protocol
- [ ] All references to in-process `litellm` removed
- [ ] Receipt shape tests green
- [ ] No regression in non-LiteLLM providers

## PR-C: Ollama Parity Final Pass
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
**Estimated LOC**: ~50
**Dependencies**: [PR-B]

### Description
Close the final delta in `OllamaAdapter` so it matches the surface area now formalized by the LiteLLM adapter (event tier labels, receipt enrichment fields, health-check shape).

### Scope
- Add `observability_tier` field
- Add health-check (`ollama list`)
- Align event-shape with W7 `CanonicalEvent`

### Success Criteria
- Ollama dispatches produce receipts indistinguishable in shape from LiteLLM dispatches
- `ollama list` health probe is wired into `provider_chain.py`

### Test Plan
- **Unit**: event-shape diff between `OllamaAdapter` output and `LiteLLMAdapter` output is empty (modulo provider-id)
- **Integration**: dispatch a tiny prompt to a local Ollama (skip on CI without Ollama) — assert receipt fields match the spec
- **Health-probe test**: `ollama list` failure surfaces as a structured `provider_unhealthy` receipt event

### Quality Gate
`gate_prc_ollama_parity`:
- [ ] Event shape parity with LiteLLM adapter
- [ ] Health probe wired
- [ ] CI-safe (skips when Ollama not installed)

## PR-D: Bedrock Validation
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @test-engineer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: ~60 (test-only)
**Dependencies**: [PR-B]

### Description
Validate that `litellm/bedrock/anthropic.claude-3-5-sonnet-...` round-trips through the shim. Tests are gated on `VNX_BEDROCK_TEST_CREDENTIALS` env; otherwise skipped with explicit reason.

### Test Plan
- **Per-provider integration test**: dispatch via LiteLLM to Bedrock backend, assert events flow (chunk -> done) within 30s budget
- **Auth-failure test**: with invalid creds, adapter returns structured `auth_error` receipt and lease releases
- **Cost-budget test**: rate-limit Bedrock call (mock the shim to return 429) — adapter respects the budget caveat from cap-token (W10) by halting further calls in the dispatch
- **Streaming-parity test**: same dispatch via Claude direct vs LiteLLM-Bedrock-Anthropic — equivalent event count + types (allow ±1 keepalive event)
- **CI-skip test**: when env var missing, suite reports `SKIPPED reason=no_credentials` rather than failing silently

### Success Criteria
- All four tests pass when creds present; skip cleanly when absent
- No credentials leak into logs or events

### Quality Gate
`gate_prd_bedrock_validation`:
- [ ] Round-trip works against real Bedrock when creds available
- [ ] Cost-budget caveat respected
- [ ] Streaming parity with Claude direct
- [ ] No secrets in receipts

## PR-E: Vertex Validation
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @test-engineer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: ~60 (test-only)
**Dependencies**: [PR-B]

### Description
Same shape as PR-D, targeting Vertex AI (`litellm/vertex_ai/gemini-1.5-pro` and similar).

### Test Plan
- **Per-provider integration test**: dispatch via LiteLLM-Vertex; assert events flow
- **Auth-failure test**: invalid `GOOGLE_APPLICATION_CREDENTIALS` -> structured `auth_error` receipt
- **Cost-budget test**: simulated quota-exceeded -> respects cap-token budget caveat
- **Streaming-parity test**: vs the existing `vertex_ai_runner.py` non-LiteLLM REST path — diff <±1 event
- **Region-fallback test**: with primary region misconfigured, adapter surfaces `region_error`, does not silently retry in another region without operator opt-in

### Quality Gate
`gate_pre_vertex_validation`:
- [ ] Round-trip works on real Vertex when creds available
- [ ] Region-fallback explicit, never silent
- [ ] Streaming parity with `vertex_ai_runner.py`

## PR-F: Mistral Validation
**Track**: B
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @test-engineer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: ~50 (test-only)
**Dependencies**: [PR-B]

### Description
Validate `litellm/mistral/mistral-large-latest` round-trip. Mistral is the simplest of the trio (single API key, no IAM).

### Test Plan
- **Per-provider integration test**: dispatch + events flow
- **Auth-failure test**: invalid API key -> structured `auth_error`
- **Cost-budget test**: token-quota simulation -> respects cap-token budget
- **Streaming-parity test**: same dispatch via Mistral-direct vs LiteLLM-Mistral — equivalent event count + types

### Quality Gate
`gate_prf_mistral_validation`:
- [ ] Round-trip works on real Mistral when creds available
- [ ] Streaming parity

## PR-G: CI Integration Tests (Multi-Provider Matrix)
**Track**: B
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @test-engineer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: ~50 (test-only)
**Dependencies**: [PR-B]

### Description
GitHub Actions matrix that runs the per-provider integration tests on demand (workflow_dispatch + nightly). Provider creds injected as repository secrets; tests skip cleanly when a secret is absent.

### Test Plan
- **Workflow lint**: `actionlint` passes on the new workflow file
- **Skip-discipline test**: matrix run with no secrets configured produces a green build with explicit `SKIPPED` markers, not a silent green
- **Concurrency test**: matrix runs do not collide on shared shim build cache
- **Receipt-archive test**: workflow uploads receipt NDJSON as artifact for post-hoc audit

### Quality Gate
`gate_prg_ci_integration_tests`:
- [ ] Workflow lints clean
- [ ] Skip discipline visible (no silent passes)
- [ ] Receipts archived per run

## PR-H: PyPI Publish Coordination — `headless-context-rotation` Live
**Track**: A
**Priority**: P0
**Complexity**: Low
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: ~30 (config + workflow only)
**Dependencies**: [PR-C, PR-D, PR-E, PR-F]

### Description
Coordinate with the W6D module (already carved out in Phase 4). Ship the first PyPI release of `headless-context-rotation` and treat the publish flow itself as a live coordination test for the multi-provider work landed above (release notes generated by a LiteLLM-bridged dispatch as proof-of-life).

### Scope
- Confirm `pyproject.toml` (built in W6B/W6D) is publish-ready
- Tag `v0.1.0`
- Trigger the existing GitHub Actions release workflow
- Use a LiteLLM-bridged dispatch (any backend) to draft release notes — proves multi-provider in production
- Verify install: `pip install headless-context-rotation` from a clean venv

### Test Plan
- **Pre-publish test**: `python -m build` produces sdist + wheel without warnings
- **Metadata test**: `twine check dist/*` clean
- **Post-publish smoke test**: clean venv -> `pip install headless-context-rotation` -> `python -c "from headless_context_rotation import Tracker"` succeeds
- **Coordination receipt test**: the release-notes-drafting dispatch produces a valid receipt with `provider_chain_at_dispatch` containing `litellm/...`
- **Rollback drill**: documented yank procedure tested on TestPyPI first

### Quality Gate
`gate_prh_pypi_publish`:
- [ ] Module installs from real PyPI in clean venv
- [ ] Release notes generated via LiteLLM bridge (coordination test green)
- [ ] TestPyPI yank drill rehearsed

## PR-I: Tests + Multi-Provider End-to-End
**Track**: B
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @test-engineer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1 day
**Estimated LOC**: ~70 (test-only)
**Dependencies**: [PR-A, PR-B, PR-C, PR-D, PR-E, PR-F, PR-G, PR-H]

### Description
The W13-final aggregator PR. Adds the `claude_github_optional` review gate because multi-provider validation has the highest blast radius of the wave: a single regression here can silently break every fallback path in W7.5 / Phase 5.

### Test Plan (cross-cutting)
- **Multi-provider matrix smoke test**: same dispatch payload routed through Claude direct, LiteLLM-Anthropic, LiteLLM-Bedrock, LiteLLM-Vertex, LiteLLM-Mistral, Ollama; collect event counts; pairwise diff <±1
- **Failover test**: simulate primary down (Claude), assert chain falls through to LiteLLM-Bedrock per `provider_chain.py`
- **Cap-token budget end-to-end test**: an attenuated cap-token (Phase 9) restricts the LiteLLM provider to N tokens; dispatch exceeding N halts cleanly
- **Receipts audit test**: every receipt produced in this matrix carries `observability_tier` AND `provider_chain_at_dispatch`
- **No-leak test**: full transcript of a multi-provider run has zero matches for AWS/Vertex/Mistral key patterns
- **Codex gate (feature-end)**: feeds full receipt corpus to codex; expect zero blocking findings on multi-provider semantics
- **Claude GitHub optional**: invoked because high-blast-radius

### Quality Gate
`gate_pri_w13_endtoend`:
- [ ] All cross-cutting tests green
- [ ] Codex gate green (feature-end)
- [ ] Claude GitHub optional executed; result recorded
- [ ] Receipts audit clean across all providers

## Model Assignment Justification

| PR | Model | Rationale |
|----|-------|-----------|
| PR-A litellm-cli-shim | Sonnet | ~150 LOC subprocess plumbing; well-bounded protocol work |
| PR-B adapter promotion | Sonnet | ~80 LOC refactor of an existing POC |
| PR-C ollama parity | Sonnet | ~50 LOC delta-closure |
| PR-D/E/F validations | Sonnet | Test-writing; provider-specific but mechanical |
| PR-G CI integration | Sonnet | YAML + harness wiring |
| PR-H PyPI coordination | Sonnet | Config-only, no logic |
| PR-I end-to-end | Sonnet | Test composition over green building blocks |

No Opus deviations in W13: every PR is bounded mechanical work with strong test scaffolding. If PR-B uncovers a `WorkerProvider` Protocol gap (W9 leftover), escalate via dispatch-followup-audit to T3 rather than upgrading model mid-PR.

## Wave-End Quality Gate

`gate_w13_feature_end`:
- [ ] All 9 PR gates green
- [ ] codex_gate (feature-end) green
- [ ] claude_github_optional gate executed on PR-I (high blast radius)
- [ ] Provider matrix smoke test green
- [ ] PyPI module live, install verified
- [ ] R9 (LiteLLM dep bloat) mitigation verified: zero transitive deps leak to core
- [ ] All receipts carry `observability_tier` and `provider_chain_at_dispatch`

## Notes / Risks

- **R9 mitigation**: shim isolation is the load-bearing design choice; PR-A's dependency-isolation test is non-negotiable
- **Cred handling**: per-provider creds via repo secrets only; tests skip clean when absent (no silent green)
- **W6D coupling**: PR-H requires W6D's PyPI workflow already in place; if W6D slipped, defer PR-H to a follow-up wave
- **Streaming parity**: ±1 event tolerance is documented; greater drift is a blocker
