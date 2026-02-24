# Track B Report: AI Config & Model Registry

**Dispatch ID**: 20260221-165539-ai-config-&-model-registry-(tr-B
**PR**: PR-1
**Track**: B
**Gate**: implementation
**Status**: success

---

## Implementation Summary

Created the centralized AI model configuration and registry system for PR-1.
Three open items were addressed:

- **OI-101**: Defined a provider-agnostic model registry schema in `src/models/ai_registry.py`
  with `ProviderRecord`, `ModelRecord`, `DefaultRouting`, `ValidationConfig`, and `AIModelRegistry`
  as the root aggregate.

- **OI-102**: Implemented YAML-based config loading in `src/services/ai_config.py` with full
  fallback defaults. Every parsing step is defensive — missing or malformed entries are logged
  and skipped; `load_registry()` always returns a usable `AIModelRegistry` object.

- **OI-103**: Added `ModelCapability` enum with `SCORING`, `CLASSIFICATION`, and `EMBEDDING`
  values. Every `ModelRecord` carries a `capabilities` list. `ProviderRecord.default_model()` and
  `AIModelRegistry.capable_models()` use this to route by capability.

**Key design decisions:**
- API keys are strictly environment-variable-only; `env_key` in config stores only the variable
  *name*, never the value. `is_provider_available()` checks `os.environ` at call time.
- `load_registry()` never raises — callers always get a registry object, even if empty.
- Local providers (Ollama) use an empty `env_key` and are always considered available,
  enabling them as a no-credentials fallback.
- `DefaultRouting` holds per-capability provider+model defaults; `AIModelRegistry.resolve()`
  uses these with a graceful fallback to the first capable model.

---

## Files Modified

| File | Action | Description |
|------|--------|-------------|
| `config/ai_models.yaml` | Created | Declarative registry: OpenAI, Anthropic, local (Ollama) providers with models, capabilities, latency tiers, cost metadata |
| `src/models/ai_registry.py` | Created | Schema: `ModelCapability`, `LatencyTier`, `ModelRecord`, `ProviderRecord`, `DefaultRouting`, `ValidationConfig`, `AIModelRegistry` |
| `src/services/ai_config.py` | Created | Service: `load_registry()`, `validate_registry()`, `is_provider_available()`, internal YAML parsers |
| `tests/test_ai_config.py` | Created | 49 tests covering all public APIs, edge cases, and integration via actual `config/ai_models.yaml` |

---

## Testing Evidence

```
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.4.1
collected 50 items

tests/test_ai_config.py   49 passed
tests/test_lead_service.py  1 passed

============================== 50 passed in 0.03s ==============================
```

**Quality gate checks:**

| Check | Result |
|-------|--------|
| All tests pass | ✅ 49/49 new tests, 50/50 total |
| `ai_registry.py` line count (179) | ✅ Under 500 |
| `ai_config.py` line count (194) | ✅ Under 500 |
| `test_ai_config.py` line count (523) | ⚠️ Slightly over 500-line warning (test file; well under 800 blocker) |
| Longest function ≤ 40 lines | ✅ All functions pass AST check |
| No hardcoded API keys / secrets | ✅ grep scan clean |
| Config validation covers all provider types | ✅ openai, anthropic, local all parsed and tested |
| Fallback defaults on missing/malformed config | ✅ `load_registry()` returns empty registry on any error |

---

## Open Items

- [ ] [warn] `tests/test_ai_config.py` is 523 lines — slightly above the 500-line warning threshold.
  The file is a test-only artifact and well under the 800-line blocker. Consider splitting into
  `test_ai_registry.py` (schema unit tests) and `test_ai_config_service.py` (service/integration tests)
  if the file grows further.
- [ ] [info] `PyYAML` is a runtime dependency used by `ai_config.py` but is not listed in
  `requirements.txt`. It is present in the current environment (v6.0.2). Should be added to
  `requirements.txt` before the PR is merged to production.
