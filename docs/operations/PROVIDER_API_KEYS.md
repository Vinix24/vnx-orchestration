# Provider API keys (Wave 7+)

Wave 7 introduces non-Claude provider lanes via the LiteLLM bridge (Path B, ADR-015).
Each provider requires an API key set as an env var before dispatch.

| Provider | Env var | Where to get | Pricing | Status |
|---|---|---|---|---|
| Anthropic Claude | ANTHROPIC_API_KEY (or OAuth via subscription) | console.anthropic.com | Sonnet 4.6 $3/$15 | default |
| DeepSeek | DEEPSEEK_API_KEY | platform.deepseek.com | V3.2 $0.28/$0.40 | Wave 7 PR-7.1 |
| Moonshot (Kimi) | MOONSHOT_API_KEY | platform.moonshot.cn | K2-0905 $0.60/$2.50 | Wave 7 PR-7.2 |
| Z.AI (GLM) | ZHIPU_API_KEY | open.bigmodel.cn | GLM-5.1 est. $0.50/$2.50 | Wave 7 PR-7.3 |

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

## Related

- ADR-015: Wave 7 Path B decision + Path D deferral
- `scripts/lib/providers/wave7_models.yaml`: full model registry
- `scripts/lib/providers/provider_registry.py`: registry loader
- `scripts/lib/adapters/_litellm_runner.py`: subprocess sidecar that performs the actual API call
