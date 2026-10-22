- Tests (track `ci-profile-a-xdist`, D3-7): `test_normalizer_keeps_5_and_5_5_apart[anthropic/claude-sonnet-5-5-sonnet-5-5]`
  failed only under xdist. `tests/test_codex_model_registry.py` points `provider_registry` at a tmp registry with only
  `openai`; the first `normalize_model_name` call in that worker built its alias tables from it and kept them. Under
  that table `claude-sonnet-5-5` still normalized through the `claude-<family>` fallback, while
  `anthropic/claude-sonnet-5-5` came back unchanged. The file now resets and restores the normalizer caches around
  every test; `tests/test_model_normalizer_registry_isolation.py` runs polluter and victim in one process.
