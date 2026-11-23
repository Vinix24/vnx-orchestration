"""stage_spec_bundle takes dispatch_paths from the deliverable metadata when the caller passes none (B10).

A deliverable row already carries its scope in ``metadata_json.dispatch_paths``
(``vnx deliverable add|set --dispatch-paths``), but staging only ever used the
``dispatch_paths`` argument its caller passed — so the door's blast-radius check
(ADR-096 B4b) saw ``scope_incomplete`` for a fire whose deliverable knew its paths.

The store is built from the real migrations, like ``tests/test_deliverable_dependencies_cli.py``.
Every staging call passes ``data_dir=tmp_path`` (with the store at ``tmp_path/state``),
so the door's live ``_resolve_data_dir`` is never touched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dispatch_bridge  # noqa: E402
import dispatch_cli  # noqa: E402

from fixtures.live_store_fixture import build_live_store  # noqa: E402

PID, OTHER = "test-proj", "other-proj"
DID = "20260102-101010-b10"
STYLE_KEY = "VNX_DOC_STYLE_FILE"
STYLE_HEADING = "## Writing style (VNX_DOC_STYLE_FILE)"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (STYLE_KEY, f"VNX_OVERRIDE_{STYLE_KEY[4:]}"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    build_live_store(d)
    return d


def _row(state_dir: Path, dispatch_id: str, metadata, pid: str = PID, raw: str | None = None) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, metadata_json) VALUES (?, ?, 'proposed', ?)",
            (dispatch_id, pid, raw if raw is not None else json.dumps(metadata)),
        )
        conn.commit()
    finally:
        conn.close()


def _stage(tmp_path: Path, *, dispatch_id: str = DID, paths: tuple = (), **over) -> Path:
    return dispatch_bridge.stage_spec_bundle(
        instruction_text="do the thing", dispatch_id=dispatch_id, role="backend-developer",
        target_slot="T1", project_id=PID, provider="claude", dispatch_paths=paths,
        data_dir=tmp_path, **over,
    )


def _payload(tmp_path: Path, *, dispatch_id: str = DID, **over) -> dict:
    return json.loads(_stage(tmp_path, dispatch_id=dispatch_id, **over).read_text(encoding="utf-8"))


def _caller_paths(*paths: str) -> tuple:
    return tuple({"path": p, "access": "read_write"} for p in paths)


def test_caller_paths_win_over_deliverable_metadata(tmp_path, state_dir):
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": ["scripts/a.py", "scripts/b.py"]})
    payload = _payload(tmp_path, paths=_caller_paths("docs/caller.md"))
    assert payload["dispatch_paths"] == [
        {"path": "docs/caller.md", "access": "read_write", "materialize_at_cwd": False}]
    assert payload["dispatch_paths_source"] == "caller"


def test_empty_caller_takes_the_deliverable_metadata_paths(tmp_path, state_dir):
    _row(state_dir, DID, {"title": "x", "deliverable": True, "dispatch_paths": ["scripts/a.py", "./docs/b.md"]})
    payload = _payload(tmp_path)
    assert payload["dispatch_paths"] == [
        {"path": "scripts/a.py", "access": "read_write", "materialize_at_cwd": False},
        {"path": "docs/b.md", "access": "read_write", "materialize_at_cwd": False},
    ]
    assert payload["dispatch_paths_source"] == "deliverable_metadata"


def test_row_without_paths_gives_none(tmp_path, state_dir):
    _row(state_dir, DID, {"title": "x", "deliverable": True})
    payload = _payload(tmp_path)
    assert payload["dispatch_paths"] == []
    assert payload["dispatch_paths_source"] == "none"


def test_row_that_is_not_a_deliverable_gives_none(tmp_path, state_dir):
    # A plain dispatch row may carry dispatch_paths metadata; only a DELIVERABLE's paths are used.
    _row(state_dir, DID, {"title": "x", "dispatch_paths": ["scripts/a.py"]})
    payload = _payload(tmp_path)
    assert payload["dispatch_paths"] == []
    assert payload["dispatch_paths_source"] == "none"


def test_missing_row_gives_none(tmp_path, state_dir):
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": ["scripts/a.py"]})
    payload = _payload(tmp_path, dispatch_id="20260102-111111-other")
    assert payload["dispatch_paths"] == []
    assert payload["dispatch_paths_source"] == "none"


def test_missing_store_gives_none(tmp_path):
    # No state/runtime_coordination.db at all: staging still works, without paths.
    payload = _payload(tmp_path)
    assert payload["dispatch_paths"] == []
    assert payload["dispatch_paths_source"] == "none"


def test_row_of_another_project_gives_none(tmp_path, state_dir):
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": ["scripts/a.py"]}, pid=OTHER)
    payload = _payload(tmp_path)
    assert payload["dispatch_paths"] == []
    assert payload["dispatch_paths_source"] == "none"


@pytest.mark.parametrize("raw,reason", [
    ("{not json", "not valid JSON"),
    ("[]", "not a JSON object"),
    ("null", "not a JSON object"),
])
def test_malformed_metadata_json_gives_none_and_reports(tmp_path, state_dir, capsys, raw, reason):
    _row(state_dir, DID, None, raw=raw)
    payload = _payload(tmp_path)
    assert payload["dispatch_paths"] == []
    assert payload["dispatch_paths_source"] == "none"
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if DID in ln]
    assert len(lines) == 1
    assert reason in lines[0]


@pytest.mark.parametrize("raw_paths", [
    "scripts/a.py",
    {"path": "scripts/a.py"},
    ["scripts/a.py", 3],
    ["scripts/a.py", None],
    ["scripts/a.py", ""],
    ["scripts/a.py", "   "],
])
def test_malformed_path_list_gives_none_and_reports(tmp_path, state_dir, capsys, raw_paths):
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": raw_paths})
    payload = _payload(tmp_path)
    assert payload["dispatch_paths"] == []
    assert payload["dispatch_paths_source"] == "none"
    lines = [ln for ln in capsys.readouterr().err.splitlines() if DID in ln]
    assert len(lines) == 1
    assert "dispatch_paths" in lines[0]


@pytest.mark.parametrize("raw_paths", [[], None])
def test_empty_path_list_gives_none_without_a_warning(tmp_path, state_dir, capsys, raw_paths):
    # "no scope stated" is not malformed metadata: staging carries no paths and stays quiet.
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": raw_paths})
    payload = _payload(tmp_path)
    assert payload["dispatch_paths"] == []
    assert payload["dispatch_paths_source"] == "none"
    assert capsys.readouterr().err == ""


def test_round_id_does_not_inherit_the_base_deliverable_paths(tmp_path, state_dir):
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": ["scripts/a.py"]})
    payload = _payload(tmp_path, dispatch_id=f"{DID}-r2")
    assert payload["dispatch_paths"] == []
    assert payload["dispatch_paths_source"] == "none"


def test_round_id_uses_its_own_deliverable_row(tmp_path, state_dir):
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": ["scripts/base.py"]})
    _row(state_dir, f"{DID}-r2", {"deliverable": True, "dispatch_paths": ["scripts/round.py"]})
    payload = _payload(tmp_path, dispatch_id=f"{DID}-r2")
    assert [p["path"] for p in payload["dispatch_paths"]] == ["scripts/round.py"]
    assert payload["dispatch_paths_source"] == "deliverable_metadata"


def test_staged_spec_loads_through_dispatch_cli(tmp_path, state_dir):
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": ["scripts/a.py", "docs/b.md"]})
    spec_file = _stage(tmp_path)
    spec = dispatch_cli.load_spec(spec_file)
    assert [str(dp.path) for dp in spec.dispatch_paths] == ["scripts/a.py", "docs/b.md"]


def test_difficulty_file_count_sees_the_metadata_paths(tmp_path, state_dir):
    # _require_difficulty runs on the FINAL paths: an absent file_count defaults to their count.
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": ["scripts/a.py", "docs/b.md"]})
    payload = _payload(tmp_path, difficulty={"plan_complexity": "low"})
    assert payload["difficulty"]["file_count"] == 2


def test_docs_dispatch_detection_sees_the_metadata_paths(tmp_path, state_dir, monkeypatch):
    # is_docs_dispatch runs on the FINAL paths too: docs-only metadata paths make this a docs dispatch.
    style = tmp_path / "style.md"
    style.write_text("Write short sentences.\n", encoding="utf-8")
    monkeypatch.setenv(STYLE_KEY, str(style))
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": ["docs/a.md"]})
    spec_file = _stage(tmp_path)
    assert STYLE_HEADING in (spec_file.parent / "instruction.md").read_text(encoding="utf-8")


def test_resolve_dispatch_paths_names_its_source(state_dir, tmp_path):
    # The helper stage_spec_bundle calls in one statement (B10); the three sources.
    caller = ({"path": "scripts/caller.py"},)
    assert dispatch_bridge._resolve_dispatch_paths(caller, tmp_path, PID, DID) == (caller, "caller")
    _row(state_dir, DID, {"deliverable": True, "dispatch_paths": ["scripts/meta.py"]})
    resolved, source = dispatch_bridge._resolve_dispatch_paths((), tmp_path, PID, DID)
    assert source == "deliverable_metadata"
    assert resolved == (
        {"path": "scripts/meta.py", "access": "read_write", "materialize_at_cwd": False},)
    # EXACT id: a round id never inherits the base deliverable's paths.
    assert dispatch_bridge._resolve_dispatch_paths((), tmp_path, PID, f"{DID}-r2") == ((), "none")
