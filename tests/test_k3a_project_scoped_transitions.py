#!/usr/bin/env python3
"""ADR-049 K-3a — transition_dispatch and transition_dispatch_idempotent scope to a project.

Root cause under test: the dispatch state machine read, updated and re-read a
row on ``dispatch_id`` alone (``scripts/lib/runtime_state_machine.py``), while
``dispatches`` is keyed ``UNIQUE(dispatch_id, project_id)``. A transition that
named only the dispatch id therefore changed every project's row with that id
and stamped an event under whichever project the shell happened to name.

K-3a makes ``project_id`` an optional keyword on both transition functions. When
it is given every read, the UPDATE, the re-read and the emitted event are scoped
to that project; when it is not given the pre-existing dispatch_id-only
behaviour stays for the callers that were not migrated here.

Tests run against the REAL coordination schema in ``tmp_path`` (the project's
own ``init_schema`` + migration helpers — nothing here is mocked; the only
"patch" is the DB path).
"""

from __future__ import annotations

import importlib.util as _ilu
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import coordination_db
import project_id_migration
from runtime_coordination import get_connection, init_schema
from runtime_state_machine import (
    transition_dispatch,
    transition_dispatch_idempotent,
)

# apply_0017 is the project's own migration that replaces the pre-ADR-007
# single-column UNIQUE(dispatch_id) with UNIQUE(dispatch_id, project_id). Without
# it a real store cannot hold the two-project fixture this file is about.
_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k3a", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)

PROJECT_A = "proj-alpha"
PROJECT_B = "proj-beta"
SHARED_DISPATCH = "d-shared"


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """A real coordination store at the production shape (project_id + composite UNIQUE)."""
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd)
    db_path = coordination_db.db_path_from_state_dir(sd)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id="vnx-dev")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        _mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return sd


def _insert_dispatch(
    conn: sqlite3.Connection,
    *,
    dispatch_id: str,
    project_id: str,
    state: str = "queued",
    attempt_count: int = 0,
) -> None:
    """Insert a row directly: register_dispatch refuses one dispatch_id across projects."""
    conn.execute(
        """
        INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json)
        VALUES (?, ?, ?, ?, '{}')
        """,
        (dispatch_id, project_id, state, attempt_count),
    )


def _row(conn: sqlite3.Connection, dispatch_id: str, project_id: str):
    return conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
        (dispatch_id, project_id),
    ).fetchone()


def _events(conn: sqlite3.Connection, dispatch_id: str):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT project_id, event_type, to_state FROM coordination_events "
            "WHERE entity_id = ? ORDER BY id",
            (dispatch_id,),
        ).fetchall()
    ]


def _two_project_rows(conn: sqlite3.Connection) -> None:
    _insert_dispatch(conn, dispatch_id=SHARED_DISPATCH, project_id=PROJECT_A, attempt_count=1)
    _insert_dispatch(conn, dispatch_id=SHARED_DISPATCH, project_id=PROJECT_B, attempt_count=7)
    conn.commit()


# ---------------------------------------------------------------------------
# 1. transition_dispatch scoped to one project
# ---------------------------------------------------------------------------

def test_scoped_transition_changes_only_the_own_project_row(state_dir: Path) -> None:
    with get_connection(state_dir) as conn:
        _two_project_rows(conn)

        result = transition_dispatch(
            conn,
            dispatch_id=SHARED_DISPATCH,
            to_state="claimed",
            actor="test",
            project_id=PROJECT_A,
        )
        conn.commit()

        assert result["project_id"] == PROJECT_A
        assert result["state"] == "claimed"
        assert _row(conn, SHARED_DISPATCH, PROJECT_A)["state"] == "claimed"
        assert _row(conn, SHARED_DISPATCH, PROJECT_B)["state"] == "queued"
        assert _events(conn, SHARED_DISPATCH) == [
            (PROJECT_A, "dispatch_claimed", "claimed"),
        ]


# ---------------------------------------------------------------------------
# 2. transition_dispatch_idempotent scoped to one project (incl. attempt_count)
# ---------------------------------------------------------------------------

def test_scoped_idempotent_transition_changes_only_the_own_project_row(state_dir: Path) -> None:
    with get_connection(state_dir) as conn:
        _two_project_rows(conn)

        result = transition_dispatch_idempotent(
            conn,
            dispatch_id=SHARED_DISPATCH,
            to_state="claimed",
            actor="test",
            project_id=PROJECT_A,
        )
        conn.commit()

        assert result["project_id"] == PROJECT_A
        assert result["state"] == "claimed"
        # The returned row is the own row — attempt_count proves it is not project B's.
        assert result["attempt_count"] == 1
        own = _row(conn, SHARED_DISPATCH, PROJECT_A)
        assert own["state"] == "claimed"
        assert own["attempt_count"] == 1
        other = _row(conn, SHARED_DISPATCH, PROJECT_B)
        assert other["state"] == "queued"
        assert other["attempt_count"] == 7
        assert _events(conn, SHARED_DISPATCH) == [
            (PROJECT_A, "dispatch_claimed", "claimed"),
        ]


def test_scoped_idempotent_noop_event_carries_the_project(state_dir: Path) -> None:
    """Already at the target state: the no-op event is scoped too, and nothing changes."""
    with get_connection(state_dir) as conn:
        _two_project_rows(conn)

        result = transition_dispatch_idempotent(
            conn,
            dispatch_id=SHARED_DISPATCH,
            to_state="queued",
            actor="test",
            project_id=PROJECT_A,
        )
        conn.commit()

        assert result["project_id"] == PROJECT_A
        assert result["state"] == "queued"
        assert _row(conn, SHARED_DISPATCH, PROJECT_B)["state"] == "queued"
        assert _events(conn, SHARED_DISPATCH) == [
            (PROJECT_A, "dispatch_noop", "queued"),
        ]


# ---------------------------------------------------------------------------
# 3. A project that has no row with that dispatch_id is not found, and nothing moves
# ---------------------------------------------------------------------------

def test_unknown_project_is_the_not_found_error_and_changes_nothing(state_dir: Path) -> None:
    with get_connection(state_dir) as conn:
        _insert_dispatch(conn, dispatch_id=SHARED_DISPATCH, project_id=PROJECT_A)
        conn.commit()

        with pytest.raises(KeyError):
            transition_dispatch(
                conn,
                dispatch_id=SHARED_DISPATCH,
                to_state="claimed",
                actor="test",
                project_id=PROJECT_B,
            )
        with pytest.raises(KeyError):
            transition_dispatch_idempotent(
                conn,
                dispatch_id=SHARED_DISPATCH,
                to_state="claimed",
                actor="test",
                project_id=PROJECT_B,
            )

        assert _row(conn, SHARED_DISPATCH, PROJECT_A)["state"] == "queued"
        assert _row(conn, SHARED_DISPATCH, PROJECT_B) is None
        assert _events(conn, SHARED_DISPATCH) == []


# ---------------------------------------------------------------------------
# 4. Without project_id the existing dispatch_id-only behaviour stays
# ---------------------------------------------------------------------------

def test_without_project_id_keeps_the_existing_behaviour(state_dir: Path) -> None:
    with get_connection(state_dir) as conn:
        _insert_dispatch(conn, dispatch_id=SHARED_DISPATCH, project_id=PROJECT_A)
        conn.commit()

        result = transition_dispatch(
            conn,
            dispatch_id=SHARED_DISPATCH,
            to_state="claimed",
            actor="test",
        )
        conn.commit()
        assert result["state"] == "claimed"
        assert result["project_id"] == PROJECT_A

        result_idem = transition_dispatch_idempotent(
            conn,
            dispatch_id=SHARED_DISPATCH,
            to_state="delivering",
            actor="test",
        )
        conn.commit()
        assert result_idem["state"] == "delivering"

        assert _row(conn, SHARED_DISPATCH, PROJECT_A)["state"] == "delivering"
        assert [to_state for _, _, to_state in _events(conn, SHARED_DISPATCH)] == [
            "claimed",
            "delivering",
        ]


# ---------------------------------------------------------------------------
# 5. A project_id that is not a non-empty string is refused, never "no project"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_project_id", [None, "", "   ", 0, 123, b"proj-alpha", ["proj-alpha"]])
def test_invalid_project_id_is_refused(state_dir: Path, bad_project_id) -> None:
    with get_connection(state_dir) as conn:
        _insert_dispatch(conn, dispatch_id=SHARED_DISPATCH, project_id=PROJECT_A)
        conn.commit()

        with pytest.raises(ValueError):
            transition_dispatch(
                conn,
                dispatch_id=SHARED_DISPATCH,
                to_state="claimed",
                actor="test",
                project_id=bad_project_id,
            )
        with pytest.raises(ValueError):
            transition_dispatch_idempotent(
                conn,
                dispatch_id=SHARED_DISPATCH,
                to_state="claimed",
                actor="test",
                project_id=bad_project_id,
            )

        assert _row(conn, SHARED_DISPATCH, PROJECT_A)["state"] == "queued"
        assert _events(conn, SHARED_DISPATCH) == []


# ---------------------------------------------------------------------------
# 6. A scoped UPDATE that matches no row is the not-found error, not success
# ---------------------------------------------------------------------------

def test_scoped_update_that_touches_no_row_is_the_not_found_error(state_dir: Path) -> None:
    """The row is visible to the scoped read, then a second writer removes it
    before the UPDATE runs (done in the trace hook — the code under test is
    untouched). The UPDATE then touches 0 rows and must raise, not succeed."""
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    with get_connection(state_dir) as conn:
        _insert_dispatch(conn, dispatch_id=SHARED_DISPATCH, project_id=PROJECT_A)
        conn.commit()

        def _delete_before_update(statement: str) -> None:
            if statement.lstrip().upper().startswith("UPDATE DISPATCHES"):
                other = sqlite3.connect(str(db_path))
                try:
                    other.execute(
                        "DELETE FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
                        (SHARED_DISPATCH, PROJECT_A),
                    )
                    other.commit()
                finally:
                    other.close()

        conn.set_trace_callback(_delete_before_update)
        try:
            with pytest.raises(KeyError):
                transition_dispatch(
                    conn,
                    dispatch_id=SHARED_DISPATCH,
                    to_state="claimed",
                    actor="test",
                    project_id=PROJECT_A,
                )
        finally:
            conn.set_trace_callback(None)

        assert _row(conn, SHARED_DISPATCH, PROJECT_A) is None
        assert _events(conn, SHARED_DISPATCH) == []
