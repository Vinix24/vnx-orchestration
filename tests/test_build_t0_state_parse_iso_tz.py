"""tests/test_build_t0_state_parse_iso_tz.py — D1 poort A regression coverage.

t0_state.json stalled 23 days (generated_at frozen at 2026-08-07T08:01:47Z)
because `_parse_iso` returned a NAIVE datetime for any timestamp lacking a
trailing ``Z`` or explicit UTC offset. Two sort-key call sites fell back to
a naive `datetime.min` on a missing/unparseable timestamp. The moment a
sort mixed a naive value (from either path) with an aware one, Python raised
"can't compare offset-naive and offset-aware datetimes" — silently, because
build_t0_state.py's own top-level `except Exception` swallowed it (poort D).

Reproduction confirmed against the pre-fix source (git HEAD at the time this
test was written) for both call sites before writing the fix. D8 removed the
feature_state site with its section; the recent_receipts site stays covered.
"""

from __future__ import annotations

import json
import sys
from datetime import timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import build_t0_state as bts  # noqa: E402


# ---------------------------------------------------------------------------
# _parse_iso itself: naive input must normalize to aware UTC
# ---------------------------------------------------------------------------

def test_parse_iso_normalizes_naive_timestamp_to_utc_aware():
    dt = bts._parse_iso("2026-08-07T08:01:47")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timezone.utc.utcoffset(dt)


def test_parse_iso_preserves_z_suffixed_timestamp_as_aware():
    dt = bts._parse_iso("2026-08-07T08:01:47Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_preserves_explicit_offset():
    dt = bts._parse_iso("2026-08-07T08:01:47+02:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 2 * 3600


def test_parse_iso_returns_none_on_garbage():
    assert bts._parse_iso("not-a-timestamp") is None
    assert bts._parse_iso("") is None


def test_min_aware_datetime_is_aware_and_comparable():
    """The datetime.min replacement sort-key callers fall back to must be
    aware, or it still crashes the moment it is compared to a real aware
    value from _parse_iso (the actual D1 failure mode)."""
    assert bts._MIN_AWARE_DATETIME.tzinfo is not None
    now_aware = bts._parse_iso("2026-08-07T08:01:47Z")
    assert bts._MIN_AWARE_DATETIME < now_aware  # must not raise


# ---------------------------------------------------------------------------
# _build_recent_receipts: final cross-dispatch sort (was ~L1697)
# ---------------------------------------------------------------------------

def test_build_recent_receipts_survives_mixed_naive_and_z_timestamps(tmp_path: Path) -> None:
    """Two different dispatches, one Z-suffixed timestamp and one naive:
    the final reverse-chronological sort across best-per-dispatch records
    must not crash (D1 crash site 2)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts = state_dir / "t0_receipts.ndjson"
    lines = [
        json.dumps({
            "dispatch_id": "D1", "timestamp": "2026-08-07T08:00:00Z",
            "event_type": "task_complete", "status": "success",
        }),
        json.dumps({
            "dispatch_id": "D2", "timestamp": "2026-08-07T08:01:47",
            "event_type": "task_complete", "status": "success",
        }),
    ]
    receipts.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = bts._build_recent_receipts(state_dir, limit=10)

    ids = [r["dispatch_id"] for r in result]
    assert set(ids) == {"D1", "D2"}
    # reverse=True: the later (naive-parsing) timestamp must sort first.
    assert ids[0] == "D2"


def test_build_recent_receipts_survives_missing_timestamp(tmp_path: Path) -> None:
    """A receipt with no timestamp at all must not crash against a receipt
    that does have one (the datetime.min fallback path)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts = state_dir / "t0_receipts.ndjson"
    lines = [
        json.dumps({
            "dispatch_id": "D1",
            "event_type": "task_complete", "status": "success",
        }),
        json.dumps({
            "dispatch_id": "D2", "timestamp": "2026-08-07T08:01:47Z",
            "event_type": "task_complete", "status": "success",
        }),
    ]
    receipts.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = bts._build_recent_receipts(state_dir, limit=10)

    assert {r["dispatch_id"] for r in result} == {"D1", "D2"}
