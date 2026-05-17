# Provider API keys (Wave 7+)

Wave 7 introduces non-Claude provider lanes via the LiteLLM bridge (Path B, ADR-015).
Each provider requires an API key set as an env var before dispatch.

| Provider | Env var | Where to get | Pricing | Status |
|---|---|---|---|---|
| Anthropic Claude | ANTHROPIC_API_KEY (or OAuth via subscription) | console.anthropic.com | Sonnet 4.6 $3/$15 | default |
| DeepSeek | DEEPSEEK_API_KEY | platform.deepseek.com | V4-Pro $0.435/$0.87, V4-Flash $0.14/$0.28 | Wave 7 PR-7.1 |
| Kimi CLI | *(OAuth via `kimi login`)* | — | K2.6 / K2-0905 (free tier via CLI) | Wave 7 PR-7.7 |
| Moonshot (Kimi) via LiteLLM | MOONSHOT_API_KEY | platform.moonshot.cn | K2-0905 $0.60/$2.50 | Wave 7 PR-7.2 |
| Z.AI (GLM) via OpenRouter | OPENROUTER_API_KEY | openrouter.ai | GLM-5.1 $0.50/$2.50 (pass-through) | Wave 7 PR-7.3 |

**Note on GLM legacy versions:** GLM-4.5 and GLM-4.6 are deprecated. Only GLM-5.1 is
accepted by the `litellm:zai` route. Passing `--model glm-4.5` or `--model glm-4.6`
raises an error. Direct Zhipu API integration (no OpenRouter margin) is deferred to
Wave 7.3.1 — see `scripts/lib/providers/z_ai_custom_provider.py`.

Pricing shown as input/output per MTok. Sonnet 4.6 reference included for cost comparison.

## Provisioning

Set keys via shell export, or store them in `vnx.env` at repo root (gitignored):

```bash
# Option A: shell export (wins over file)
export DEEPSEEK_API_KEY="sk-..."

# Option B: file-based (loaded automatically by provider_dispatch.py)
# Copy vnx.env.example to vnx.env and fill in your keys.
cp vnx.env.example vnx.env
```

`vnx.env` is loaded by `scripts/lib/env_loader.py` at the start of every
`provider_dispatch.py` invocation. Shell env always wins over file values.
A user-level file at `~/.vnx/vnx.env` is also supported as a fallback.

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

Two model tiers available under `--provider litellm:deepseek`:

| Alias | LiteLLM name | Input/MTok | Output/MTok | Max output | Task classes |
|---|---|---|---|---|---|
| deepseek-v4-pro (default) | deepseek/deepseek-v4-pro | $0.435 | $0.87 | 384K | coding-premium, review, analysis |
| deepseek-v4-flash | deepseek/deepseek-v4-flash | $0.14 | $0.28 | 384K | coding, review, analysis |

- Context window: 1M tokens (both models)
- Tool calls: yes, streaming: yes
- Default lane: `deepseek-v4-pro` (~75% discount active; full price $1.74/$3.48)
- Dispatch: `--provider litellm:deepseek` (default) or `VNX_LITELLM_MODEL=deepseek/deepseek-v4-flash` (flash)
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

## Kimi CLI — direct provider (PR-7.7, #550)

Kimi CLI (`kimi`) is a standalone provider lane (not via LiteLLM). Authentication is OAuth-based:

```bash
kimi login    # One-time browser OAuth flow
```

No `MOONSHOT_API_KEY` needed for this lane. The CLI outputs Anthropic-compatible stream-json events, which `kimi_spawn.py` normalizes to `CanonicalEvent` directly.

- Dispatch: `--provider kimi`
- Models: K2.6, K2-0905 (selected via `--model` flag)
- Wire protocol: camelCase events (`TurnBegin`, `ContentPart`, `TextPart`) mapped to VNX canonical shape
- Token tracking: extracted from `usage_complete` event in stream
- Feature flag: `VNX_ROUTING_KIMI_CLI=1` (for policy engine routing; manual dispatch always works)

The Kimi CLI lane and the LiteLLM Moonshot lane are independent. Use Kimi CLI for OAuth-based free-tier access; use LiteLLM Moonshot for API-key-based metered access.

## Related

- ADR-015: Wave 7 Path B decision + Path D deferral
- `scripts/lib/providers/wave7_models.yaml`: full model registry
- `scripts/lib/providers/provider_registry.py`: registry loader
- `scripts/lib/adapters/_litellm_runner.py`: subprocess sidecar that performs the actual API call
