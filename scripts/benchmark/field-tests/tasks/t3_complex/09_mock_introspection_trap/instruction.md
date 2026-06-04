# Task 09 — Diagnose + fix mock-introspection trap (Kimi hang signature)

Source-inspiratie: Mission Control WH-01 (`inspect.getsource()` op pollution-mocked function — Kimi hung 42+ StepBegin events). Tier: T3 complex. Deadline: 30 minutes wallclock.

## Context

This is the **canonical Kimi-hang trigger** — a test that introspects a function via `inspect.getsource()` while a `unittest.mock.patch` has replaced it. The mock object lacks the `__wrapped__`/source-attribute and `inspect.getsource` raises `TypeError` deep inside an unrelated assertion, masking the real bug. Pattern-bounded models loop on the introspection error without understanding the mock-pollution mechanism.

This task is INTENTIONALLY HARD for goedkope modellen — that's the discriminator. Sonnet/Opus should diagnose + fix in <10 min. Kimi/DS-flash likely hang or surface-fix without addressing root cause.

## Bug to fix

The seed contains:
- `query_inspector.py` — a utility that uses `inspect.getsource(fn)` to detect SQL-injection patterns in the source of query-building functions (security guard for dev-time linting)
- `tests/test_query_inspector.py` — pytest that uses `mock.patch('builders.build_user_query', return_value='SELECT * FROM users')` then calls `inspector.detect_unsafe(build_user_query)`

When the test runs:
- `mock.patch` replaces `build_user_query` with a `MagicMock`
- `inspector.detect_unsafe` calls `inspect.getsource(fn)` on the MagicMock
- `inspect.getsource` raises `TypeError: module, class, method, function, traceback, frame, or code object was expected, got MagicMock`
- The original test assertion fails with a confusing trace pointing at the inspector, NOT at the test's mock setup

The actual bug is **in the test** (mock-pollution against an introspection-using function), but a Kimi-class model often "fixes" `query_inspector.py` to swallow the TypeError — wrong fix, masks the real issue.

## Required fix

1. Diagnose: the test's `mock.patch` is the root cause. `query_inspector.detect_unsafe` is correct.
2. Modify `tests/test_query_inspector.py` to test the inspector against a REAL function (not a mocked one). The mock here is the bug.
3. Add 2 new tests (in the same file):
   - `test_detect_unsafe_real_function_with_fstring` — passes a real function with f-string SQL, asserts unsafe pattern detected
   - `test_detect_unsafe_real_function_with_param` — passes a real function using parameterized query, asserts safe

## Anti-patterns (auto-fail)

- Modifying `query_inspector.py` to catch/swallow TypeError
- Adding `inspect.unwrap` workarounds without removing the mock
- Using `mock.patch.object(..., spec=callable)` as a band-aid

## Definition of done

- `pytest tests/test_query_inspector.py -v` passes (3 tests: original-fixed + 2 new)
- `query_inspector.py` UNCHANGED from seed
- No `try: inspect.getsource(...) except TypeError` added anywhere
- Diagnosis explicitly stated in test file as a comment near the rewritten test
