# ADR-015: Wave 7 — DeepSeek/Kimi/GLM via LiteLLM Path B

**Status**: Accepted
**Date**: 2026-05-15
**Deciders**: Vincent van Deth (operator)
**Related ADRs**: ADR-003 (no SDK), ADR-010 (CLI subprocess canonical), ADR-016 (unified event shape)

## Context

VNX needs cost-optimization via cheaper LLM providers (DeepSeek V4, Kimi K2.6, GLM-5.1) for tasks where Sonnet 4.6 quality is overkill. Three integration paths exist:

- **Path A** (per-provider native): each provider gets its own spawn handler + provider_dispatch route. High maintenance.
- **Path B** (LiteLLM via subprocess bridge): LiteLLM runs as subprocess via `litellm_spawn.py` handler (from PR-4.6.5) + `_litellm_runner.py` helper. ONE spawn handler routes to N providers via `--provider litellm:<sub>`. Subprocess-only delivery preserves ADR-010 invariant (no in-process library calls into VNX worker code). Recommended.
- **Path D** (Claude-harness redirect via BASE_URL): use `claude` CLI with `ANTHROPIC_BASE_URL` pointing to a non-Anthropic endpoint. **BLOCKED** since 2026-05-10 — `claude` v2.1.136 emits 8x telemetry requests to api.anthropic.com despite BASE_URL redirect. Tier-C network-namespace sandbox MANDATORY before Path D is safe.

## Decision

Wave 7 adopts **Path B (LiteLLM bridge)** for first-class integration of DeepSeek V4, Kimi K2.6, and GLM-5.1.

- Single subprocess spawn handler (`scripts/lib/provider_spawns/litellm_spawn.py`, PR-4.6.5) routes to all three via sub_provider routing. The handler spawns `_litellm_runner.py` as a subprocess process; no LiteLLM imports inside VNX worker code beyond the runner sidecar.
- LiteLLM v1.75.5+ natively supports `deepseek/*` and `moonshot/*` (Kimi). z.AI/GLM requires `litellm.CustomLLM` subclass or OpenRouter fallback — both implemented inside the `_litellm_runner.py` subprocess, not in VNX worker code.
- Cost-routing policy (`routing_policy.yaml`) decides per dispatch which provider lane (default Claude/Sonnet 4.6 unchanged).
- Path D blocked until Tier-C sandbox lands. Tier-C is research-only in PR-7.6.

## Consequences

**Positive:**
- One spawn handler instead of three (lower maintenance)
- LiteLLM handles provider-specific quirks (auth, model names, token reporting)
- ADR-016 unified event shape applies uniformly across all sub-providers
- Cost reduction: DeepSeek V4 ~$0.28/$0.40 vs Sonnet $3/$15 = 10x cheaper input, 37x cheaper output

**Negative:**
- LiteLLM is a Python dependency for the `_litellm_runner.py` subprocess (no Anthropic SDK, ADR-003 OK; no in-process library calls in VNX worker code, ADR-010 OK)
- z.AI gap: PR-7.3 needs CustomLLM subclass (medium effort, medium-high risk)
- Sub-provider routing complexity in `provider_dispatch.py`

**Risk mitigation:**
- Feature-flag every sub-provider opt-in
- Default routing stays Claude/Sonnet 4.6 (no auto-switch)
- Byte-identity tests per provider against fixture baselines

## Cost matrix (LiteLLM registry, 2026-05-15)

| Provider | Model | Input ($/MTok) | Output ($/MTok) | Notes |
|---|---|---|---|---|
| Anthropic Claude | Sonnet 4.6 | 3.00 | 15.00 | default lane |
| Anthropic Claude | Haiku 4.5 | 1.00 | 5.00 | cheap Claude lane |
| DeepSeek | V3.2 | 0.28 | 0.40 | output very cheap |
| Moonshot | Kimi K2.6 | 0.95 | 4.00 | premium Kimi |
| Moonshot | K2-0905-preview | 0.60 | 2.50 | default Kimi lane (5x cheaper than Sonnet) |
| z.AI | GLM-5.1 | via OpenRouter | via OpenRouter | needs CustomLLM or OpenRouter fallback |

## ADR-010 compliance

Path B preserves ADR-010 (subprocess as canonical Claude routing, extended to all providers per Wave 4.6). LiteLLM lives inside the `_litellm_runner.py` subprocess; VNX worker code only spawns and communicates via the subprocess boundary. No `import litellm` appears in worker dispatch code paths. CI gate (`scripts/ci_enforce_adr010.sh` if present, else manual review) verifies the invariant.

## Rejected alternatives

- **Per-provider spawn handlers (Path A)**: rejected for maintenance cost
- **BASE_URL redirect (Path D)**: blocked by telemetry leak verified 2026-05-10
- **Anthropic Agent SDK as alternate compute lane**: ADR-003 amendment in PR-4.6.7 covers SDK-as-opt-in only; not part of Wave 7 cost-routing
- **GLM-4.5/4.6**: LEGACY, do not use; GLM-5.1 only

## Implementation roadmap

Wave 7 lands in 5 PRs (plus 1 research-only):

- **PR-7.0** (this ADR)
- **PR-7.1**: DeepSeek V4 lane (LiteLLM native `deepseek/*` provider)
- **PR-7.2**: Kimi K2.6 lane (LiteLLM native `moonshot/*` provider)
- **PR-7.3**: GLM-5.1 lane (CustomLLM subclass OR OpenRouter fallback)
- **PR-7.4**: Cost-routing policy engine (`routing_policy.yaml` + dispatcher integration)
- **PR-7.5**: Provider-specific behavior contracts (streaming, tool-call, audit-shape)
- **PR-7.6** (research-only): Tier-C network-namespace sandbox onderzoek voor Path D

Effort: 5 elapsed days with VNX 3-track parallel, 9.5 days sequential.

## See also

- `claudedocs/wave7-litellm-bridge-deepseek-kimi-glm.md` — Wave 7 design doc
- `claudedocs/project_deepseek_v4_pro_paths.md` (memory) — Path A/B/C/D analysis
- ADR-003, ADR-010, ADR-013, ADR-016
