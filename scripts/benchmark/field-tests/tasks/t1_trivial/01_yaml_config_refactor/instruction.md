# Task 01 — YAML config refactor with env-var fallback

Source-inspiratie: Mission Control PR #236 (worker_queues YAML config).
Tier: T1 trivial. Deadline: 10 minutes wallclock.

## Context

The seed includes a Python module `worker_runner.py` that contains a hardcoded list of queue names at the top of the file:

```python
QUEUES = ["default", "scoring", "ingestion", "indexing"]
```

This list needs to live in a YAML config file so operators can edit queues without code changes. The refactor must remain backwards-compatible: if the YAML file is missing OR an env variable `WORKER_QUEUES` is set, the system falls back gracefully.

## Required changes

1. Create `config/worker_queues.yaml` with the queue list under a top-level `queues:` key.
2. Refactor `worker_runner.py` so `QUEUES` is no longer hardcoded. Resolution order:
   - If env var `WORKER_QUEUES` is set (comma-separated string), use that.
   - Else if `config/worker_queues.yaml` exists, parse it.
   - Else default to `["default"]`.
3. Add a helper function `load_queues() -> list[str]` that encapsulates this resolution.
4. Keep the public interface intact: `worker_runner.QUEUES` must still be importable as a top-level attribute (assigned from `load_queues()` at import time).

## Tests

The seed includes `tests/test_worker_runner.py` with 5 tests. **They currently FAIL against the seed** — the seed has the hardcoded list, the tests encode the refactor contract you must implement. After your refactor, all 5 must pass:

1. `test_default_queues_when_no_config` — yaml file absent, env unset → returns `["default"]`
2. `test_yaml_config_loads` — yaml file exists → returns its `queues:` list
3. `test_env_var_overrides_yaml` — env var set → wins over yaml
4. `test_module_attribute_is_loaded` — `worker_runner.QUEUES` is the resolved list at import time
5. `test_yaml_parse_error_falls_back` — malformed yaml → falls back to default, does not crash

Run with: `pytest tests/test_worker_runner.py -v`

## Files you may create/modify

- `worker_runner.py` (modify)
- `config/worker_queues.yaml` (create)

Do NOT modify `tests/test_worker_runner.py`. The tests are the contract.

## Definition of done

- All 5 tests pass
- `config/worker_queues.yaml` exists with valid YAML containing the four original queues
- `load_queues()` is defined and used
- `worker_runner.QUEUES` is still a list at module load
