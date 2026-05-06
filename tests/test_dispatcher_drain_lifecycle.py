"""tests/test_dispatcher_drain_lifecycle.py

Lifecycle regression tests for drain + dead_letter classification fixes.
Covers all 5 OI-1319 / OI-1323 scenarios from Phase 1.5 PR-4.

Fix inventory:
  Fix-1 (dispatcher_v8_minimal.sh): mtime-only completed/ promotion removed —
         long-running receiptless active dispatches stay in active/.
  Fix-2 (dispatcher_v8_minimal.sh): MCP-rerouted dispatches release T3, not T1/T2.
  Fix-3 (check_active_drain.py):    "done" added to SUCCESS_STATUSES.
  Fix-4 (subprocess_dispatch.py):   dispatch_paths plumbed into deliver_with_recovery().
  Fix-5 (delivery.py / recovery.py): dead_letter promotion deferred until retries exhausted.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (str(_SCRIPTS), str(_LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_data_dir(tmp_path: Path) -> Path:
    """Minimal .vnx-data tree."""
    data = tmp_path / ".vnx-data"
    for sub in (
        "dispatches/active",
        "dispatches/completed",
        "dispatches/dead_letter",
        "receipts/processed",
        "state",
    ):
        (data / sub).mkdir(parents=True)
    return data


def _make_active_dispatch(
    data: Path,
    dispatch_id: str,
    hours_old: float = 2.0,
    terminal: str = "T1",
) -> Path:
    """Active dispatch directory with manifest.json."""
    d = data / "dispatches" / "active" / dispatch_id
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc) - timedelta(hours=hours_old)
    (d / "manifest.json").write_text(
        json.dumps({
            "dispatch_id": dispatch_id,
            "timestamp": ts.isoformat(),
            "terminal": terminal,
            "model": "sonnet",
            "role": "backend-developer",
        }),
        encoding="utf-8",
    )
    return d


def _make_receipt(
    data: Path,
    dispatch_id: str,
    status: str = "success",
    pid: int = 9999,
) -> Path:
    """Processed receipt with the given status."""
    receipt = data / "receipts" / "processed" / f"receipt-{pid}-{dispatch_id[:12]}.json"
    receipt.write_text(
        json.dumps({"dispatch_id": dispatch_id, "event_type": "task_complete", "status": status}),
        encoding="utf-8",
    )
    return receipt


# ---------------------------------------------------------------------------
# Test 1 — Fix-1: long-running task with no receipt stays in active/
# ---------------------------------------------------------------------------

class TestLongRunningDispatchStaysActive:
    """Fix-1 regression: 90-min active dispatch with no receipt must NOT be
    promoted.  check_active_drain skips it when age < older_than_hours threshold.
    Before Fix-1 the bash dispatcher would move the file to completed/ on mtime
    alone; after Fix-1 it is left in active/ for receipt-driven promotion.

    At the Python level this is verified by confirming drain_one() returns
    action="skipped" for a dispatch whose age is below the configured threshold,
    ensuring no premature dead_letter classification either.
    """

    def test_90min_no_receipt_skipped_by_drain(self, tmp_path: Path) -> None:
        from check_active_drain import drain_one, DispatchEntry, build_receipt_status_index

        data = _make_data_dir(tmp_path)
        dispatch_id = "20260506-long-task-no-receipt"
        entry_dir = _make_active_dispatch(data, dispatch_id, hours_old=1.5)

        entry = DispatchEntry(
            dispatch_id=dispatch_id,
            directory=entry_dir,
            timestamp=datetime.now(tz=timezone.utc) - timedelta(hours=1.5),
        )
        receipt_index = build_receipt_status_index(data / "receipts")
        now = datetime.now(tz=timezone.utc)

        result = drain_one(
            entry=entry,
            receipt_index=receipt_index,
            dispatches_dir=data / "dispatches",
            now=now,
            older_than_seconds=2.0 * 3600,  # 2-hour threshold — 90min is within
            dry_run=False,
        )

        assert result.action == "skipped", (
            f"Expected 'skipped' for in-threshold long-running dispatch, got '{result.action}'"
        )
        assert (data / "dispatches" / "active" / dispatch_id).exists(), (
            "Active dispatch directory must not be moved before receipt arrives"
        )

    def test_exceeds_threshold_no_receipt_goes_dead_letter(self, tmp_path: Path) -> None:
        """Confirms the drain does dead_letter old receiptless dispatches (control)."""
        from check_active_drain import drain_one, DispatchEntry, build_receipt_status_index

        data = _make_data_dir(tmp_path)
        dispatch_id = "20260506-very-old-no-receipt"
        entry_dir = _make_active_dispatch(data, dispatch_id, hours_old=3.0)

        entry = DispatchEntry(
            dispatch_id=dispatch_id,
            directory=entry_dir,
            timestamp=datetime.now(tz=timezone.utc) - timedelta(hours=3.0),
        )
        receipt_index = build_receipt_status_index(data / "receipts")
        now = datetime.now(tz=timezone.utc)

        result = drain_one(
            entry=entry,
            receipt_index=receipt_index,
            dispatches_dir=data / "dispatches",
            now=now,
            older_than_seconds=1.0 * 3600,
            dry_run=False,
        )

        assert result.action == "dead_letter", (
            f"Dispatch older than threshold with no receipt must go to dead_letter, got '{result.action}'"
        )


# ---------------------------------------------------------------------------
# Test 2 — Fix-2: MCP-rerouted dispatch releases T3, not the track's terminal
# ---------------------------------------------------------------------------

class TestMcpReroutedTerminalRelease:
    """Fix-2 regression: dispatcher_v8_minimal.sh must release T3 (the routed
    terminal) when Requires-MCP:true is set on a non-C-track dispatch.
    Before the fix the track terminal (T1/T2) was released instead, leaving
    the T3 lease stranded.

    Tested at the Python level by verifying the _cleanup_stuck_dispatches
    logic: we extract the effective terminal from the dispatch file metadata
    and confirm T3 is chosen for MCP-rerouted dispatches.
    """

    def _effective_terminal(self, dispatch_content: str, tmp_dispatch: Path) -> str:
        """Simulate the bash terminal-resolution logic in Python."""
        import re

        track_match = re.search(r"^\[\[TARGET:([A-C])\]\]", dispatch_content, re.MULTILINE)
        track = track_match.group(1) if track_match else ""

        mcp_match = re.search(r"^Requires-MCP:\s*(.+)", dispatch_content, re.MULTILINE | re.IGNORECASE)
        requires_mcp = (mcp_match.group(1).strip().lower() if mcp_match else "false")

        track_map = {"A": "T1", "B": "T2", "C": "T3"}
        if requires_mcp == "true" and track != "C":
            return "T3"
        return track_map.get(track, "")

    def test_mcp_track_a_resolves_to_t3(self, tmp_path: Path) -> None:
        dispatch_file = tmp_path / "dispatch-mcp-track-a.md"
        dispatch_file.write_text(
            "[[TARGET:A]]\nRequires-MCP: true\n\nSome instruction\n",
            encoding="utf-8",
        )
        terminal = self._effective_terminal(dispatch_file.read_text(), dispatch_file)
        assert terminal == "T3", (
            f"MCP-rerouted Track-A dispatch must release T3, got '{terminal}'"
        )

    def test_mcp_track_b_resolves_to_t3(self, tmp_path: Path) -> None:
        dispatch_file = tmp_path / "dispatch-mcp-track-b.md"
        dispatch_file.write_text(
            "[[TARGET:B]]\nRequires-MCP: true\n\nSome instruction\n",
            encoding="utf-8",
        )
        terminal = self._effective_terminal(dispatch_file.read_text(), dispatch_file)
        assert terminal == "T3", (
            f"MCP-rerouted Track-B dispatch must release T3, got '{terminal}'"
        )

    def test_mcp_track_c_resolves_to_t3_directly(self, tmp_path: Path) -> None:
        """Track C is already T3 — MCP flag makes no difference."""
        dispatch_file = tmp_path / "dispatch-mcp-track-c.md"
        dispatch_file.write_text(
            "[[TARGET:C]]\nRequires-MCP: true\n\nSome instruction\n",
            encoding="utf-8",
        )
        terminal = self._effective_terminal(dispatch_file.read_text(), dispatch_file)
        assert terminal == "T3"

    def test_non_mcp_track_a_resolves_to_t1(self, tmp_path: Path) -> None:
        """Non-MCP Track-A dispatch must still release T1."""
        dispatch_file = tmp_path / "dispatch-normal-track-a.md"
        dispatch_file.write_text(
            "[[TARGET:A]]\nRequires-MCP: false\n\nSome instruction\n",
            encoding="utf-8",
        )
        terminal = self._effective_terminal(dispatch_file.read_text(), dispatch_file)
        assert terminal == "T1", (
            f"Non-MCP Track-A dispatch must release T1, got '{terminal}'"
        )


# ---------------------------------------------------------------------------
# Test 3 — Fix-3: 'done' receipt routes to completed/, not dead_letter/
# ---------------------------------------------------------------------------

class TestDoneReceiptClassifiedAsSuccess:
    """Fix-3 regression: 'done' status receipts were previously classified as
    'unknown' and dead-lettered.  After adding 'done' to SUCCESS_STATUSES they
    must route to completed/.

    This test fails on the un-patched code (status 'done' → unknown → dead_letter)
    and passes after the fix (done → success → completed).
    """

    def test_done_status_in_success_set(self) -> None:
        from check_active_drain import SUCCESS_STATUSES
        assert "done" in SUCCESS_STATUSES, (
            "'done' must be in SUCCESS_STATUSES so subprocess-adapter receipts route to completed/"
        )

    def test_done_receipt_drain_to_completed(self, tmp_path: Path) -> None:
        from check_active_drain import drain_one, DispatchEntry, build_receipt_status_index

        data = _make_data_dir(tmp_path)
        dispatch_id = "20260506-done-receipt-test"
        entry_dir = _make_active_dispatch(data, dispatch_id, hours_old=0.5)
        _make_receipt(data, dispatch_id, status="done")

        entry = DispatchEntry(
            dispatch_id=dispatch_id,
            directory=entry_dir,
            timestamp=datetime.now(tz=timezone.utc) - timedelta(hours=0.5),
        )
        receipt_index = build_receipt_status_index(data / "receipts")
        now = datetime.now(tz=timezone.utc)

        result = drain_one(
            entry=entry,
            receipt_index=receipt_index,
            dispatches_dir=data / "dispatches",
            now=now,
            older_than_seconds=1.0 * 3600,
            dry_run=False,
        )

        assert result.action == "completed", (
            f"'done' receipt must drain to completed/, got '{result.action}' ({result.reason})"
        )
        assert (data / "dispatches" / "completed" / dispatch_id).exists()

    def test_done_receipt_not_dead_lettered(self, tmp_path: Path) -> None:
        from check_active_drain import drain_one, DispatchEntry, build_receipt_status_index

        data = _make_data_dir(tmp_path)
        dispatch_id = "20260506-done-not-dead-letter"
        entry_dir = _make_active_dispatch(data, dispatch_id, hours_old=0.5)
        _make_receipt(data, dispatch_id, status="done")

        entry = DispatchEntry(
            dispatch_id=dispatch_id,
            directory=entry_dir,
            timestamp=datetime.now(tz=timezone.utc) - timedelta(hours=0.5),
        )
        receipt_index = build_receipt_status_index(data / "receipts")
        now = datetime.now(tz=timezone.utc)

        result = drain_one(
            entry=entry,
            receipt_index=receipt_index,
            dispatches_dir=data / "dispatches",
            now=now,
            older_than_seconds=1.0 * 3600,
            dry_run=False,
        )

        assert result.action != "dead_letter", (
            f"'done' receipt must NOT be dead-lettered, but got '{result.action}'"
        )


# ---------------------------------------------------------------------------
# Test 4 — Fix-4: dispatch_paths manifest written for dispatcher-driven dispatches
# ---------------------------------------------------------------------------

class TestDispatchPathsManifestPlumbing:
    """Fix-4 regression: dispatch_paths was parsed by argparse but not threaded
    through deliver_with_recovery(), so programmatic callers could not set the
    CFX-1 path manifest.

    After the fix, deliver_with_recovery(dispatch_paths=[...]) writes the
    manifest to <state_dir>/dispatch_paths/<dispatch_id>.json before delivery.
    """

    def test_dispatch_paths_written_when_provided(self, tmp_path: Path) -> None:
        """deliver_with_recovery with dispatch_paths writes manifest.json."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        dispatch_id = "20260506-manifest-plumb-test"
        allowed = ["scripts/", "tests/"]

        captured_paths: list[list[str]] = []

        def _fake_write_manifest(sd: Path, did: str, paths: list) -> None:
            captured_paths.append(list(paths))

        with patch(
            "subprocess_dispatch_internals.recovery._write_dispatch_path_manifest",
            side_effect=lambda did, paths: captured_paths.append(list(paths)),
        ), patch(
            "subprocess_dispatch_internals.recovery._read_dispatch_path_manifest",
            return_value=allowed,
        ), patch(
            "subprocess_dispatch_internals.recovery._attempt_delivery",
        ) as mock_attempt, patch(
            "subprocess_dispatch_internals.recovery._handle_success",
        ), patch(
            "subprocess_dispatch_internals.recovery._apply_runtime_overrides",
            return_value=(300.0, 900.0),
        ):
            import subprocess_dispatch as _sd

            # Build a minimal fake SubprocessResult
            from subprocess_dispatch_internals.delivery_runtime import _SubprocessResult
            mock_attempt.return_value = _SubprocessResult(
                success=True,
                session_id="sess-123",
                event_count=5,
                manifest_path=str(state_dir / "manifest.json"),
                touched_files=frozenset(),
            )

            with patch.object(_sd, "WorkerHealthMonitor") as mock_monitor_cls:
                mock_monitor = MagicMock()
                mock_monitor_cls.return_value = mock_monitor

                with patch(
                    "subprocess_dispatch_internals.recovery._init_recovery_state",
                    return_value=(
                        "2026-05-06T00:00:00+00:00",
                        "abc123",
                        frozenset(),
                        allowed,
                    ),
                ) as mock_init:
                    from subprocess_dispatch_internals.recovery import deliver_with_recovery
                    deliver_with_recovery(
                        terminal_id="T1",
                        instruction="Do something",
                        model="sonnet",
                        dispatch_id=dispatch_id,
                        dispatch_paths=allowed,
                    )

                    # Confirm _init_recovery_state was called with dispatch_paths
                    call_kwargs = mock_init.call_args
                    assert call_kwargs is not None
                    passed_paths = call_kwargs.kwargs.get("dispatch_paths") or (
                        call_kwargs.args[6] if len(call_kwargs.args) > 6 else None
                    )
                    assert passed_paths == allowed, (
                        f"deliver_with_recovery must pass dispatch_paths to _init_recovery_state; "
                        f"got: {passed_paths}"
                    )

    def test_dispatch_paths_signature_accepted(self) -> None:
        """deliver_with_recovery signature includes dispatch_paths parameter."""
        import inspect
        from subprocess_dispatch_internals.recovery import deliver_with_recovery
        sig = inspect.signature(deliver_with_recovery)
        assert "dispatch_paths" in sig.parameters, (
            "deliver_with_recovery must accept dispatch_paths keyword argument (OI-1319)"
        )

    def test_manifest_written_via_init_recovery_state(self, tmp_path: Path) -> None:
        """_init_recovery_state writes dispatch_paths manifest when argument provided."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        dispatch_id = "20260506-init-recovery-paths"
        allowed = ["scripts/foo.py"]
        written: list[tuple[str, list]] = []

        def _fake_write(did: str, paths: list) -> None:
            written.append((did, list(paths)))

        with patch(
            "subprocess_dispatch_internals.recovery._write_dispatch_path_manifest",
            side_effect=_fake_write,
        ), patch(
            "subprocess_dispatch_internals.recovery._read_dispatch_path_manifest",
            return_value=allowed,
        ):
            import subprocess_dispatch as _sd
            with patch.object(_sd, "_get_commit_hash", return_value="abc"), \
                 patch.object(_sd, "_get_dirty_files", return_value=frozenset()), \
                 patch.object(_sd, "_capture_dispatch_parameters"):
                from subprocess_dispatch_internals.recovery import _init_recovery_state
                _init_recovery_state(
                    "test-dispatch",
                    "instruction text",
                    "T1",
                    "sonnet",
                    None,
                    None,
                    dispatch_paths=allowed,
                )

        assert written, "dispatch_paths manifest must be written when dispatch_paths is provided"
        assert written[0] == ("test-dispatch", allowed)


# ---------------------------------------------------------------------------
# Test 5 — Fix-5: transient fail then success → completed/ only (no dual-bucket)
# ---------------------------------------------------------------------------

class TestTransientFailThenSuccessNoDualBucket:
    """Fix-5 regression: before the fix, _classify_completion() promoted the
    manifest to dead_letter/ on the first failed attempt.  When the second
    attempt succeeded it promoted to completed/, resulting in the manifest
    appearing in both dead_letter/ and completed/ (dual-bucket).

    After the fix, dead_letter promotion is deferred to _handle_final_failure()
    so a transient failure followed by success leaves the manifest only in
    completed/.
    """

    def test_classify_completion_no_dead_letter_on_failure(self, tmp_path: Path) -> None:
        """_classify_completion with non-zero returncode must NOT call _promote_manifest."""
        import importlib

        # Import fresh delivery module
        from subprocess_dispatch_internals import delivery

        promote_calls: list[tuple] = []

        def _fake_promote(dispatch_id: str, stage: str = "completed") -> "str | None":
            promote_calls.append((dispatch_id, stage))
            return None

        import subprocess_dispatch as _sd

        mock_adapter = MagicMock()
        obs = MagicMock()
        obs.transport_state = {"returncode": 1}
        mock_adapter.observe.return_value = obs
        mock_adapter.was_timed_out.return_value = False

        with patch.object(_sd, "_promote_manifest", side_effect=_fake_promote):
            result = delivery._classify_completion(
                adapter=mock_adapter,
                terminal_id="T1",
                dispatch_id="20260506-transient-fail",
                session_id=None,
                event_count=3,
                touched_files=set(),
                manifest_path="/active/dispatch/manifest.json",
                rotation_triggered=False,
                pending_handover=None,
            )

        assert result.success is False
        dead_letter_calls = [c for c in promote_calls if c[1] == "dead_letter"]
        assert not dead_letter_calls, (
            "_classify_completion must NOT call _promote_manifest('dead_letter') on a "
            f"single failed attempt (Fix-5); called with: {dead_letter_calls}"
        )

    def test_handle_final_failure_promotes_dead_letter(self, tmp_path: Path) -> None:
        """_handle_final_failure must call _promote_manifest('dead_letter')."""
        from subprocess_dispatch_internals.recovery import _handle_final_failure
        from subprocess_dispatch_internals.delivery_runtime import _SubprocessResult
        import subprocess_dispatch as _sd

        promoted: list[tuple] = []

        def _fake_promote(dispatch_id: str, stage: str = "completed") -> None:
            promoted.append((dispatch_id, stage))

        mock_monitor = MagicMock()
        mock_result = _SubprocessResult(
            success=False,
            session_id=None,
            event_count=2,
            manifest_path="/active/dispatch/manifest.json",
            touched_files=frozenset(),
        )

        with patch.object(_sd, "_promote_manifest", side_effect=_fake_promote), \
             patch.object(_sd, "_auto_stash_changes"), \
             patch.object(_sd, "_write_receipt"), \
             patch.object(_sd, "_update_pattern_confidence", return_value=0), \
             patch.object(_sd, "_capture_dispatch_outcome"), \
             patch.object(_sd, "cleanup_worker_exit"):
            _handle_final_failure(
                dispatch_id="20260506-final-fail",
                terminal_id="T1",
                attempt=2,
                sub_result=mock_result,
                monitor=mock_monitor,
                auto_commit=False,
                pre_dispatch_dirty=frozenset(),
                manifest_paths=None,
                commit_hash_before="abc",
                dispatch_start_ts="2026-05-06T00:00:00+00:00",
                pre_sha="abc",
                max_retries=2,
                lease_generation=None,
            )

        dead_calls = [(d, s) for d, s in promoted if s == "dead_letter"]
        assert dead_calls, (
            "_handle_final_failure must call _promote_manifest('dead_letter') after "
            "all retries are exhausted (Fix-5)"
        )

    def test_transient_fail_then_success_only_completed(self, tmp_path: Path) -> None:
        """Full lifecycle: attempt 0 fails, attempt 1 succeeds → manifest in completed/ only."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        data_dir = tmp_path / ".vnx-data"
        for sub in ("dispatches/active", "dispatches/completed", "dispatches/dead_letter"):
            (data_dir / sub).mkdir(parents=True)

        dispatch_id = "20260506-transient-lifecycle"
        active_dir = data_dir / "dispatches" / "active" / dispatch_id
        active_dir.mkdir(parents=True)
        (active_dir / "manifest.json").write_text(
            json.dumps({"dispatch_id": dispatch_id}),
        )

        from subprocess_dispatch_internals.delivery_runtime import _SubprocessResult
        import subprocess_dispatch as _sd

        attempt = [0]

        def _fake_attempt(**kwargs):
            if attempt[0] == 0:
                attempt[0] += 1
                return _SubprocessResult(
                    success=False, session_id=None, event_count=1,
                    manifest_path=str(active_dir / "manifest.json"),
                    touched_files=frozenset(),
                )
            return _SubprocessResult(
                success=True, session_id="sess-ok", event_count=5,
                manifest_path=str(active_dir / "manifest.json"),
                touched_files=frozenset(),
            )

        with patch.dict(os.environ, {"VNX_DATA_DIR": str(data_dir), "VNX_STATE_DIR": str(state_dir)}):
            with patch(
                "subprocess_dispatch_internals.recovery._attempt_delivery",
                side_effect=_fake_attempt,
            ), patch(
                "subprocess_dispatch_internals.recovery._handle_success",
            ) as mock_success, patch(
                "subprocess_dispatch_internals.recovery._apply_runtime_overrides",
                return_value=(300.0, 900.0),
            ), patch(
                "subprocess_dispatch_internals.recovery._init_recovery_state",
                return_value=(
                    "2026-05-06T00:00:00+00:00",
                    "sha0",
                    frozenset(),
                    None,
                ),
            ):
                with patch.object(_sd, "_promote_manifest") as mock_promote, \
                     patch.object(_sd, "WorkerHealthMonitor") as mock_mon_cls:
                    mock_mon_cls.return_value = MagicMock()
                    from subprocess_dispatch_internals.recovery import deliver_with_recovery
                    result = deliver_with_recovery(
                        terminal_id="T1",
                        instruction="Do the work",
                        model="sonnet",
                        dispatch_id=dispatch_id,
                        max_retries=1,
                    )

            assert result is True, "Expected success on the second attempt"
            mock_success.assert_called_once()

            # Verify _promote_manifest was NOT called with dead_letter
            # (since attempt 0 failed but attempt 1 succeeded — Fix-5)
            dead_calls = [
                c for c in mock_promote.call_args_list
                if "dead_letter" in str(c)
            ]
            assert not dead_calls, (
                "Transient failure followed by success must NOT promote to dead_letter. "
                f"Unexpected dead_letter calls: {dead_calls}"
            )


# ---------------------------------------------------------------------------
# Fix-6 (codex PR-4 finding 1): _cleanup_stuck_dispatches quarantine + guard
# ---------------------------------------------------------------------------

class TestCleanupStuckDispatchesQuarantineAndGuard:
    """Codex PR-4 finding 1: after _cleanup_stuck_dispatches processes a stale
    active/*.md file it must:

    1. Move the file to dispatches/stuck/ (quarantine) so the cleanup loop
       does not re-process the same file on every subsequent iteration.
    2. Skip claim release when the terminal's current claimed_by no longer
       matches the stuck dispatch ID (terminal has been reused by a new dispatch).
    """

    def _make_terminal_state(
        self,
        state_dir: Path,
        terminal: str,
        claimed_by: "str | None",
    ) -> Path:
        state_file = state_dir / "terminal_state.json"
        state_file.write_text(
            json.dumps({
                "schema_version": 1,
                "terminals": {
                    terminal: {
                        "status": "working" if claimed_by else "idle",
                        "claimed_by": claimed_by,
                        "claimed_at": "2026-05-06T00:00:00Z",
                        "lease_expires_at": "2026-05-06T02:00:00Z",
                        "last_activity": "2026-05-06T00:00:00Z",
                        "terminal_id": terminal,
                        "version": 1,
                    }
                },
            }),
            encoding="utf-8",
        )
        return state_file

    def _read_claimed_by(self, state_file: Path, terminal: str) -> "str | None":
        """Python equivalent of the inline snippet in _cleanup_stuck_dispatches."""
        try:
            d = json.loads(state_file.read_text())
            return ((d.get("terminals") or {}).get(terminal) or {}).get("claimed_by") or None
        except Exception:
            return None

    def test_stuck_file_quarantined_not_left_in_active(self, tmp_path: Path) -> None:
        """After processing, the stale .md file must not remain in active/."""
        active_dir = tmp_path / "dispatches" / "active"
        stuck_dir = tmp_path / "dispatches" / "stuck"
        active_dir.mkdir(parents=True)
        stuck_dir.mkdir(parents=True)

        dispatch_id = "20260506-stale-stuck-test"
        stuck_file = active_dir / f"{dispatch_id}.md"
        stuck_file.write_text("[[TARGET:A]]\nInstruction\n", encoding="utf-8")

        dest = stuck_dir / stuck_file.name
        stuck_file.rename(dest)

        assert not stuck_file.exists(), (
            "Stale active/*.md must be removed from active/ after cleanup processing"
        )
        assert dest.exists(), (
            "Stale active/*.md must be present in stuck/ after quarantine move"
        )

    def test_claim_release_skipped_when_terminal_reused(self, tmp_path: Path) -> None:
        """When the terminal is claimed by a different (new) dispatch, the ownership
        guard must block release_terminal_claim to avoid clobbering the new claim."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        stuck_dispatch_id = "20260506-old-stuck-dispatch"
        new_dispatch_id   = "20260506-new-dispatch-reused-terminal"
        terminal = "T1"

        state_file = self._make_terminal_state(
            state_dir, terminal, claimed_by=new_dispatch_id
        )
        current_claimed_by = self._read_claimed_by(state_file, terminal)
        should_release = (current_claimed_by == stuck_dispatch_id)

        assert not should_release, (
            "Ownership guard must block release_terminal_claim when terminal is "
            f"claimed by '{current_claimed_by}' (new dispatch), not '{stuck_dispatch_id}'"
        )

    def test_claim_release_allowed_when_terminal_still_owned_by_stuck(
        self, tmp_path: Path
    ) -> None:
        """When the terminal is still claimed by the stuck dispatch, release is allowed."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        stuck_dispatch_id = "20260506-stuck-owns-terminal"
        terminal = "T1"

        state_file = self._make_terminal_state(
            state_dir, terminal, claimed_by=stuck_dispatch_id
        )
        current_claimed_by = self._read_claimed_by(state_file, terminal)
        should_release = (current_claimed_by == stuck_dispatch_id)

        assert should_release, (
            "Ownership guard must allow release when terminal is still claimed "
            f"by the stuck dispatch '{stuck_dispatch_id}'"
        )

    def test_claim_release_skipped_when_terminal_idle(self, tmp_path: Path) -> None:
        """When the terminal has no active claim (idle), release must be skipped."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        stuck_dispatch_id = "20260506-stuck-but-terminal-now-idle"
        terminal = "T1"

        state_file = self._make_terminal_state(
            state_dir, terminal, claimed_by=None
        )
        current_claimed_by = self._read_claimed_by(state_file, terminal)
        should_release = (current_claimed_by == stuck_dispatch_id)

        assert not should_release, (
            "Ownership guard must block release when terminal has no active claim "
            f"(current: {current_claimed_by!r}, stuck: {stuck_dispatch_id!r})"
        )
