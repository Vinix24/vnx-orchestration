"""tests/digest/test_progress.py — Unit tests for D2 digest progress + renderer.

6 tests:
  test_progress_yesterday_window_filter
  test_progress_zero_defaults_when_file_missing
  test_progress_skips_malformed_ndjson_line
  test_render_progress_section_table_format
  test_render_minimal_digest_with_no_decisions
  test_orchestrator_writes_to_state_dir_atomically

Note on import ordering: pytest adds tests/ to sys.path (for tests/digest/__init__.py),
which would shadow scripts/lib/digest. We evict the test-package shadow from sys.modules
here so all digest.* imports resolve to the library package in scripts/lib/digest.
"""

from __future__ import annotations

import json
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# --- path + sys.modules fix (must run before any digest.* import) ---
_REPO = Path(__file__).resolve().parents[2]
_LIB = _REPO / "scripts" / "lib"
_SCRIPTS = _REPO / "scripts"

sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(1, str(_SCRIPTS))

# Evict the tests/digest shadow from sys.modules so subsequent digest.* imports
# resolve to scripts/lib/digest (now at sys.path[0]). Preserve the current test
# module and any already-loaded conftest to avoid breaking pytest's collection.
_evict = [
    k for k in sys.modules
    if (k == "digest" or k.startswith("digest."))
    and "test_progress" not in k
    and "conftest" not in k
]
for _k in _evict:
    del sys.modules[_k]
# --- end path fix ---

from digest.collectors.progress import collect_progress  # noqa: E402
from digest.renderer import render_minimal_digest, render_progress_section  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_ndjson(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_progress_yesterday_window_filter(tmp_path: Path) -> None:
    """Events older than 24h must be excluded from all counts."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=25)
    recent = now - timedelta(hours=1)

    receipts = [
        {"event_type": "subprocess_completion", "status": "done", "timestamp": _iso(old)},
        {"event_type": "subprocess_completion", "status": "done", "timestamp": _iso(recent)},
        {"event_type": "pr_merged", "status": "done", "timestamp": _iso(old)},
        {"event_type": "pr_merged", "status": "done", "timestamp": _iso(recent)},
    ]
    state_dir = tmp_path / "state"
    _write_ndjson(state_dir / "t0_receipts.ndjson", receipts)
    _write_ndjson(state_dir / "open_items.ndjson", [])

    result = collect_progress(state_dir=state_dir, data_dir=tmp_path)

    # Only the 2 recent events are in window.
    assert result["dispatches"] == 2
    assert result["pr_merged"] == 1
    assert result["dispatch_success_rate"] == "100%"


def test_progress_zero_defaults_when_file_missing(tmp_path: Path) -> None:
    """Missing NDJSON files return a zeros dict, no exception raised."""
    state_dir = tmp_path / "state_missing"
    result = collect_progress(state_dir=state_dir, data_dir=tmp_path)

    assert result["dispatches"] == 0
    assert result["pr_merged"] == 0
    assert result["dispatch_success_rate"] == "n/a"
    assert result["ois_filed"] == 0
    assert result["ois_closed"] == 0
    assert result["auto_dream_cycles"] == 0
    assert result["failed_ci"] == 0


def test_progress_skips_malformed_ndjson_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed NDJSON line is skipped; the valid line still counts."""
    import logging

    now = datetime.now(timezone.utc)
    valid = {"event_type": "subprocess_completion", "status": "done", "timestamp": _iso(now)}

    state_dir = tmp_path / "state"
    receipts_path = state_dir / "t0_receipts.ndjson"
    receipts_path.parent.mkdir(parents=True, exist_ok=True)
    receipts_path.write_text("NOT VALID JSON\n" + json.dumps(valid) + "\n", encoding="utf-8")
    _write_ndjson(state_dir / "open_items.ndjson", [])

    with caplog.at_level(logging.DEBUG):
        result = collect_progress(state_dir=state_dir, data_dir=tmp_path)

    assert result["dispatches"] == 1
    assert result["dispatch_success_rate"] == "100%"


def test_render_progress_section_table_format() -> None:
    """render_progress_section must produce a markdown table with all metrics."""
    progress = {
        "pr_merged": 3,
        "dispatches": 10,
        "dispatch_success_rate": "90%",
        "ois_filed": 2,
        "ois_closed": 5,
        "auto_dream_cycles": 1,
        "failed_ci": 1,
    }
    output = render_progress_section(progress)

    assert "| Metric" in output
    assert "| PRs merged" in output
    assert "| 3 |" in output
    assert "| Success rate" in output
    assert "| 90% |" in output
    assert "| Dream cycles" in output
    assert "| 1 |" in output


def test_render_minimal_digest_with_no_decisions() -> None:
    """render_minimal_digest with manual_decisions=None must show the D3 placeholder."""
    progress = {
        "pr_merged": 0,
        "dispatches": 0,
        "dispatch_success_rate": "n/a",
        "ois_filed": 0,
        "ois_closed": 0,
        "auto_dream_cycles": 0,
        "failed_ci": 0,
    }
    output = render_minimal_digest(progress=progress, manual_decisions=None)

    assert "# VNX Decisions Digest" in output
    assert "Need YOUR decision" in output
    assert "D3" in output
    assert "Yesterday's Progress" in output


def test_orchestrator_writes_to_state_dir_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() must call atomic_write_text with state_dir/decisions_digest.md."""
    import build_decisions_digest  # imported inside test: digest.* shadow already evicted above

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

    with mock.patch("digest.io.atomic_write_text") as mock_aw:
        result = build_decisions_digest.main()

    assert result == 0
    mock_aw.assert_called_once()
    written_path = mock_aw.call_args[0][0]
    assert written_path == state_dir / "decisions_digest.md"
