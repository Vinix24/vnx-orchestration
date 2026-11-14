"""OI-2727: the review queue reader counts an unreadable review file (ADR-066)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dream"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import review_gate  # noqa: E402
from test_dream_cli import _insert_cycle, _make_db, _write_pending_review  # noqa: E402

CYCLE = "dream-20260529-120000-abcd1234"


def _write_bad(tmp_path: Path, content: str = "{not json") -> None:
    state_dir = tmp_path / ".vnx-data" / "state" / "dream"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "bad-pending-review.json").write_text(content, encoding="utf-8")


def _run_status(tmp_path: Path, capsys) -> tuple[int, str]:
    import vnx_cli.commands.dream as dream_mod

    db_path = _make_db(tmp_path)
    _insert_cycle(db_path, CYCLE, "vnx-dev", "2026-05-29T12:00:00+00:00")
    args = argparse.Namespace(project_id="vnx-dev", project_dir=str(tmp_path))
    with patch.object(dream_mod, "_resolve_paths", return_value=(tmp_path, db_path)), \
            patch.object(dream_mod, "_resolve_data_root", return_value=tmp_path / ".vnx-data"):
        rc = dream_mod._cmd_status(args)
    return rc, capsys.readouterr().out


def test_queue_returns_payload_and_unreadable_name(tmp_path):
    _write_pending_review(tmp_path, CYCLE, "vnx-dev")
    _write_bad(tmp_path)
    root = tmp_path / ".vnx-data"
    pending, unreadable = review_gate.pending_review_queue("vnx-dev", root)
    assert [p["cycle_id"] for p in pending] == [CYCLE]
    assert unreadable == ["bad-pending-review.json"]
    assert [p["cycle_id"] for p in review_gate.list_pending_reviews("vnx-dev", root)] == [CYCLE]


def test_non_object_json_is_unreadable(tmp_path):
    _write_bad(tmp_path, "[1, 2]")
    assert review_gate.pending_review_queue("vnx-dev", tmp_path / ".vnx-data") == (
        [], ["bad-pending-review.json"])


def test_no_state_dir(tmp_path):
    assert review_gate.pending_review_queue("vnx-dev", tmp_path / ".vnx-data") == ([], [])


def test_status_names_unreadable_file_when_only_broken(tmp_path, capsys):
    _write_bad(tmp_path)
    rc, out = _run_status(tmp_path, capsys)
    assert rc == 0
    assert "  Unreadable review files (1): bad-pending-review.json" in out
    assert "Pending reviews" not in out


def test_status_clean_queue_prints_no_unreadable_line(tmp_path, capsys):
    _write_pending_review(tmp_path, CYCLE, "vnx-dev")
    rc, out = _run_status(tmp_path, capsys)
    assert rc == 0
    assert "Pending reviews (1)" in out
    assert "Unreadable" not in out
