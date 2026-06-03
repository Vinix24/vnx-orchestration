# ADR-021 — Exception Discipline: Narrow Exceptions and noqa for Silent Catches

**Status:** Accepted
**Date:** 2026-06-03
**Decided by:** Operator (Vincent van Deth)
**References:** ADR-005 (write ordering), ADR-007 (multitenant composite keys), PR #814 (5 codex_gate blockers)

## Context

PR #814 (`scripts/build_decisions_digest.py`) failed codex_gate round-1 with 5 blockers, 4 of which were the same `except Exception: pass` pattern in separate functions:

- `_select_top_3_decisions`: outer `except Exception: pass` swallowed an `AttributeError` from `sqlite3.Row.get()` (which does not exist on `sqlite3.Row`)
- `_build_dream_insights`: `except Exception: pass` made DB connection failures invisible to the caller
- `_build_health`: same pattern on the receipt-lag read path
- `_build_tomorrow_queue`: same pattern on the DB query block

Sonnet defaults to broad `except Exception: pass` as a perceived robustness measure. The governance gate correctly treats this as fault-masking. No existing ADR addressed Python exception discipline, so the gate had no clean citation target when declaring these blockers.

`governance_emit.py` already documents the correct approach: "Receipt write MUST NOT silently fail — raises RuntimeError on OSError." That contract was not extended to digest code because it lives in a separate file with no shared helper to anchor the pattern.

## Decision

`except Exception:` in production pipeline and digest code is permitted only in two forms.

**Form A — Log + re-raise (preferred for unexpected failures):**

```python
except Exception as exc:
    logger.error("digest.health: receipt lag read failed: %s", exc)
    raise
```

**Form B — Documented silent catch (narrow types only):**

```python
# noqa: vnx-silent-except reason=table absent in older schema -- absence is valid
except sqlite3.OperationalError:
    pass
```

The `reason=` string is mandatory. CI grep flags `vnx-silent-except` without a `reason=` string as a lint error.

### Narrow exception types (preferred over broad)

| Scenario | Correct catch |
|---|---|
| Schema-optional DB table absent | `sqlite3.OperationalError` |
| Missing file on disk | `FileNotFoundError` or `OSError` |
| Malformed NDJSON line | `json.JSONDecodeError` |
| Timestamp parse failure | `ValueError` |
| sqlite3.Row field access | Use `dict(row)` — eliminates AttributeError entirely |

### AttributeError is never caught silently

`AttributeError` indicates a coding error (`sqlite3.Row.get()` does not exist; object is not what the code assumes). The PR #814 blockers were hiding this class of error behind `except Exception: pass`. Correct fix: convert rows with `dict(row)` before field access, or use `row["column"]` with explicit `except KeyError` if key absence is expected.

### Applies to all new production code in

- `scripts/lib/` — shared helpers
- `scripts/lib/digest/` — digest collectors and renderer
- `scripts/commands/` — CLI entry points
- Any file where a broad except previously produced a codex_gate blocker

Does NOT require immediate refactor of stable production files (e.g. `governance_emit.py`, `subprocess_adapter.py`). Those carry their own inline documentation and are addressed in a dedicated 1.0.1 refactor wave (see Consequences).

## Consequences

**Positive:**

- Gate enforcement via `scripts/lib/ci_lint_patterns.py`: the `broad-except` pattern is already a blocking rule; this ADR gives it a clean citation target (`ADR-021`).
- `AttributeError` bugs surface at the call site instead of silently returning empty results.
- Form B's `reason=` string creates inline documentation of why silence is acceptable, auditable in code review.

**Negative / mitigations:**

- Existing call-sites in `scripts/lib/` that use broad except (estimated 8-12 files) are not immediately compliant. A refactor wave in 1.0.1 converts them: each broad except is converted to Form A (log+re-raise) or Form B (narrow type + noqa + reason). Until then, existing files are grandfathered; new files and modified functions must comply.
- Form A (`raise`) changes caller behavior if callers currently rely on silent degradation. Each conversion must be reviewed: does the caller handle the exception, or should the caller default on `OSError`/`JSONDecodeError` with an explicit fallback?

**Gate enforcement:**

`scripts/lib/codex_severity_policy.yaml` already lists `broad-except` as blocking. The enforcement grep is extended to also flag `vnx-silent-except` without `reason=`:

```yaml
- pattern: "# noqa: vnx-silent-except(?!.*reason=)"
  severity: blocking
  message: "Silent except requires reason= annotation per ADR-021"
  citation: ADR-021
```

## Dogfooding

`scripts/lib/atomic_io.py` (shipped in PR-D1, same dispatch as this ADR) applies Form A throughout: `BaseException` on temp-file cleanup re-raises after unlinking the temp; `OSError` from write failures propagates to the caller. No broad `except Exception: pass` in the helper.

## References

- ADR-005: NDJSON ledger-first write ordering — appenders raise on OSError
- ADR-007: multitenant composite keys — cited as gate-enforcement model
- PR #814: 5 codex_gate blockers (root cause analysis in NIGHTLY-DIGEST-REDESIGN-V2.md section 1)
- `scripts/lib/codex_severity_policy.yaml`: lint patterns enforced in CI
- `scripts/lib/governance_emit.py`: prior art — "Receipt write MUST NOT silently fail"
