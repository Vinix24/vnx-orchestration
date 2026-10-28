#!/usr/bin/env python3
"""T-B2 (terminal-binding-afbouwen): the receipt carries no terminal snapshot.

The `terminal_snapshot` field and the `VNX_ADAPTER_T0` condition that used to
annotate it are gone. A receipt's identity comes from the dispatch row; "what
is running" is `live_work` in t0_state.json. These tests pin both halves of the
removal:

- a completion receipt enriched with `VNX_ADAPTER_T0=subprocess` has no
  `terminal_snapshot` key (this is the red run: on the old code the field is
  written on every completion receipt);
- `append_receipt` no longer re-exports `is_headless_t0()` or
  `collect_terminal_snapshot`;
- the state side (no `terminals` block) is pinned in
  `tests/test_build_t0_state_no_tmux.py`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import append_receipt as ar


def _completion_receipt() -> dict:
    return {
        "timestamp": "1714000000",
        "event_type": "task_complete",
        "terminal": "T0",
    }


def _enrich(receipt: dict, state_dir: Path) -> dict:
    """Run the real _enrich_completion_receipt with the external reads stubbed."""
    with patch.object(ar, "ensure_env", return_value={
        "VNX_STATE_DIR": str(state_dir),
        "PROJECT_ROOT": str(state_dir),
    }), \
         patch.object(ar, "_build_git_provenance", return_value={"git_ref": "HEAD"}), \
         patch.object(ar, "_build_session_metadata", return_value={"session_id": "s1"}), \
         patch.object(ar, "enrich_receipt_provenance", return_value=None), \
         patch.object(ar, "validate_receipt_provenance", return_value=MagicMock(gaps=[])), \
         patch.object(ar, "get_changed_files", return_value=[]):
        return ar._enrich_completion_receipt(receipt, repo_root=state_dir)


@pytest.mark.parametrize("adapter", ["subprocess", None])
def test_completion_receipt_carries_no_terminal_snapshot(tmp_path: Path, adapter):
    """Neither the headless flag nor its absence reintroduces the field."""
    if adapter is None:
        env = {k: v for k, v in os.environ.items() if k != "VNX_ADAPTER_T0"}
        with patch.dict(os.environ, env, clear=True):
            enriched = _enrich(_completion_receipt(), tmp_path)
    else:
        with patch.dict(os.environ, {"VNX_ADAPTER_T0": adapter}):
            enriched = _enrich(_completion_receipt(), tmp_path)

    assert "terminal_snapshot" not in enriched


def test_append_receipt_no_longer_exports_is_headless_t0():
    assert not hasattr(ar, "is_headless_t0")
    assert "is_headless_t0" not in ar.__all__


def test_append_receipt_no_longer_exports_collect_terminal_snapshot():
    assert not hasattr(ar, "collect_terminal_snapshot")
    assert "collect_terminal_snapshot" not in ar.__all__
