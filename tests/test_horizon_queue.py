"""tests/test_horizon_queue.py — `vnx horizon queue` (C1).

The readable "what is queued" overview that replaces FEATURE_PLAN.md /
PR_QUEUE.md: one row per deliverable with its state (the deliverables VIEW's
derived_status), the delivering PR from ``dispatches.pr_ref`` (the deliverable
or any ``-r<n>`` round), and its track's ``track_dependencies`` edges.

Verifies:
- ``--all`` shows a done deliverable, the default hides it
- the PR comes from ``pr_ref`` on the deliverable OR its round, ``-`` when none
- a track's dependency is named with its kind
- ``--json`` carries exactly the documented fields, untruncated title
- an empty store exits 0 with a clear line
- a present-but-unparseable pr_ref is refused (fail-closed, not "no PR")
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
for p in (_LIB, _SCRIPTS, _ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import planning_cli  # noqa: E402
import tracks as tracks_lib  # noqa: E402
from test_planning_deliverables import _build_db  # noqa: E402


PROJECT_ID = "test-proj"
TRACK_A = "track-a"
TRACK_B = "track-b"
LONG_TITLE = (
    "A deliberately long deliverable title that must be cut in the text form "
    "but kept whole in --json — UNIQUE-SUFFIX"
)


def _insert_dispatch(
    state_dir: Path,
    dispatch_id: str,
    *,
    state: str = "ready",
    track: str = TRACK_A,
    output_ref: str | None = None,
    pr_ref: str | None = None,
    title: str | None = None,
    updated_at: str = "2026-01-01T00:00:00.000Z",
) -> None:
    meta = json.dumps({"deliverable": True, "title": title}) if title is not None else "{}"
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    conn.execute(
        """
        INSERT INTO dispatches
            (dispatch_id, project_id, state, track, pr_ref, output_ref,
             output_kind, metadata_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dispatch_id, PROJECT_ID, state, track, pr_ref, output_ref,
            "pr" if output_ref else None, meta, updated_at,
        ),
    )
    conn.commit()
    conn.close()


def _queue(state_dir: Path, *extra: str) -> int:
    return planning_cli.main([
        "horizon", "queue",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
        *extra,
    ])


def _seed_tracks(state_dir: Path) -> None:
    tracks_lib.create_track(
        state_dir, TRACK_A, PROJECT_ID,
        title="Track A", goal_state="ship A", phase="queued", horizon="now",
    )
    tracks_lib.create_track(
        state_dir, TRACK_B, PROJECT_ID,
        title="Track B", goal_state="ship B", phase="queued", horizon="next",
    )
    tracks_lib.add_dependency(
        state_dir, TRACK_A, PROJECT_ID, TRACK_B, PROJECT_ID,
        kind="hard", derivation_source="manual",
    )


@pytest.fixture()
def queue_state(tmp_path: Path) -> Path:
    """One closed deliverable (#12), one ready without PR, one whose PR is on -r2."""
    state_dir = _build_db(tmp_path)
    _seed_tracks(state_dir)
    _insert_dispatch(
        state_dir, "dlv-000000000001", state="completed",
        output_ref="pr:dlv-000000000001", pr_ref="#12", title=LONG_TITLE,
        updated_at="2026-01-03T00:00:00.000Z",
    )
    _insert_dispatch(
        state_dir, "dlv-000000000002", state="ready",
        output_ref="pr:dlv-000000000002", title="Ready without PR",
        updated_at="2026-01-02T00:00:00.000Z",
    )
    _insert_dispatch(
        state_dir, "dlv-000000000003", state="ready",
        output_ref="pr:dlv-000000000003", title="PR on the round",
        updated_at="2026-01-01T00:00:00.000Z",
    )
    _insert_dispatch(
        state_dir, "dlv-000000000003-r2", state="completed",
        pr_ref="#34", updated_at="2026-01-04T00:00:00.000Z",
    )
    return state_dir


def test_queue_all_has_three_rows_with_prs(queue_state: Path, capsys):
    rc = _queue(queue_state, "--all", "--json")
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3
    assert [r["pr"] for r in rows] == [12, None, 34]
    assert [r["state"] for r in rows] == ["done", "ready", "ready"]
    assert [r["deliverable"] for r in rows] == [
        "dlv-000000000001", "dlv-000000000002", "dlv-000000000003",
    ]
    assert all(r["track"] == TRACK_A for r in rows)
    assert all(r["depends_on"] == [{"track": TRACK_B, "kind": "hard"}] for r in rows)


def test_queue_default_hides_done(queue_state: Path, capsys):
    rc = _queue(queue_state, "--json")
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert [r["deliverable"] for r in rows] == [
        "dlv-000000000002", "dlv-000000000003",
    ]


def test_queue_json_has_exact_fields_and_untruncated_title(queue_state: Path, capsys):
    rc = _queue(queue_state, "--all", "--json")
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert set(rows[0]) == {"track", "deliverable", "state", "pr", "depends_on", "title"}
    assert rows[0]["title"] == LONG_TITLE


def test_queue_text_form_cuts_title_and_names_dependency(queue_state: Path, capsys):
    rc = _queue(queue_state, "--all")
    assert rc == 0
    out = capsys.readouterr().out
    assert f"[{TRACK_A}]" in out
    assert "dlv-000000000001" in out
    assert "#12" in out and "#34" in out
    assert f"{TRACK_B}(hard)" in out
    assert "UNIQUE-SUFFIX" not in out  # the fixed-width column cut it


def test_queue_empty_store_exits_zero(tmp_path: Path, capsys):
    state_dir = _build_db(tmp_path)
    rc = _queue(state_dir)
    assert rc == 0
    assert "No deliverables queued" in capsys.readouterr().out


def test_queue_refuses_unparseable_pr_ref(tmp_path: Path, capsys):
    state_dir = _build_db(tmp_path)
    _seed_tracks(state_dir)
    _insert_dispatch(
        state_dir, "dlv-000000000009", state="ready",
        output_ref="pr:dlv-000000000009", pr_ref="not-a-pr", title="Bad ref",
    )
    rc = _queue(state_dir)
    assert rc == 2
    assert "unparseable pr_ref" in capsys.readouterr().err
