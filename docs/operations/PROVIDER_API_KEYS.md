# Provider API keys (Wave 7+)

Wave 7 introduces non-Claude provider lanes via the LiteLLM bridge (Path B, ADR-015).
Each provider requires an API key set as an env var before dispatch.

| Provider | Env var | Where to get | Pricing | Status |
|---|---|---|---|---|
| Anthropic Claude | *(OAuth via the Max subscription; `claude -p`)* | — | Sonnet 5 $3/$15 | default (`claude_headless` lane, subscription-preserving; ANTHROPIC_API_KEY is not a VNX-lane key, see `no-anthropic-sdk`) |
| DeepSeek | DEEPSEEK_API_KEY | platform.deepseek.com | V4-Pro $0.435/$0.87, V4-Flash $0.14/$0.28 | Wave 7 PR-7.1 |
| Kimi CLI | *(OAuth via `kimi login`)* | — | K3 / K2-7 (default: K3) | Wave 7 PR-7.7 |
| Moonshot (Kimi) via LiteLLM | MOONSHOT_API_KEY | platform.moonshot.cn | K2-0905 $0.60/$2.50 | **blocked route** (`kimi-via-cli-only` constraint, see below) |
| Z.AI (GLM) via OpenRouter | OPENROUTER_API_KEY | openrouter.ai | GLM-5.2 $0.683/$2.147, GLM-5.3 $1.40/$4.40, GLM-5.3-flash $0.15/$0.50 (pass-through) | Wave 7 PR-7.3 |

**Note on GLM legacy versions:** GLM-4.5, GLM-4.6, GLM-5 (base), and GLM-5.1 are
deprecated. Only glm-5.2, glm-5.3, and glm-5.3-flash are accepted by the `litellm:zai`
route (`deprecated-glm-models` constraint, operator directive 2026-09-14; glm-5.2 remains
default). Passing any other variant raises an error. Direct Zhipu API integration (no
OpenRouter margin) is deferred to Wave 7.3.1 — see `scripts/lib/providers/z_ai_custom_provider.py`.

Pricing shown as input/output per MTok. Sonnet 5 reference included for cost comparison.

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
The repo-level secret scan (gitleaks, `.gitleaks.toml`, wired into
`.github/workflows/public-ci.yml`) covers leak prevention.

## Model registry

`scripts/lib/providers/wave7_models.yaml` is the SSOT for model names, pricing, and feature
flags. `provider_dispatch.py` uses it to resolve the LiteLLM model string for each
sub-provider. Override the resolved model via `VNX_LITELLM_MODEL` env var.

## Feature flags

There is no `VNX_ROUTING_DEEPSEEK` / `VNX_ROUTING_KIMI` / `VNX_ROUTING_GLM`
per-provider toggle: none of the three appears anywhere in `scripts/`, `bin/`
or `vnx_cli/`. Every sub-provider dispatches directly via its `--provider`
flag (`litellm:deepseek`, `kimi`, `litellm:zai` / `glm-harness`); there is no
enable/disable gate in front of it. Automatic cost-aware routing policy is
gated separately by `VNX_ROUTING_POLICY_ENABLED=1` (unset fleet-wide); see
`scripts/lib/routing_policy.py` and `scripts/lib/subprocess_dispatch.py`.

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

## Kimi K2 via Moonshot (blocked route)

`--provider litellm:moonshot` (direct Moonshot API, including the
`litellm:moonshot` sub-provider) is **blocked** by the `kimi-via-cli-only`
constraint (`scripts/lib/providers/provider_constraints.yaml`, `audit_severity:
blocking`, `override_allowed: false`). Kimi routes exclusively through the
`kimi` CLI OAuth lane below, to avoid API-key management and to keep cost
tracking on a single lane. The `moonshot` entries in
`scripts/lib/providers/wave7_models.yaml` (`kimi-k2-0905-default`,
`kimi-k2-6`) carry `dispatch_allowed: false` for exactly this reason:
bare-baseline registry data only, never a live dispatch target.

## GLM via OpenRouter / Z.AI (PR-7.3)

GLM routes through OpenRouter as `openrouter/z-ai/<model>`. Three versions are
admitted (operator directive 2026-09-14, deprecated-glm-models constraint);
glm-5.2 remains the dispatch default:

| Alias | LiteLLM name | Input/MTok | Output/MTok | Task classes |
|---|---|---|---|---|
| glm-5.2 | openrouter/z-ai/glm-5.2 | $0.683 | $2.147 | coding, review |
| glm-5.3 | openrouter/z-ai/glm-5.3 | $1.40 | $4.40 | coding, review |
| glm-5.3-flash | openrouter/z-ai/glm-5.3-flash | $0.15 | $0.50 | coding, review |

- Dispatch: `--provider litellm:zai` (defaults to glm-5.2) or `--provider glm-harness` (claude-CLI harness via local litellm proxy)
- Missing `OPENROUTER_API_KEY` → immediate exit(64) before subprocess spawn
- Deprecated models GLM-4.5, GLM-4.6, GLM-5 (base), GLM-5.1 → blocked by deprecated-glm-models constraint
- Context: glm-5.2 1,048,576 tokens; glm-5.3 and glm-5.3-flash 1,310,720 tokens; streaming + tool calls supported
- Direct Zhipu integration deferred to Wave 7.3.1 (see `z_ai_custom_provider.py`)

## Kimi CLI — direct provider (PR-7.7, #550)

Kimi CLI (`kimi`) is a standalone provider lane (not via LiteLLM, and the only
Kimi route; see `kimi-via-cli-only` above). Authentication is OAuth-based:

```bash
kimi login    # One-time browser OAuth flow
```

No `MOONSHOT_API_KEY` needed for this lane. The CLI outputs Anthropic-compatible stream-json events, which `kimi_spawn.py` normalizes to `CanonicalEvent` directly.

- Dispatch: `--provider kimi`
- Models: `kimi-k3` (default, `scripts/lib/providers/wave7_models.yaml` `kimi_cli.default_model`), `kimi-k2-7` (selected via `--model` flag). `kimi-k2-6` is disabled since 2026-07-21 (kimi-lane-hardening): the installed kimi-cli has no matching model.
- Wire protocol: camelCase events (`TurnBegin`, `ContentPart`, `TextPart`) mapped to VNX canonical shape
- Token tracking: extracted from `usage_complete` event in stream

## Related

- ADR-015: Wave 7 Path B decision + Path D deferral
- `scripts/lib/providers/wave7_models.yaml`: full model registry
- `scripts/lib/providers/provider_registry.py`: registry loader
- `scripts/lib/adapters/_litellm_runner.py`: subprocess sidecar that performs the actual API call
