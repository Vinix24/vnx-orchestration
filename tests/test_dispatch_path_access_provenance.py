"""test_dispatch_path_access_provenance.py — OI-1271: access=read becomes a write grant.

The spec knows a per-path access right (``DispatchPath.access``), and
``worker_permissions.resolve_dispatch_write_scope`` knows how to honour it — but
the single-entry door drops the right on the floor before the tmux lane ever
sees it. ``dispatch_cli.py:1868`` builds the ``dispatch_paths`` kwarg for
``TmuxInteractiveDispatch.dispatch`` as ``[str(dp.path) for dp in
plan.dispatch_paths]``: plain path strings, no ``access`` anywhere. Downstream,
``worker_permissions._parse_dispatch_path_entry`` treats a bare string (no
``:access`` suffix) as ``PathAccess.READ_WRITE`` — so a path the spec declared
``access=read`` silently becomes writable once it reaches the worker-scope
enforcement hook.

This is a HERKOMST test, not a parser test: it never hand-writes a
``"path:read"`` string and asserts the parser reads it back correctly (that
already works and is not the bug). Instead it captures what the door actually
hands to ``TmuxInteractiveDispatch.dispatch`` — by monkeypatching that method
and recording its kwargs — for a spec whose ``DispatchPath`` carries
``access=read``, and only then feeds that captured, door-produced value into
``resolve_dispatch_write_scope``. The assertion hangs off the real call chain
(``dispatch_cli._execute_claude`` -> ``TmuxInteractiveDispatch.dispatch`` ->
``worker_permissions.resolve_dispatch_write_scope``), not off a value the test
itself typed in.

Every test in this file exercises ``dispatch_cli._execute_claude`` on
unmodified production code. ``test_read_access_path_...`` and
``test_mixed_access_paths_...`` are RED on origin/main (the defect); the
``read_write`` regression test is GREEN on main today and must stay GREEN
once the fix lands, since it is the proof the fix has not narrowed anything
that used to work.
"""
from __future__ import annotations

import fnmatch
import hashlib
import sys
from pathlib import Path, PurePosixPath

from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import _execute_claude
from dispatch_internal import issue_permit
from dispatch_plan import ExecutionPlan
from dispatch_spec import DispatchPath, Isolation, PathAccess, Provider
from worker_permissions import resolve_dispatch_write_scope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_instruction_file(tmp_path: Path, text: str = "# OI-1271 provenance test\n") -> Path:
    f = tmp_path / "instruction.md"
    f.write_text(text, encoding="utf-8")
    return f


def _make_plan(tmp_path: Path, dispatch_paths: "tuple[DispatchPath, ...]") -> ExecutionPlan:
    """A valid claude-lane plan carrying *dispatch_paths* with a matching instruction sha256.

    Mirrors ``_make_mcp_plan`` in test_requires_mcp_propagation.py — the same
    ``_execute_claude`` call shape, just varying ``dispatch_paths`` instead of
    ``requires_mcp``.
    """
    ifile = _fake_instruction_file(tmp_path)
    sha = hashlib.sha256(ifile.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    return ExecutionPlan(
        dispatch_id="oi1271-exec-001",
        project_id="vnx-dev",
        provider=Provider.CLAUDE,
        model="sonnet",
        lane="claude_tmux_subscription",
        adapter="tmux_claude",
        target_id="ephemeral",
        billing="subscription",
        serialization_class="claude-tmux",
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="verify_strict",
        deadline_seconds=3600,
        base_ref="origin/main",
        dispatch_paths=dispatch_paths,
        instruction_file=ifile,
        route_reason="D11,D3,D1,D2,D4,D5,D6,D7,D8,D9,D10,D12",
        instruction_sha256=sha,
    )


def _capture_door_forwarded_dispatch_paths(tmp_path: Path, dispatch_paths: "tuple[DispatchPath, ...]") -> "list[str]":
    """Drive the real door path (_execute_claude -> TmuxInteractiveDispatch.dispatch)
    and return exactly what the door handed the tmux lane as ``dispatch_paths``.

    This is the HERKOMST capture point: whatever list comes back here is what
    line 1868 of dispatch_cli.py actually produced, not a string the test wrote.
    """
    plan = _make_plan(tmp_path, dispatch_paths)
    permit = issue_permit(plan)
    with patch(
        "tmux_interactive_dispatch.TmuxInteractiveDispatch.dispatch",
        return_value=MagicMock(success=True),
    ) as mock_dispatch:
        _execute_claude(plan, permit, state_dir=tmp_path / "state", data_dir=tmp_path)
    mock_dispatch.assert_called_once()
    _, kwargs = mock_dispatch.call_args
    assert "dispatch_paths" in kwargs
    return kwargs["dispatch_paths"]


def _is_in_write_scope(file_path: str, write_scope: "list[str] | None") -> bool:
    if write_scope is None:
        return True
    return any(fnmatch.fnmatch(file_path, scope) for scope in write_scope)


# ---------------------------------------------------------------------------
# The defect: access=read does not survive the door -> lane -> write-scope chain
# ---------------------------------------------------------------------------

class TestReadAccessPathLosesItsRestrictionAtTheDoor:
    def test_read_access_path_excluded_from_door_forwarded_write_scope(self, tmp_path: Path) -> None:
        """A DispatchPath declared access=read must not end up write-scoped.

        RED on main: dispatch_cli.py:1868 builds dispatch_paths as bare
        ``str(dp.path)`` strings, so the captured value the door hands the
        lane carries no access information at all. Fed into
        resolve_dispatch_write_scope, a bare string defaults to READ_WRITE
        (worker_permissions._parse_dispatch_path_entry), so the read-only
        path incorrectly lands in the resulting write scope and this
        assertion fails.
        """
        read_path = "scripts/lib/secret_module.py"
        dispatch_paths = (
            DispatchPath(PurePosixPath(read_path), access=PathAccess.READ),
        )

        captured = _capture_door_forwarded_dispatch_paths(tmp_path, dispatch_paths)
        write_scope = resolve_dispatch_write_scope(captured)

        assert not _is_in_write_scope(read_path, write_scope), (
            f"access=read path {read_path!r} must not be write-scoped, but the door-forwarded "
            f"dispatch_paths {captured!r} resolved to write_scope {write_scope!r} which includes it "
            "(OI-1271: dispatch_cli.py:1868 drops DispatchPath.access before handing dispatch_paths "
            "to TmuxInteractiveDispatch.dispatch)"
        )

    def test_mixed_access_paths_write_scope_reflects_only_write_granting_paths(self, tmp_path: Path) -> None:
        """A dispatch declaring BOTH a read and a read_write path must only write-scope the latter.

        RED on main for the same reason as above — pulls the full chain
        (door -> lane kwargs -> resolve_dispatch_write_scope) through a
        realistic mixed-access spec instead of a single-path one, so the
        selectivity itself (not just "empty vs non-empty") is under test.
        """
        read_path = "scripts/lib/secret_module.py"
        write_path = "scripts/lib/build_module.py"
        dispatch_paths = (
            DispatchPath(PurePosixPath(read_path), access=PathAccess.READ),
            DispatchPath(PurePosixPath(write_path), access=PathAccess.READ_WRITE),
        )

        captured = _capture_door_forwarded_dispatch_paths(tmp_path, dispatch_paths)
        write_scope = resolve_dispatch_write_scope(captured)

        assert _is_in_write_scope(write_path, write_scope), (
            f"access=read_write path {write_path!r} must remain write-scoped; got write_scope {write_scope!r}"
        )
        assert not _is_in_write_scope(read_path, write_scope), (
            f"access=read path {read_path!r} must not be write-scoped alongside a read_write sibling, but "
            f"door-forwarded dispatch_paths {captured!r} resolved to write_scope {write_scope!r} which includes it"
        )


# ---------------------------------------------------------------------------
# Regression net — read_write must keep working. GREEN on main, stays GREEN.
# ---------------------------------------------------------------------------

class TestReadWriteAccessPathRegressionNet:
    def test_read_write_access_path_included_in_door_forwarded_write_scope(self, tmp_path: Path) -> None:
        """A DispatchPath declared access=read_write must remain write-scoped end-to-end.

        GREEN on main today: a bare path string defaults to READ_WRITE in
        worker_permissions._parse_dispatch_path_entry, so this already
        passes by coincidence of the very default the defect exploits. It
        must keep passing once the fix threads real access through, so the
        fix is proven not to have narrowed a legitimately write-granted path.
        """
        write_path = "scripts/lib/build_module.py"
        dispatch_paths = (
            DispatchPath(PurePosixPath(write_path), access=PathAccess.READ_WRITE),
        )

        captured = _capture_door_forwarded_dispatch_paths(tmp_path, dispatch_paths)
        write_scope = resolve_dispatch_write_scope(captured)

        assert _is_in_write_scope(write_path, write_scope), (
            f"access=read_write path {write_path!r} must be write-scoped; got captured={captured!r}, "
            f"write_scope={write_scope!r}"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
