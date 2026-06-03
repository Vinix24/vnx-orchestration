"""test_staging_enforcement.py — Parameterized tests for staging→pending gate enforcement.

Covers the four required cases from dispatch 20260603-140019-pending-governance-enforcement:
  (i)   --from-staging-id with a valid pending dispatch.json present → pass
  (ii)  --allow-unstaged + --reason → pass
  (iii) Neither arg provided → exit 1, stderr contains 'staging-pending-flow violated'
  (iv)  --from-staging-id pointing to non-existent dispatch → exit 1, same message
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from staging_validator import validate_staging_path  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pending_dispatch(tmp_path: Path, dispatch_id: str) -> None:
    """Create a canonical pending dispatch directory with dispatch.json."""
    d = tmp_path / "dispatches" / "pending" / dispatch_id
    d.mkdir(parents=True)
    (d / "dispatch.json").write_text(
        f'{{"dispatch_id": "{dispatch_id}", "terminal_id": "T1"}}',
        encoding="utf-8",
    )


def _make_staging_dispatch(tmp_path: Path, dispatch_id: str) -> None:
    """Create a staging dispatch as a .md file (convention used in this repo)."""
    staging = tmp_path / "dispatches" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / f"{dispatch_id}.md").write_text(
        f"# Dispatch {dispatch_id}\n\nInstruction body here.",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestStagingEnforcement:

    def test_accepts_from_staging_id_with_valid_pending_dispatch(self, tmp_path: Path) -> None:
        """(i) --from-staging-id pointing to an existing pending dispatch.json passes."""
        _make_pending_dispatch(tmp_path, "20260601-test-dispatch")
        # Must not raise or exit
        validate_staging_path(
            "20260601-test-dispatch",
            False,
            None,
            data_dir=tmp_path,
        )

    def test_accepts_from_staging_id_in_staging_dir(self, tmp_path: Path) -> None:
        """(i-b) --from-staging-id pointing to an existing staging .md file passes."""
        _make_staging_dispatch(tmp_path, "20260601-staging-only")
        validate_staging_path(
            "20260601-staging-only",
            False,
            None,
            data_dir=tmp_path,
        )

    def test_accepts_allow_unstaged_with_reason(self, tmp_path: Path) -> None:
        """(ii) --allow-unstaged with a non-empty reason bypasses file check."""
        validate_staging_path(
            None,
            True,
            "emergency hotfix: CI speed test, no dispatch file needed",
            data_dir=tmp_path,
        )

    def test_rejects_without_either_arg(self, tmp_path: Path, capsys) -> None:
        """(iii) Neither --from-staging-id nor --allow-unstaged: exits 1 with clear message."""
        with pytest.raises(SystemExit) as exc_info:
            validate_staging_path(None, False, None, data_dir=tmp_path)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "staging-pending-flow violated" in captured.err

    def test_rejects_from_staging_id_pointing_to_nonexistent_dispatch(
        self, tmp_path: Path, capsys
    ) -> None:
        """(iv) --from-staging-id with a non-existent ID: exits 1 with 'staging-pending-flow violated'."""
        with pytest.raises(SystemExit) as exc_info:
            validate_staging_path(
                "20260601-does-not-exist",
                False,
                None,
                data_dir=tmp_path,
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "staging-pending-flow violated" in captured.err

    def test_rejects_allow_unstaged_without_reason(self, tmp_path: Path, capsys) -> None:
        """--allow-unstaged without --reason exits 1 (audit trail requires reason)."""
        with pytest.raises(SystemExit) as exc_info:
            validate_staging_path(None, True, None, data_dir=tmp_path)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "--allow-unstaged requires" in captured.err

    def test_rejects_allow_unstaged_with_empty_reason(self, tmp_path: Path, capsys) -> None:
        """--allow-unstaged with blank reason also exits 1."""
        with pytest.raises(SystemExit) as exc_info:
            validate_staging_path(None, True, "   ", data_dir=tmp_path)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "--allow-unstaged requires" in captured.err


# ---------------------------------------------------------------------------
# Wiring checks — validate that both CLI entry-points declare the new args
# ---------------------------------------------------------------------------

class TestArgWiring:

    def test_subprocess_dispatch_declares_staging_args(self) -> None:
        """subprocess_dispatch __main__ parser must accept --from-staging-id etc."""
        import argparse
        import importlib.util
        import types

        spec = importlib.util.spec_from_file_location(
            "subprocess_dispatch",
            Path(__file__).resolve().parents[2]
            / "scripts" / "lib" / "subprocess_dispatch.py",
        )
        # We only need to verify the argument declarations, not execute the module.
        # Parse the source for the argparse add_argument calls.
        src = (
            Path(__file__).resolve().parents[2]
            / "scripts" / "lib" / "subprocess_dispatch.py"
        ).read_text(encoding="utf-8")
        assert "--from-staging-id" in src
        assert "--allow-unstaged" in src
        assert "--reason" in src
        assert "validate_staging_path" in src

    def test_tmux_interactive_dispatch_declares_staging_args(self) -> None:
        """tmux_interactive_dispatch main() parser must accept --from-staging-id etc."""
        src = (
            Path(__file__).resolve().parents[2]
            / "scripts" / "lib" / "tmux_interactive_dispatch.py"
        ).read_text(encoding="utf-8")
        assert "--from-staging-id" in src
        assert "--allow-unstaged" in src
        assert "--reason" in src
        assert "validate_staging_path" in src
