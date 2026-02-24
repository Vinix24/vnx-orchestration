# Track B Report: Multi-Provider AI Dispatch

**Dispatch ID**: 20260221-165539-multi-provider-dispatch-(track-B
**PR**: PR-5
**Track**: B
**Gate**: implementation
**Status**: success

---

## Implementation Summary

Created the multi-provider AI dispatch layer for PR-5. All three open items addressed:

- **OI-501 — Provider routing strategy**: Three strategies implemented in `RoutingStrategy` enum.
  - `COST`: sorts candidates by `cost_per_1k_input + cost_per_1k_output` ascending (cheapest first).
  - `LATENCY`: sorts by `LatencyTier` (LOW → MEDIUM → HIGH).
  - `CAPABILITY`: preserves registry declaration order from the YAML config.
  A soft `preferred_provider` hint places a specific provider first when it is available, without
  preventing fallback to others.

- **OI-502 — Fallback chain**: `AIDispatcher.dispatch()` iterates an ordered chain of
  `(ProviderRecord, ModelRecord)` pairs. Any exception (including `asyncio.TimeoutError`) moves
  to the next candidate. `_append_fallback()` injects the `DefaultRouting.fallback_provider`
  (the configured `local` Ollama entry) at the end of the chain when not already present.
  Each call is wrapped in `asyncio.wait_for(timeout=5.0s)`, satisfying the 5-second gate.

- **OI-503 — Usage tracking**: Every attempt (success or failure) is recorded in `UsageTracker`
  via `UsageRecord`. Cost is computed as:
  `(input_tokens/1000) * cost_per_1k_input + (output_tokens/1000) * cost_per_1k_output`.
  `UsageTracker.total_cost(provider_id=None)` and `by_provider()` support per-provider audit.

**Key design decisions:**
- `ProviderCallAdapter` is a `Protocol` so production HTTP clients and test mocks are injected
  without modifying the dispatcher. No HTTP library dependency was added — the real adapter
  implementation belongs in a separate PR with the provider SDK dependencies.
- API keys are checked via `os.environ.get(provider.env_key)` at dispatch time, never from config.
  Providers with `env_key=""` (local models) are always available.
- `UsageTracker` is in-memory by default, dependency-injected so callers can swap in a
  DB-backed implementation without changing the dispatcher.
- `dispatch()` returns a `DispatchResult` (not a union/Optional) — failure always raises
  `RuntimeError`, keeping call sites simple.

---

## Files Modified

| File | Action | Description |
|------|--------|-------------|
| `src/models/dispatch_config.py` | Created | `RoutingStrategy`, `DispatchRequest`, `DispatchResult`, `UsageRecord` dataclasses |
| `src/services/ai_dispatch.py` | Created | `ProviderCallAdapter` Protocol, `UsageTracker`, `AIDispatcher` with routing, fallback, cost tracking |
| `tests/test_ai_dispatch.py` | Created | 25 tests covering routing strategies, fallback chain, timeout, cost tracking, env-key filtering |

---

## Testing Evidence

```
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.4.1
collected 75 items

tests/test_ai_config.py   49 passed
tests/test_ai_dispatch.py 25 passed
tests/test_lead_service.py  1 passed

============================== 75 passed in 0.10s ==============================
```

**Quality gate checks:**

| Check | Result |
|-------|--------|
| All tests pass including fallback scenarios | ✅ 75/75 total |
| `dispatch_config.py` line count (57) | ✅ Under 500 |
| `ai_dispatch.py` line count (297) | ✅ Under 500 |
| `test_ai_dispatch.py` line count (588) | ⚠️ Above 500-line warning (test file; well under 800 blocker) |
| All functions ≤ 40 lines | ✅ AST scan clean — no warnings |
| 100% API keys from environment variables | ✅ `os.environ.get(provider.env_key)` only |
| No secrets in config files / source | ✅ grep scan clean |
| Cost tracking records every request | ✅ every attempt (success and failure) recorded with provider, model, token count, cost |
| Fallback chain timeout ≤ 5 seconds | ✅ `asyncio.wait_for(timeout=5.0)` enforced per provider attempt |
| Timeout fallback test (0.02s timeout, 10s sleep) | ✅ `test_dispatch_timeout_causes_fallback_to_next_provider` passes |

---

## Open Items

- [ ] [info] `tests/test_ai_dispatch.py` is 588 lines — above the 500-line soft warning.
  The file is test-only and well under the 800-line blocker. Split into
  `test_dispatch_config.py` (model tests) and `test_ai_dispatcher.py` (service tests)
  if it grows further.
- [ ] [info] `ProviderCallAdapter` is a Protocol with no concrete implementation yet.
  A production HTTP adapter (using `httpx` or provider SDKs) should be implemented
  in a follow-up PR when provider SDK dependencies are established. The Protocol
  contract is fully tested via `_MockAdapter` in the test suite.
- [ ] [info] `datetime.datetime.utcnow()` produces deprecation warnings in Python 3.12.
  Both `ai_dispatch.py` and test direct construction of `UsageRecord` should migrate
  to `datetime.datetime.now(datetime.UTC)` when the project targets Python 3.11+.
