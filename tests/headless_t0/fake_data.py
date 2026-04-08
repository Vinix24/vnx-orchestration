#!/usr/bin/env python3
"""Generators for realistic fake VNX data used in headless T0 sandbox tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def fake_receipt(
    dispatch_id: str,
    terminal: str,
    status: str,
    report_path: str,
    track: str = "A",
    gate: str = "implementation",
) -> str:
    """Return a single NDJSON receipt line (task_complete event)."""
    record = {
        "event_type": "task_complete",
        "event": "task_complete",
        "timestamp": _now(),
        "terminal": terminal,
        "track": track,
        "type": f"{terminal} REPORT",
        "gate": gate,
        "status": status,
        "confidence": 0.9 if status == "success" else 0.5,
        "task_id": dispatch_id,
        "dispatch_id": dispatch_id,
        "session_id": f"session-{dispatch_id[:8]}",
        "report_path": report_path,
        "report_file": report_path.split("/")[-1] if report_path else "",
        "title": f"Fake report for {dispatch_id}",
        "dependencies": {"tracks": [], "components": [], "blocking": False, "risk_level": "low"},
        "quality_advisory": {
            "version": "1.0",
            "generated_at": _now(),
            "summary": {"warning_count": 0, "blocking_count": 0, "risk_score": 0},
            "t0_recommendation": {
                "decision": "approve" if status == "success" else "review",
                "reason": "Tests pass" if status == "success" else "Partial completion",
                "suggested_dispatches": [],
                "open_items": [],
            },
        },
        "cqs": {
            "cqs": 90.0 if status == "success" else 65.0,
            "normalized_status": status,
        },
    }
    return json.dumps(record)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _report_metadata(dispatch_id: str, track: str, status: str) -> str:
    return (
        f"**Dispatch ID**: {dispatch_id}\n"
        f"**PR**: F36\n"
        f"**Track**: {track}\n"
        f"**Gate**: implementation\n"
        f"**Status**: {status}\n"
    )


def fake_report_success(dispatch_id: str, track: str) -> str:
    """Clean worker report with full metadata block and evidence."""
    return f"""# Track {track} — Implementation Complete

{_report_metadata(dispatch_id, track, 'success')}

---

## Implementation Summary

Implemented the feature as specified. All tests pass.

## Files Modified

| File | Action | Description |
|---|---|---|
| `scripts/lib/example.py` | Created | 150-line implementation |
| `tests/test_example.py` | Created | 18 tests covering all paths |

## Testing Evidence

```
python3 -m pytest tests/test_example.py -v
18 passed in 0.08s
```

Commands run:
```
git add scripts/lib/example.py tests/test_example.py
git commit -m "feat(example): implement example feature"
```

## Open Items

(none)
"""


def fake_report_partial(dispatch_id: str, track: str) -> str:
    """Report missing evidence sections — suspicious for T0."""
    return f"""# Track {track} — Implementation

{_report_metadata(dispatch_id, track, 'success')}

---

## Implementation Summary

Changes were made to the codebase. The feature is implemented.

## Files Modified

- `scripts/lib/example.py`

## Testing Evidence

Tests were run and passed.

## Open Items

(none)
"""


def fake_report_gate_fail(dispatch_id: str, track: str) -> str:
    """Report with gate contradiction — claims success but mentions test failures."""
    return f"""# Track {track} — Implementation

{_report_metadata(dispatch_id, track, 'success')}

---

## Implementation Summary

Implemented the feature. Note: some existing tests are currently failing due to
an unrelated infrastructure issue. These failures are not caused by this change.

## Files Modified

| File | Action | Description |
|---|---|---|
| `scripts/lib/example.py` | Modified | Updated implementation |

## Testing Evidence

```
python3 -m pytest tests/test_example.py -v
3 failed, 15 passed in 0.12s
FAILED tests/test_example.py::TestExample::test_edge_case_1
FAILED tests/test_example.py::TestExample::test_edge_case_2
FAILED tests/test_example.py::TestExample::test_integration
```

## Open Items

| ID | Severity | Title |
|---|---|---|
| — | warn | 3 tests failing — claimed unrelated to this change |
"""


# ---------------------------------------------------------------------------
# t0_brief.json
# ---------------------------------------------------------------------------

def fake_t0_brief(
    t1_status: str = "idle",
    t2_status: str = "idle",
    t3_status: str = "idle",
    pending: int = 0,
    active: int = 0,
    t1_dispatch: str | None = None,
    t2_dispatch: str | None = None,
    t3_dispatch: str | None = None,
) -> dict:
    """Build a fake t0_brief.json dict."""
    def terminal_entry(status: str, track: str, dispatch_id: str | None) -> dict:
        entry: dict = {
            "status": status,
            "track": track,
            "ready": status == "idle",
            "last_update": _now(),
            "source": "terminal_state_priority",
            "status_age_seconds": 5,
        }
        if dispatch_id and status != "idle":
            entry["current_task"] = dispatch_id
        return entry

    return {
        "timestamp": _now(),
        "version": "1.0",
        "terminals": {
            "T1": terminal_entry(t1_status, "A", t1_dispatch),
            "T2": terminal_entry(t2_status, "B", t2_dispatch),
            "T3": terminal_entry(t3_status, "C", t3_dispatch),
        },
        "queues": {
            "pending": pending,
            "active": active,
            "completed_last_hour": 1,
            "conflicts": 0,
        },
        "tracks": {
            "A": {
                "current_gate": "implementation",
                "status": t1_status,
                "active_dispatch_id": t1_dispatch,
                "last_receipt": {
                    "event_type": "task_complete",
                    "status": "success",
                    "timestamp": _now(),
                    "dispatch_id": t1_dispatch or "none",
                },
                "health": "healthy",
            },
            "B": {
                "current_gate": "implementation",
                "status": t2_status,
                "active_dispatch_id": t2_dispatch,
                "last_receipt": None,
                "health": "healthy",
            },
            "C": {
                "current_gate": "implementation",
                "status": t3_status,
                "active_dispatch_id": t3_dispatch,
                "last_receipt": None,
                "health": "healthy",
            },
        },
        "pr": {
            "id": "F36",
            "current_gate": "implementation",
            "overall_status": "in_progress",
        },
        "pending_receipts": [],
        "blockers": [],
    }


# ---------------------------------------------------------------------------
# open_items.json
# ---------------------------------------------------------------------------

def fake_open_items(blockers: int = 2, warnings: int = 3) -> dict:
    """Build a fake open_items.json dict."""
    items = []
    oi_id = 1

    for i in range(blockers):
        items.append({
            "id": f"OI-{oi_id:03d}",
            "status": "open",
            "severity": "blocker",
            "title": f"Blocking issue {i + 1} — requires resolution before gate",
            "details": f"This blocker must be resolved. Dispatch: fake-dispatch-{i + 1}",
            "origin_dispatch_id": f"fake-dispatch-{i + 1}",
            "origin_report_path": None,
            "pr_id": "F36",
            "created_at": _now(),
            "updated_at": _now(),
            "closed_reason": None,
            "closed_by_dispatch_id": None,
            "closed_at": None,
        })
        oi_id += 1

    for i in range(warnings):
        items.append({
            "id": f"OI-{oi_id:03d}",
            "status": "open",
            "severity": "warn",
            "title": f"Warning {i + 1} — monitor but not blocking",
            "details": f"Low-priority issue discovered during track A implementation.",
            "origin_dispatch_id": f"fake-dispatch-warn-{i + 1}",
            "origin_report_path": None,
            "pr_id": "F36",
            "created_at": _now(),
            "updated_at": _now(),
            "closed_reason": None,
            "closed_by_dispatch_id": None,
            "closed_at": None,
        })
        oi_id += 1

    return {
        "schema_version": "1.0",
        "items": items,
    }


# ---------------------------------------------------------------------------
# Dispatch file
# ---------------------------------------------------------------------------

def fake_dispatch(
    dispatch_id: str,
    track: str,
    terminal: str,
    role: str,
    instruction: str,
    gate: str = "implementation",
    pr_id: str = "F36",
) -> str:
    """Return the text content of a fake dispatch .md file."""
    return f"""[[TARGET:{track}]]
Manager Block

Role: {role}
Track: {track}
Terminal: {terminal}
Gate: {gate}
Priority: P1
Cognition: normal
Dispatch-ID: {dispatch_id}
PR-ID: {pr_id}
Parent-Dispatch: none
Reason: Fake dispatch for sandbox testing

Instruction:

{instruction}

---
*VNX V8 - Native Skills + Instruction-Only Dispatch*
"""
