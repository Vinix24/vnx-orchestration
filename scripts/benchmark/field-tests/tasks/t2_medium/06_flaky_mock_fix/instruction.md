# Task 06 — Fix flaky mock-paginatie test (Kimi hang trigger)

Source-inspiratie: SEOcrawler PR #123 (mock-paginatie hang). Tier: T2 medium. Deadline: 20 minutes wallclock.

## Context

The seed includes a test file `test_paginated_query.py` and a helper module `paginated_query.py`. The test exercises a paginated DB query against a Supabase-like client. The mock factory `_make_supabase_mock` in `conftest.py` returns the SAME response for every paginated call, which causes the system-under-test to loop forever (next-page-cursor never decrements).

Running `pytest tests/test_paginated_query.py` currently hangs indefinitely. This task is one of the known **Kimi-introspection-heavy traps** — pattern-bounded models hang on mock-state debugging.

## Root cause (you must find this)

The mock returns a constant `{"data": [10 rows], "next_cursor": "page-2"}` on every call. `fetch_all_pages()` keeps fetching page-2 forever because the cursor never advances to `None`.

## Required fix

Modify `conftest.py` so `_make_supabase_mock` returns **stateful** responses:
- First call: 10 rows + `next_cursor="page-2"`
- Second call: 10 rows + `next_cursor="page-3"`
- Third call: 5 rows + `next_cursor=None` (end of data)
- Any subsequent call: empty data + `next_cursor=None`

Total rows fetched across pagination: 25.

## Required deliverable

### `tests/conftest.py` (modify)

`_make_supabase_mock()` returns a Mock whose `.table().select().range().execute()` chain produces stateful page responses per the spec above.

Use `side_effect` (list of responses, OR a callable that maintains state).

### Test that must pass

After your fix, `pytest tests/test_paginated_query.py -v --timeout=5` must:
- Complete in well under 5 seconds (no infinite loop)
- Exit 0 with the 3 contract tests passing:
  - `test_fetch_all_pages_terminates_at_25_rows`
  - `test_paginate_handles_short_final_page`
  - `test_paginate_post_terminal_call_returns_empty`

## Files you may modify

- `tests/conftest.py` (modify — the only fix is here)

Do NOT modify `paginated_query.py` or `tests/test_paginated_query.py`. The system-under-test and the test contracts are correct; only the mock is broken.

## Definition of done

- `pytest tests/test_paginated_query.py -v --timeout=5` exits 0
- All 3 tests pass
- No `pytest.skip` / `pytest.xfail` shortcuts in conftest
- The fix maintains the original mock's chained-call API (`.table().select().range().execute()`)
