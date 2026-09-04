"""Tests for OI D2: pytest-fixture receipts must not count as real work.

Measured 2026-09-04 on ~/.vnx-data/vnx-dev/state/t0_receipts.ndjson: 28,944
lines total, 7,738 (27%) carrying ``source == "pytest"`` — synthetic test
entries (dispatch_id like "DISP-007"), never real dispatch work. Any
state-reader that folds them into recent-activity or a derived success rate
is reading a polluted number.

Covers:
- ``_is_pytest_noise_receipt`` — the shared predicate.
- ``_build_recent_receipts`` excludes pytest-source receipts.
- A before/after count on a constructed ledger: the delta must equal exactly
  the number of pytest-source receipts injected (OI D2's own "test your
  filter" requirement — a filter that removes too much is as wrong as one
  that removes nothing).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make scripts/ importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_ndjson(state_dir: Path, entries: list[dict]) -> Path:
    p = state_dir / "t0_receipts.ndjson"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(json.dumps(e) for e in entries)
    p.write_text(lines + "\n", encoding="utf-8")
    return p


def _real_receipt(dispatch_id: str, timestamp: str) -> dict:
    return {
        "event_type": "subprocess_completion",
        "dispatch_id": dispatch_id,
        "status": "done",
        "project_id": "vnx-dev",
        "terminal": "T1",
        "timestamp": timestamp,
    }


def _pytest_receipt(dispatch_id: str, timestamp: str) -> dict:
    return {
        "event_type": "task_complete",
        "dispatch_id": dispatch_id,
        "status": "success",
        "project_id": "vnx-dev",
        "terminal": "T1",
        "timestamp": timestamp,
        "source": "pytest",
    }


class TestIsPytestNoiseReceipt:
    def test_source_pytest_is_noise(self) -> None:
        from build_t0_state import _is_pytest_noise_receipt
        assert _is_pytest_noise_receipt({"source": "pytest"}) is True

    def test_source_pytest_case_insensitive(self) -> None:
        from build_t0_state import _is_pytest_noise_receipt
        assert _is_pytest_noise_receipt({"source": "PyTest"}) is True

    def test_real_source_is_not_noise(self) -> None:
        from build_t0_state import _is_pytest_noise_receipt
        assert _is_pytest_noise_receipt({"source": "subprocess"}) is False

    def test_missing_source_is_not_noise(self) -> None:
        from build_t0_state import _is_pytest_noise_receipt
        assert _is_pytest_noise_receipt({}) is False


class TestBuildRecentReceiptsExcludesPytestNoise:
    def _fn(self, state_dir: Path, project_id: str = "vnx-dev", limit: int = 20) -> list:
        from build_t0_state import _build_recent_receipts
        os.environ.pop("VNX_USE_CENTRAL_DB", None)
        return _build_recent_receipts(state_dir, project_id=project_id, limit=limit)

    def test_pytest_source_receipt_excluded(self, tmp_path: Path) -> None:
        entries = [
            _real_receipt("real-1", "2026-06-03T10:00:00Z"),
            _pytest_receipt("DISP-007", "2026-06-03T10:05:00Z"),
        ]
        state_dir = tmp_path / "state"
        _make_ndjson(state_dir, entries)
        result = self._fn(state_dir)
        ids = [r["dispatch_id"] for r in result]
        assert "real-1" in ids
        assert "DISP-007" not in ids

    def test_before_after_count_delta_equals_injected_pytest_count(self, tmp_path: Path) -> None:
        """OI D2's own instruction: prove the filter removes exactly the
        pytest receipts, not more and not less."""
        real_entries = [
            _real_receipt(f"real-{i:03d}", f"2026-06-03T{i:02d}:00:00Z")
            for i in range(12)
        ]
        pytest_entries = [
            _pytest_receipt(f"DISP-{i:03d}", f"2026-06-04T{i:02d}:00:00Z")
            for i in range(7)
        ]
        state_dir = tmp_path / "state"
        _make_ndjson(state_dir, real_entries + pytest_entries)

        # "Before": count what a naive scan of the raw file would see.
        raw_lines = (state_dir / "t0_receipts.ndjson").read_text(encoding="utf-8").splitlines()
        before_count = sum(1 for ln in raw_lines if ln.strip())
        assert before_count == 19

        after = self._fn(state_dir, limit=100)
        after_count = len(after)

        assert before_count - after_count == 7, (
            f"expected the delta to equal exactly the 7 injected pytest "
            f"receipts, got before={before_count} after={after_count}"
        )
        assert after_count == 12
        for r in after:
            assert not r["dispatch_id"].startswith("DISP-")

    def test_pytest_receipt_with_real_looking_dispatch_id_still_excluded(self, tmp_path: Path) -> None:
        """A pytest receipt whose dispatch_id happens to collide with a real
        naming scheme must still be excluded — the filter keys off
        ``source``, not the shape of dispatch_id."""
        entries = [
            _pytest_receipt("20260904-120000-realistic-id", "2026-06-03T10:00:00Z"),
        ]
        state_dir = tmp_path / "state"
        _make_ndjson(state_dir, entries)
        result = self._fn(state_dir)
        assert result == []
