# Provider API keys (Wave 7+)

Wave 7 introduces non-Claude provider lanes via the LiteLLM bridge (Path B, ADR-015).
Each provider requires an API key set as an env var before dispatch.

| Provider | Env var | Where to get | Pricing | Status |
|---|---|---|---|---|
| Anthropic Claude | ANTHROPIC_API_KEY (or OAuth via subscription) | console.anthropic.com | Sonnet 4.6 $3/$15 | default |
| DeepSeek | DEEPSEEK_API_KEY | platform.deepseek.com | V3.2 $0.28/$0.40 | Wave 7 PR-7.1 |
| Moonshot (Kimi) | MOONSHOT_API_KEY | platform.moonshot.cn | K2-0905 $0.60/$2.50 | Wave 7 PR-7.2 |
| Z.AI (GLM) via OpenRouter | OPENROUTER_API_KEY | openrouter.ai | GLM-5.1 $0.50/$2.50 (pass-through) | Wave 7 PR-7.3 |

**Note on GLM legacy versions:** GLM-4.5 and GLM-4.6 are deprecated. Only GLM-5.1 is
accepted by the `litellm:zai` route. Passing `--model glm-4.5` or `--model glm-4.6`
raises an error. Direct Zhipu API integration (no OpenRouter margin) is deferred to
Wave 7.3.1 — see `scripts/lib/providers/z_ai_custom_provider.py`.

Pricing shown as input/output per MTok. Sonnet 4.6 reference included for cost comparison.

## Provisioning

Set keys via shell export or `.env` before invoking `provider_dispatch.py`:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

Keys are consumed by `_litellm_runner.py` subprocess — they never reach VNX worker code.
The repo-level secret scan (`scripts/lib/secret_scanner.py`) covers leak prevention.

## Model registry

`scripts/lib/providers/wave7_models.yaml` is the SSOT for model names, pricing, and feature
flags. `provider_dispatch.py` uses it to resolve the LiteLLM model string for each
sub-provider. Override the resolved model via `VNX_LITELLM_MODEL` env var.

## Feature flags

Each sub-provider is off by default. Enable via env var:

```bash
export VNX_ROUTING_DEEPSEEK=1   # enables DeepSeek V4 lane
export VNX_ROUTING_KIMI=1       # enables Kimi lane (PR-7.2)
export VNX_ROUTING_GLM=1        # enables GLM lane (PR-7.3)
```

Without feature flags, `provider_dispatch.py` still routes `litellm:deepseek`
dispatches — the flags control automatic routing policy (PR-7.4, not yet shipped).

## DeepSeek V4 (PR-7.1)

- LiteLLM model string: `deepseek/deepseek-v3.2`
- Context: 163,840 tokens
- Tool calls: yes, streaming: yes
- Dispatch: `--provider litellm:deepseek`
- Missing `DEEPSEEK_API_KEY` → immediate exit(64) before subprocess spawn

## Kimi K2 via Moonshot (PR-7.2)

Two model tiers available under `--provider litellm:moonshot`:

| Alias | LiteLLM name | Input/MTok | Output/MTok | Task classes |
|---|---|---|---|---|
| kimi-k2-0905-default | moonshot/kimi-k2-0905-preview | $0.60 | $2.50 | coding, review, analysis |
| kimi-k2-6 | moonshot/kimi-k2.6 | $0.95 | $4.00 | coding-premium |

- Default lane: `kimi-k2-0905-preview` (~5x cheaper than Claude Sonnet 4.6 output)
- Dispatch: `--provider litellm:moonshot` (default) or `--provider litellm:moonshot:kimi-k2-6` (premium)
- Missing `MOONSHOT_API_KEY` → immediate exit(64) before subprocess spawn
- Context: 8,192 tokens (both models); streaming + tool calls supported

## GLM-5.1 via OpenRouter / Z.AI (PR-7.3)

GLM-5.1 routes through OpenRouter as `openrouter/z-ai/glm-5`:

| Alias | LiteLLM name | Input/MTok | Output/MTok | Task classes |
|---|---|---|---|---|
| glm-5.1-default | openrouter/z-ai/glm-5 | $0.50 | $2.50 | coding, review |

- Dispatch: `--provider litellm:zai`
- Missing `OPENROUTER_API_KEY` → immediate exit(64) before subprocess spawn
- Deprecated models GLM-4.5 and GLM-4.6 → ValueError on dispatch (explicit legacy guard)
- Context: 8,192 tokens; streaming + tool calls supported
- Direct Zhipu integration deferred to Wave 7.3.1 (see `z_ai_custom_provider.py`)

## Related

- ADR-015: Wave 7 Path B decision + Path D deferral
- `scripts/lib/providers/wave7_models.yaml`: full model registry
- `scripts/lib/providers/provider_registry.py`: registry loader
- `scripts/lib/adapters/_litellm_runner.py`: subprocess sidecar that performs the actual API call
