# NIGHTLY-DIGEST-REDESIGN-V2 — Architecture Document

**Dispatch-ID:** 20260603-144840-nightly-digest-architect-redesign
**Date:** 2026-06-03
**Trigger:** B3.1 (>=3 NEW blockers in codex_gate round-1 on PR #814)
**Reference:** `claudedocs/NIGHTLY-DIGEST-REDESIGN-2026-06-03.md` (original design -- sections 1-2 remain valid; this document supersedes the implementation plan)

---

## 1. Root Cause

**Primary: (c) coding-pattern habits** -- Sonnet defaults to broad `except Exception: pass` for defensive robustness. Four of five blockers are this exact pattern, each in a separate function. The model treats silent catch-all as a stability feature; the governance gate correctly treats it as a fault-masking defect.

Evidence:
- `_select_top_3_decisions`: outer `except Exception: pass` swallows `AttributeError` from `sqlite3.Row.get()` (which does not exist -- `sqlite3.Row` requires key access, not `.get()`)
- `_build_dream_insights`: `except Exception: pass` wraps the entire DB connection block; a connection failure is invisible to the caller
- `_build_health`: `except Exception: pass` swallows every failure in the receipt-lag read path
- `_build_tomorrow_queue`: same pattern on the DB query block
- The worker had access to `scripts/lib/governance_emit.py` which documents the correct approach ("Receipt write MUST NOT silently fail -- raises RuntimeError on OSError") but did not apply it to digest code

**Secondary: (a) over-broad single-PR scope.** `scripts/build_decisions_digest.py` grew to 589 LOC in one file -- three of five private functions exceeded the 70-LOC threshold. The worker had no component boundary to enforce against.

**Secondary: (d) missing shared helpers.** The atomic write pattern (temp file + `os.replace`) is re-implemented inline in at least four files:
- `scripts/lib/state_writer.py:65`
- `scripts/lib/worker_permission_relay.py:212`
- `scripts/lib/tmux_interactive_dispatch.py:982`
- `scripts/lib/intelligence_dashboard.py:115`

No canonical `atomic_write_text()` helper existed. The worker wrote `output_path.write_text(digest_md)` without the temp+replace pattern (blocker 1) because there was no helper to reach for.

**Not primary: (b) over-broad dispatcher instruction.** The original design doc estimated ~430 LOC. The worker shipped 1060 LOC. The instruction scope was appropriate; the worker over-implemented.

---

## 2. Component Boundaries

Target: 8 files, each <=200 LOC, each function <=70 LOC. Seams drawn on data-ownership, not convenience.

```
scripts/
  lib/
    atomic_io.py                   # shared infra: atomic_write_text, ndjson_append
    digest/
      __init__.py                  # package marker (empty)
      io.py                        # path resolution + digest-specific write helpers
      renderer.py                  # render_markdown (pure: dataclass inputs -> str output)
      collectors/
        __init__.py
        progress.py                # collect_progress
        decisions.py               # collect_decisions
        dream.py                   # collect_dream
        health.py                  # collect_health
        queue.py                   # collect_queue
  build_decisions_digest.py        # orchestrator entry point (~60 LOC)
  decisions_log.py                 # KEEP but fix: events/decisions.ndjson not state/
```

**What each component owns:**

| File | Owns | LOC budget |
|---|---|---|
| `atomic_io.py` | `atomic_write_text`, `atomic_write_json`, `ndjson_append` with `fcntl.flock` | <=80 |
| `digest/io.py` | VNX path resolution for digest outputs, `write_digest_output` (calls `atomic_io`) | <=80 |
| `digest/renderer.py` | `render_markdown(decisions, progress, dream, health, queue, date_str) -> str` -- pure, no I/O | <=150 |
| `collectors/progress.py` | Reads `dispatch_register.ndjson` + phase log -> `ProgressData` dataclass | <=100 |
| `collectors/decisions.py` | Reads suggestions + antipatterns + gate results -> `list[DecisionItem]` | <=130 |
| `collectors/dream.py` | Reads `quality_intelligence.db` -> `DreamData` dataclass | <=90 |
| `collectors/health.py` | Reads `nightly_pipeline_health.json` + receipts + lane mix -> `HealthData` dataclass | <=90 |
| `collectors/queue.py` | Reads `dispatches/pending/` + tracks DB -> `list[QueueItem]` | <=70 |
| `build_decisions_digest.py` | Orchestrates collectors -> renderer -> `digest/io.write_digest_output` | <=70 |
| `decisions_log.py` | `append_decision` -> `.vnx-data/events/decisions.ndjson` (fix path + add flock) | <=60 |

**Seam rules:**
- Collectors return typed dataclasses, never raw dicts. Renderer takes only dataclasses.
- No collector reads from another collector's data source.
- `build_decisions_digest.py` is the only file that imports collectors + renderer together.
- DB access (sqlite3) is isolated inside each collector; the orchestrator never touches sqlite3 directly.

---

## 3. Infra Helpers

**Grep results confirm:** no canonical `atomic_io.py` exists in `scripts/lib/`. The tmp+replace pattern is duplicated inline in 4+ files. A shared helper eliminates both the duplication and the missing-helper motivation for writing bare `write_text()`.

### `scripts/lib/atomic_io.py` (new, ~80 LOC)

Skeleton (full implementation is D1 scope):

```python
# atomic_io.py -- Shared atomic file write and NDJSON append helpers.
# Consolidates the tmp+os.replace pattern (4 current inline implementations)
# and the fcntl.flock NDJSON append (currently embedded in governance_emit.py).

import fcntl, json, os
from pathlib import Path

def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    # Write text file atomically via temp + os.replace.
    # Raises OSError on write failure. Never partially-overwrites the target.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding=encoding)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

def atomic_write_json(path: Path, payload: dict, indent: int = 2) -> None:
    # Write JSON file atomically via temp + os.replace.
    atomic_write_text(path, json.dumps(payload, indent=indent, ensure_ascii=False))

def ndjson_append(path: Path, record: dict) -> None:
    # Append one JSON line to an NDJSON file with exclusive lock.
    # Safe for concurrent writers. ADR-005: ledger-first.
    # Raises OSError on write failure (never silently drops events).
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
```

**Decision on `governance_emit.py`:** Do NOT refactor it now. It is stable, production-used infrastructure. Extract the shared helpers into `atomic_io.py`; let `governance_emit.py` continue with its own inline implementation until a dedicated refactor PR. The dual-implementation gap goes in Open Items.

---

## 4. PR Breakdown

All LOC targets include tests in `tests/digest/`.

| PR | Files touched | LOC | Scope | Depends on |
|---|---|---|---|---|
| **D1** infra helpers | `scripts/lib/atomic_io.py` + `tests/test_atomic_io.py` | <=100 | **1.0** | none |
| **D2** progress + minimal digest | `scripts/lib/digest/__init__.py`, `digest/io.py`, `collectors/progress.py`, `digest/renderer.py` (progress-only), `scripts/build_decisions_digest.py` (progress path only), `tests/digest/test_progress.py` | <=250 | **1.0** | D1 |
| **D3** decisions + decision log | `collectors/decisions.py`, `decisions_log.py` (path fix + flock), `tests/digest/test_decisions.py` | <=200 | 1.0.1 | D1 |
| **D4** dream + health + queue + full render | `collectors/dream.py`, `collectors/health.py`, `collectors/queue.py`, `digest/renderer.py` (complete), `tests/digest/test_collectors.py` | <=250 | 1.0.1 | D1, D2, D3 |
| **D5** CLI + email template | `scripts/commands/digest.sh`, `scripts/send_digest_email.py` update, `bin/vnx` update, phase 20->21 re-integration | <=150 | 1.0.1 | D2, D4 |

**What NOT to ship in 1.0:**
- `_select_top_3_decisions` auto-selector (4 of 5 blockers originated here)
- `decisions_log.py` / `vnx digest decide` CLI (requires D3 decision log wiring)
- `_build_dream_insights` DB queries (sqlite3.Row.get() bug + broad except live here)
- `_build_tomorrow_queue` DB queries (outer except swallows DB failures)
- Phase 20 insertion into `nightly_intelligence_pipeline.sh` (health-summary timing issue; ship as phase 21 in D5)

**What MUST ship for 9-juni launch (D1 + D2 only):**
- `atomic_io.py` -- eliminates the non-atomic write blocker; also reduces duplication in existing codebase
- `_build_progress_table` -- reads only NDJSON (`dispatch_register.ndjson`, `nightly_pipeline_phases.ndjson`); no DB, no sqlite3.Row; failure modes are minimal
- Minimal renderer showing yesterday's progress numbers table
- Tests: D1 (5 tests), D2 (5 tests), total 10 tests at <=300 LOC combined

---

## 5. Exception Discipline

**Proposed as ADR-021** (new file: `docs/governance/decisions/ADR-021-exception-discipline.md`).

### Rule

`except Exception:` in production digest/pipeline code is permitted only in two forms:

**Form A -- Log + re-raise:**

```python
except Exception as exc:
    logger.error("digest.health: receipt lag read failed: %s", exc)
    raise
```

**Form B -- Documented silent (narrow cases only):**

```python
# noqa: vnx-silent-except reason=table absent in older schema -- absence is valid
except sqlite3.OperationalError:
    pass
```

The `reason=` string is mandatory. CI grep will flag `vnx-silent-except` without a reason string.

### Narrow exception types (preferred over broad)

| Scenario | Correct catch |
|---|---|
| Schema-optional DB table absent | `sqlite3.OperationalError` |
| Missing file on disk | `FileNotFoundError` or `OSError` |
| Malformed NDJSON line | `json.JSONDecodeError` |
| Timestamp parse failure | `ValueError` |
| sqlite3.Row field access | Use `dict(row)` -- eliminates AttributeError entirely |

### `AttributeError` is never caught silently

`AttributeError` indicates a coding error (`sqlite3.Row.get()` does not exist; object is not what the code assumes). The PR #814 outer `except Exception: pass` was hiding this. Fix: convert rows with `dict(row)` before field access, or use `row["column"]` with explicit `except KeyError` if key absence is expected.

### Where this lives

New **ADR-021**, not merged into an existing ADR. Rationale:
- ADR-005 governs write ordering (NDJSON ledger-first), not error handling patterns
- ADR-007 governs schema design (tenant scoping)
- No existing ADR covers Python exception discipline
- `scripts/lib/codex_severity_policy.yaml` gates need a clean citation target for the "broad-except" blocking rule

---

## 6. ADR-005 Events Ledger Clarification

**Question:** Does `decisions_log.ndjson` go to `.vnx-data/events/` or `.vnx-data/state/`?

**Answer: `.vnx-data/events/decisions.ndjson`**

`.vnx-data/events/decisions.ndjson` **already exists** (confirmed by directory listing; entries dated 2026-04-22 with fields `question`, `action`, `reasoning`, `confidence`, `backend_used`, `latency_ms`).

The PR #814 `decisions_log.py` wrote to `_state_dir() / "decisions_log.ndjson"` = `.vnx-data/state/decisions_log.ndjson`. This is wrong on two counts:
1. Creates a new file instead of appending to the existing `events/decisions.ndjson`
2. `state/` holds SQLite projections and structured state per ADR-005; operator-decision events are ledger events, not state projections

**Canonical rule (note to add in ADR-005 "Implementation note" section):**

> Operator decision events append to `.vnx-data/events/decisions.ndjson`.
> This file is a durable ledger -- NOT truncated per-dispatch (unlike per-terminal event files).
> New entries include `event_type: "decision"` and `project_id: "vnx-dev"` per ADR-016 canonical event shape.

**Existing `decisions.ndjson` schema (observed):**

```json
{"timestamp": "2026-04-22T10:39:10Z", "question": "re_dispatch",
 "context_hash": "fceaaa06a978", "action": "skip",
 "reasoning": "State is normal", "confidence": 1.0,
 "backend_used": "dry-run", "latency_ms": 0}
```

**New operator-decision entries (D3 target schema):**

```json
{"event_type": "decision", "timestamp": "...", "project_id": "vnx-dev",
 "dec_id": "DEC-1", "action": "accept", "reason": "...", "actor": "operator"}
```

The existing entries lack `event_type` -- they predate ADR-016. New schema is a superset; both shapes coexist. Backfill migration is out of scope.

---

## 7. Launch Decision

**Context:** 6 days to 9-juni HN/Reddit launch. B3.1 redesign work for 1.0 scope = D1 + D2 + ADR-021 stub = ~2 working days. Full feature (D1-D5) = 5-7 days.

**Recommendation: Ship Phase 1 (D1 + D2) in 1.0. Defer D3-D5 to 1.0.1.**

### Risk matrix

| Option | Launch risk | Feature value | Rework risk |
|---|---|---|---|
| Defer entirely (no digest feature) | Low -- clean 1.0, no new code | None | None |
| Phase 1 only (D1 + D2) | Low -- 2 files new, <=350 LOC | Progress numbers in digest | Low -- progress.py minimal failure modes |
| Full feature D1-D5 | High -- 6+ days, 5 PRs, B3.1 path still warm | Full decisions-first digest | High -- insufficient time for gate review cycle |

**Why Phase 1 rather than full defer:**
- `atomic_io.py` (D1) is independently valuable in <=100 LOC -- reduces duplication in 4 existing files; clean standalone PR regardless of digest feature
- `_build_progress_table` (D2) reads only NDJSON (no DB, no sqlite3.Row, no complex state); the only failure mode is "files missing -> return defaults" -- this is safe to ship
- 33% unknown dispatch outcomes in current metrics is a concrete problem; a progress table with accurate numbers directly serves the "operator can trust last night's run" goal
- The "Need YOUR decision (top 3)" section is manually curated by the operator until D3 ships; this is explicit and honest rather than auto-selected but error-masked

**Why not full feature:**
- The auto-selection logic in `_select_top_3_decisions` is where 4 of 5 blockers lived in PR #814 -- needs the most review time
- 9-juni is a positioning launch (HN/Reddit), not a digest-feature demo; digest is DX improvement, not the headline
- Shipping a clean Phase 1 under gate review is better than rushing a full feature that fails codex_gate round-2

**1.0.1 commitment:**
- D3-D5 as a single sprint: target 9-juni + 7 days (2026-06-16)
- Phase 20 health-summary timing fix ships in D5 (rename to phase 21, run after health write)
- ADR-021 exception discipline formally drafted and merged in the D1 PR

---

## Appendix A: Phase 20 Timing Issue (Advisory)

`nightly_intelligence_pipeline.sh` writes `nightly_pipeline_health.json` at the end of the script, AFTER all phases complete. Phase 20 runs inside the phase sequence. Therefore `collect_health()` reads the PREVIOUS run's health file -- the health shown in the digest is one day stale.

**Fix:** Move digest generation to run AFTER the health summary write (rename to Phase 21). Requires only a shell reorder in `nightly_intelligence_pipeline.sh`. Deferred to D5 since `collect_health` is out of 1.0 scope anyway.

---

## Appendix B: Component Skeletons

Signature-only stubs. No implementation. The implementing worker fills in per ADR-021 exception discipline.

### `scripts/lib/atomic_io.py`

```python
def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None: ...
def atomic_write_json(path: Path, payload: dict, indent: int = 2) -> None: ...
def ndjson_append(path: Path, record: dict) -> None: ...
```

### `scripts/lib/digest/io.py`

```python
@dataclass
class DigestPaths:
    state_dir: Path
    data_dir: Path
    output_path: Path

def resolve_paths(state_dir: Path | None = None, data_dir: Path | None = None) -> DigestPaths: ...
def write_digest_output(paths: DigestPaths, content: str) -> None: ...
```

### `scripts/lib/digest/collectors/progress.py`

```python
@dataclass
class ProgressData:
    prs_merged: int = 0
    dispatches_total: int = 0
    dispatches_success_pct: str = "n/a"
    ois_filed: int = 0
    ois_closed: int = 0
    dream_cycles: int = 0
    failed_phases: int = 0

def collect_progress(data_dir: Path, state_dir: Path, window_hours: int = 24) -> ProgressData:
    # Reads dispatch_register.ndjson + phase log.
    # Returns ProgressData with defaults on any read error.
    # Exception discipline: OSError -> return defaults; json.JSONDecodeError -> skip line.
    ...
```

### `scripts/lib/digest/collectors/decisions.py`

```python
@dataclass
class DecisionItem:
    id: str
    source_id: str
    title: str
    context: str
    recommended: str
    alternative: str
    source_ref: str
    age_h: float

def collect_decisions(
    suggestions: list[dict],
    antipatterns: list[dict],
    gates_dir: Path | None = None,
) -> list[DecisionItem]:
    # Exception discipline: sqlite3.OperationalError acceptable (schema-optional);
    # AttributeError never caught -- use dict(row) to avoid sqlite3.Row.get() issue.
    ...

def _select_p1_pending(suggestions: list[dict], now: datetime, seen: set) -> list[DecisionItem]: ...
def _select_p2_antipatterns(antipatterns: list[dict], seen: set) -> list[DecisionItem]: ...
def _select_p3_stuck_gates(gates_dir: Path, now: datetime, seen: set) -> list[DecisionItem]: ...
```

### `scripts/lib/digest/collectors/dream.py`

```python
@dataclass
class DreamData:
    new_candidates: list[dict] = field(default_factory=list)
    auto_promoted: list[str] = field(default_factory=list)

def collect_dream(db_path: Path, days: int = 7) -> DreamData:
    # sqlite3.OperationalError: acceptable (schema-optional tables)
    # sqlite3.connect() failure: log ERROR, return DreamData() with empty defaults
    # Row access: use dict(row) -- avoids sqlite3.Row.get() AttributeError
    ...
```

### `scripts/lib/digest/collectors/health.py`

```python
@dataclass
class HealthData:
    pipeline_status: str = "unknown"
    phases_ok: int = 0
    phases_run: int = 0
    lane_mix: str = "n/a"
    receipt_lag: str = "unknown"
    db_sizes: dict = field(default_factory=dict)

def collect_health(state_dir: Path) -> HealthData:
    # Note: nightly_pipeline_health.json is written AFTER all phases.
    # If called from phase 20 (inside sequence), reflects PREVIOUS run.
    # Fix: run as phase 21. See Appendix A. Deferred to D5 / 1.0.1.
    ...
```

### `scripts/lib/digest/collectors/queue.py`

```python
@dataclass
class QueueItem:
    type: str
    ref: str
    title: str
    status: str

def collect_queue(data_dir: Path, max_items: int = 5) -> list[QueueItem]:
    # sqlite3.OperationalError: acceptable (tracks table schema-optional)
    # OSError on pending_dir scan: return [] (no pending dispatches is valid)
    ...
```

### `scripts/lib/digest/renderer.py`

```python
def render_markdown(
    decisions: list[DecisionItem],
    progress: ProgressData,
    dream: DreamData,
    health: HealthData,
    queue: list[QueueItem],
    date_str: str,
) -> str:
    # Pure function. No I/O. No side effects. No exceptions possible.
    ...

def _render_decisions_section(decisions: list[DecisionItem]) -> list[str]: ...
def _render_progress_section(progress: ProgressData) -> list[str]: ...
def _render_dream_section(dream: DreamData) -> list[str]: ...
def _render_health_section(health: HealthData) -> list[str]: ...
def _render_queue_section(queue: list[QueueItem]) -> list[str]: ...
```

### `scripts/build_decisions_digest.py` (orchestrator)

```python
# Phase 20 entry point -- decisions-first digest builder.
# 1.0 scope: progress-only path. D3-D5 collectors wired in 1.0.1.

def render_decisions_digest(state_dir: Path | None = None, data_dir: Path | None = None) -> str:
    # Orchestrate collectors -> renderer -> markdown string.
    # Called by tests and main().
    ...

def main() -> int:
    # Write digest to state_dir/decisions_digest.md atomically via atomic_io.
    ...
```

---

*Generated by architect dispatch 20260603-144840-nightly-digest-architect-redesign.*
