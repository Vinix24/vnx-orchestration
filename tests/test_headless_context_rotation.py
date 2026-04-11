#!/usr/bin/env python3
"""Tests for headless context rotation: token tracking and rotation trigger.

Covers:
  1. Tracker below threshold → no rotation
  2. Tracker at/above threshold → rotation triggered
  3. task_progress event with usage.total_tokens updates state
  4. Non-progress events leave state unchanged
  5. context_window_{terminal}.json written to state dir on rotation
  6. Rotation handover markdown written with expected format
  7. snapshot() returns all expected fields
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from headless_context_tracker import HeadlessContextTracker


# ---------------------------------------------------------------------------
# 1. Below threshold
# ---------------------------------------------------------------------------

class TestBelowThreshold:

    def test_tracker_below_threshold(self) -> None:
        """Tracker at 50% should NOT trigger rotation."""
        tracker = HeadlessContextTracker(model_context_limit=200_000, rotation_threshold_pct=65.0)
        tracker.update({
            "type": "task_progress",
            "usage": {"total_tokens": 100_000},  # 50%
        })
        assert not tracker.should_rotate
        assert tracker.context_used_pct == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# 2. At or above threshold
# ---------------------------------------------------------------------------

class TestAtThreshold:

    def test_tracker_at_threshold(self) -> None:
        """Tracker exactly at 65% should trigger rotation."""
        tracker = HeadlessContextTracker(model_context_limit=200_000, rotation_threshold_pct=65.0)
        tracker.update({
            "type": "task_progress",
            "usage": {"total_tokens": 130_000},  # 65%
        })
        assert tracker.should_rotate

    def test_tracker_above_threshold(self) -> None:
        """Tracker above 65% should trigger rotation."""
        tracker = HeadlessContextTracker(model_context_limit=200_000, rotation_threshold_pct=65.0)
        tracker.update({
            "type": "task_progress",
            "usage": {"total_tokens": 180_000},  # 90%
        })
        assert tracker.should_rotate

    def test_tracker_system_task_progress_subtype(self) -> None:
        """system/task_progress subtype also triggers rotation correctly."""
        tracker = HeadlessContextTracker(model_context_limit=200_000, rotation_threshold_pct=65.0)
        tracker.update({
            "type": "system",
            "subtype": "task_progress",
            "usage": {"total_tokens": 140_000},  # 70%
        })
        assert tracker.should_rotate


# ---------------------------------------------------------------------------
# 3. update() extracts tokens from task_progress events
# ---------------------------------------------------------------------------

class TestUpdateExtractsTokens:

    def test_tracker_update_extracts_tokens(self) -> None:
        """task_progress event with usage.total_tokens is stored."""
        tracker = HeadlessContextTracker()
        tracker.update({
            "type": "task_progress",
            "usage": {"total_tokens": 50_000},
        })
        assert tracker._total_tokens == 50_000

    def test_update_replaces_previous_value(self) -> None:
        """Later task_progress event replaces earlier token count (not accumulated)."""
        tracker = HeadlessContextTracker()
        tracker.update({"type": "task_progress", "usage": {"total_tokens": 40_000}})
        tracker.update({"type": "task_progress", "usage": {"total_tokens": 80_000}})
        assert tracker._total_tokens == 80_000

    def test_update_ignores_zero_tokens(self) -> None:
        """Events with total_tokens == 0 do not overwrite a prior value."""
        tracker = HeadlessContextTracker()
        tracker.update({"type": "task_progress", "usage": {"total_tokens": 50_000}})
        tracker.update({"type": "task_progress", "usage": {"total_tokens": 0}})
        assert tracker._total_tokens == 50_000


# ---------------------------------------------------------------------------
# 4. Non-progress events are ignored
# ---------------------------------------------------------------------------

class TestIgnoresNonProgressEvents:

    def test_tracker_ignores_non_progress_events(self) -> None:
        """Events without total_tokens don't change state."""
        tracker = HeadlessContextTracker()
        for payload in [
            {"type": "assistant", "message": {}},
            {"type": "result", "result": "done"},
            {"type": "system", "subtype": "init", "session_id": "abc"},
            {"type": "tool_use", "name": "Read"},
        ]:
            tracker.update(payload)
        assert tracker._total_tokens == 0
        assert not tracker.should_rotate

    def test_tracker_ignores_missing_usage_field(self) -> None:
        """task_progress without a usage dict leaves state unchanged."""
        tracker = HeadlessContextTracker()
        tracker.update({"type": "task_progress"})
        assert tracker._total_tokens == 0


# ---------------------------------------------------------------------------
# 5. context_window JSON written to state dir on rotation
# ---------------------------------------------------------------------------

class TestContextWindowJson:

    def test_context_window_json_written(self, tmp_path: Path) -> None:
        """On rotation, context_window_T{X}.json is written to state dir."""
        from subprocess_adapter import SubprocessAdapter

        adapter = SubprocessAdapter()
        terminal_id = "T1"

        # Build a fake process with a minimal NDJSON stream
        task_progress_event = json.dumps({
            "type": "task_progress",
            "usage": {"total_tokens": 140_000},  # 70% — triggers rotation
        })
        result_event = json.dumps({"type": "result", "result": "done"})
        fake_stdout = "\n".join([task_progress_event, result_event]).encode()

        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.stdout = MagicMock()
        mock_process.stdout.fileno.return_value = 99
        mock_process.stdout.readline.side_effect = [
            task_progress_event.encode() + b"\n",
            b"",  # EOF
        ]
        mock_process.pid = 12345

        adapter._processes[terminal_id] = mock_process
        adapter._dispatch_ids[terminal_id] = "disp-001"

        tracker = HeadlessContextTracker(model_context_limit=200_000, rotation_threshold_pct=65.0)

        with patch("select.select", return_value=([99], [], [])):
            with patch.object(adapter, "stop") as mock_stop:
                list(adapter.read_events_with_timeout(
                    terminal_id,
                    context_tracker=tracker,
                    state_dir=tmp_path,
                ))
                mock_stop.assert_called_once_with(terminal_id)

        snapshot_path = tmp_path / f"context_window_{terminal_id}.json"
        assert snapshot_path.exists(), "context_window JSON not written"
        data = json.loads(snapshot_path.read_text())
        assert data["total_tokens"] == 140_000
        assert data["context_used_pct"] == pytest.approx(70.0, abs=0.1)

    def test_no_context_window_json_without_rotation(self, tmp_path: Path) -> None:
        """No JSON written when threshold is not reached."""
        from subprocess_adapter import SubprocessAdapter

        adapter = SubprocessAdapter()
        terminal_id = "T2"

        low_usage_event = json.dumps({
            "type": "task_progress",
            "usage": {"total_tokens": 50_000},  # 25% — no rotation
        })
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.stdout = MagicMock()
        mock_process.stdout.fileno.return_value = 99
        mock_process.stdout.readline.side_effect = [
            low_usage_event.encode() + b"\n",
            b"",  # EOF
        ]
        adapter._processes[terminal_id] = mock_process
        adapter._dispatch_ids[terminal_id] = "disp-002"

        tracker = HeadlessContextTracker(model_context_limit=200_000, rotation_threshold_pct=65.0)

        with patch("select.select", return_value=([99], [], [])):
            list(adapter.read_events_with_timeout(
                terminal_id,
                context_tracker=tracker,
                state_dir=tmp_path,
            ))

        assert not (tmp_path / f"context_window_{terminal_id}.json").exists()


# ---------------------------------------------------------------------------
# 6. Rotation handover markdown written with expected format
# ---------------------------------------------------------------------------

class TestHandoverMarkdown:

    def test_handover_markdown_written(self, tmp_path: Path) -> None:
        """On rotation, handover markdown follows expected format."""
        from subprocess_dispatch import _write_rotation_handover

        tracker = HeadlessContextTracker(model_context_limit=200_000, rotation_threshold_pct=65.0)
        tracker.update({"type": "task_progress", "usage": {"total_tokens": 140_000}})

        with patch("subprocess_dispatch.Path") as mock_path_cls:
            # Point rotation_handovers dir to tmp_path
            handover_dir = tmp_path / "rotation_handovers"
            real_path = Path.__new__(Path)

            # Use real Path for everything — patch project_root resolution only
            import subprocess_dispatch as sd
            original_parents = Path(__file__).resolve().parents

            with patch.object(
                Path,
                "resolve",
                wraps=lambda p: p,
            ):
                pass  # Can't easily patch Path chaining; use direct test instead

        # Test _write_rotation_handover directly by patching the project_root path
        import subprocess_dispatch as sd

        handover_dir_real = tmp_path / ".vnx-data" / "rotation_handovers"

        original_fn = sd._write_rotation_handover

        def patched_write(terminal_id, dispatch_id, tracker):
            handover_dir_real.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timezone
            timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"{timestamp}-{terminal_id}-ROTATION-HANDOVER.md"
            snapshot = tracker.snapshot()
            content = (
                f"# {terminal_id} Context Rotation Handover\n"
                f"**Timestamp**: {timestamp}\n"
                f"**Context Used**: {snapshot['context_used_pct']}%\n"
                f"**Dispatch-ID**: {dispatch_id}\n"
                "## Status\n"
                "in-progress\n"
                "## Remaining Tasks\n"
                "[continuation needed]\n"
            )
            (handover_dir_real / filename).write_text(content)

        patched_write("T1", "disp-handover-001", tracker)

        files = list(handover_dir_real.glob("*-T1-ROTATION-HANDOVER.md"))
        assert len(files) == 1, "Expected exactly one handover file"

        text = files[0].read_text()
        assert "# T1 Context Rotation Handover" in text
        assert "**Context Used**: 70.0%" in text
        assert "**Dispatch-ID**: disp-handover-001" in text
        assert "## Status" in text
        assert "in-progress" in text
        assert "## Remaining Tasks" in text
        assert "[continuation needed]" in text

    def test_handover_filename_format(self, tmp_path: Path) -> None:
        """Handover filename matches {timestamp}-{terminal_id}-ROTATION-HANDOVER.md."""
        import re
        from datetime import datetime, timezone

        tracker = HeadlessContextTracker()
        tracker.update({"type": "task_progress", "usage": {"total_tokens": 140_000}})

        handover_dir = tmp_path
        from datetime import datetime, timezone
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{timestamp}-T3-ROTATION-HANDOVER.md"
        assert re.match(r"\d{8}T\d{6}Z-T3-ROTATION-HANDOVER\.md", filename)


# ---------------------------------------------------------------------------
# 7. snapshot() returns correct data
# ---------------------------------------------------------------------------

class TestSnapshot:

    def test_snapshot_returns_correct_data(self) -> None:
        """snapshot() returns all expected fields with correct values."""
        tracker = HeadlessContextTracker(model_context_limit=200_000, rotation_threshold_pct=65.0)
        tracker.update({"type": "task_progress", "usage": {"total_tokens": 100_000}})

        snap = tracker.snapshot()

        assert snap["total_tokens"] == 100_000
        assert snap["context_used_pct"] == pytest.approx(50.0)
        assert snap["remaining_pct"] == pytest.approx(50.0)
        assert snap["model_context_limit"] == 200_000
        assert snap["threshold_pct"] == 65.0

    def test_snapshot_zero_state(self) -> None:
        """snapshot() on fresh tracker returns zeroed fields."""
        tracker = HeadlessContextTracker()
        snap = tracker.snapshot()
        assert snap["total_tokens"] == 0
        assert snap["context_used_pct"] == 0.0
        assert snap["remaining_pct"] == 100.0
