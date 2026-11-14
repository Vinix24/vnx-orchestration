#!/usr/bin/env python3
"""OI-2786: reattr verify finds a moved row on its stable identity. Every store lives under tmp_path."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))

import qi_transfer_plan as qtp  # noqa: E402
from test_pattern_reattribution import _SCHEMA_SQL, _db_in, _insert_success_pattern  # noqa: E402

_spec = importlib.util.spec_from_file_location("learning_misfile_report", REPO / "scripts" / "learning_misfile_report.py")
_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_report)
verify_plan = _report.verify_plan


def _sql(db: Path, sql: str, *args) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(sql, args)
    conn.commit()
    conn.close()


@pytest.fixture
def moved(tmp_path: Path):
    """A planned move of one row alpha -> beta, executed by hand: copied to beta, gone from alpha."""
    src, dest = _db_in(tmp_path, "alpha"), _db_in(tmp_path, "beta")
    rid = _insert_success_pattern(src, project_id="alpha", source_dispatch_ids=["d-1"], title="misrouted")
    conn = qtp.open_readonly(src)
    image = qtp.read_pre_image(conn, "success_patterns", rid)
    conn.close()
    move = {
        "table": "success_patterns", "source_id": rid, "source_db": str(src), "destination_db": str(dest),
        "project_id": "alpha", "destination_project_id": "beta", "pre_image": image,
        "content_key": qtp.content_key(image["row"]),
    }
    cols = [c for c in image["row"] if c not in ("id", "project_id")]
    conn = sqlite3.connect(str(dest))
    conn.execute(f"INSERT INTO success_patterns ({', '.join(cols)}, project_id) "
                 f"VALUES ({', '.join('?' for _ in cols)}, 'beta')", [image["row"][c] for c in cols])
    conn.commit()
    conn.close()
    _sql(src, "DELETE FROM success_patterns WHERE id = ?", rid)
    return {"move": move, "src": src, "dest": dest, "id": rid}


def _reasons(result):
    return [f["reason"] for f in result["failures"]]


def test_mutable_column_change_is_present_and_named(moved):
    # Red run: on origin/main the changed confidence_score gives missing_in_destination.
    _sql(moved["dest"], "UPDATE success_patterns SET confidence_score = 0.5 WHERE project_id = 'beta'")
    result = verify_plan([moved["move"]])
    assert result["ok"] is True and result["present"] == 1 and result["failures"] == []
    assert result["mutated_since_move"] == [{
        "table": "success_patterns", "source_id": moved["id"],
        "columns": {"confidence_score": {"old": 0.8, "new": 0.5}},
    }]


def test_unchanged_row_has_no_mutation(moved):
    result = verify_plan([moved["move"]])
    assert result["ok"] is True and result["mutated_since_move"] == []


def test_non_mutable_column_change_is_missing(moved):
    _sql(moved["dest"], "UPDATE success_patterns SET title = 'renamed' WHERE project_id = 'beta'")
    result = verify_plan([moved["move"]])
    assert result["ok"] is False and _reasons(result) == ["missing_in_destination"]
    assert result["mutated_since_move"] == []


def test_source_leftover_with_only_a_mutable_change_is_still_in_source(moved):
    _insert_success_pattern(moved["src"], project_id="alpha", source_dispatch_ids=["d-1"], title="misrouted")
    _sql(moved["src"], "UPDATE success_patterns SET confidence_score = 0.1, usage_count = 9")
    result = verify_plan([moved["move"]])
    assert result["ok"] is False and _reasons(result) == ["still_in_source"]


def test_two_destination_rows_with_one_stable_identity_are_duplicates(moved):
    conn = sqlite3.connect(str(moved["dest"]))
    conn.execute("INSERT INTO success_patterns (pattern_type, category, title, description, pattern_data, "
                 "confidence_score, usage_count, source_dispatch_ids, project_id) "
                 "VALUES ('code', 'architect', 'misrouted', 'desc', '{}', 0.2, 7, '[\"d-1\"]', 'beta')")
    conn.commit()
    conn.close()
    result = verify_plan([moved["move"]])
    assert result["ok"] is False and _reasons(result) == ["duplicate_in_destination"]


def test_unreadable_destination_stays_a_failure(moved):
    moved["move"]["destination_db"] = str(moved["dest"].parent / "absent.db")
    assert _reasons(verify_plan([moved["move"]])) == ["destination_unreadable"]


def test_content_key_is_unchanged_for_an_unchanged_row():
    row = {"id": 4, "project_id": "alpha", "title": "t", "confidence_score": 0.8, "usage_count": 3}
    # Pinned: sha256 over the canonical JSON of the row without id and project_id (unchanged by OI-2786).
    assert qtp.content_key(row) == "e0b89dd38578db1864ba351e24f0033e8941517e43f7f9603621af2031e4e20f"


def test_stable_key_ignores_mutable_columns_only():
    row = {"id": 4, "project_id": "alpha", "title": "t", "confidence_score": 0.8, "usage_count": 3}
    assert qtp.stable_key("success_patterns", row) == qtp.stable_key(
        "success_patterns", {**row, "confidence_score": 0.1, "usage_count": 99})
    assert qtp.stable_key("success_patterns", row) != qtp.stable_key("success_patterns", {**row, "title": "u"})
    assert qtp.stable_key("unknown_table", row) == qtp.content_key(row)


def test_mutable_columns_exist_in_the_schema():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA_SQL)
    assert set(qtp.MUTABLE_COLUMNS) == {"success_patterns", "antipatterns"}
    # content_hash, pattern_category and source_receipts come from migrations, not from the base DDL.
    migrated = {"content_hash", "pattern_category", "source_receipts"}
    for table, cols in qtp.MUTABLE_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert cols - migrated <= have, (table, cols - migrated - have)
        assert not cols & {"id", "project_id", "valid_until"}


def _variant(move, **changes):
    """A hand-made copy of a planned move whose pre-image row carries ``changes``."""
    row = {**move["pre_image"]["row"], **changes}
    return {**move, "source_id": move["source_id"] + 100, "pre_image": {**move["pre_image"], "row": row},
            "content_key": qtp.content_key(row)}


def test_moves_differing_only_in_a_mutable_column_are_ambiguous(moved):
    # Red run: on ad610769 this plan gave ok true, planned 2, present 2 from one destination row.
    twin = _variant(moved["move"], confidence_score=0.3)
    result = verify_plan([moved["move"], twin])
    assert result["ok"] is False and result["present"] == 0
    assert _reasons(result) == ["ambiguous_stable_identity"] * 2
    assert all(f["source_ids"] == [moved["id"], twin["source_id"]] for f in result["failures"])


def test_second_move_on_the_same_destination_row_is_reused(moved):
    other = _variant(moved["move"])
    del other["pre_image"]["row"]["title"]  # looser lookup: a different stable identity matching the same row
    result = verify_plan([moved["move"], other])
    assert result["ok"] is False and result["present"] == 1
    assert _reasons(result) == ["destination_row_reused"]
    failure = result["failures"][0]
    assert failure["source_id"] == other["source_id"] and failure["already_assigned_to"] == moved["id"]
    assert isinstance(failure["destination_row_id"], int)


def test_two_identities_with_two_destination_rows_are_both_present(moved):
    other = _variant(moved["move"], title="second")
    cols = [c for c in other["pre_image"]["row"] if c not in ("id", "project_id")]
    conn = sqlite3.connect(str(moved["dest"]))
    conn.execute(f"INSERT INTO success_patterns ({', '.join(cols)}, project_id) "
                 f"VALUES ({', '.join('?' for _ in cols)}, 'beta')", [other["pre_image"]["row"][c] for c in cols])
    conn.commit()
    conn.close()
    result = verify_plan([moved["move"], other])
    assert result["ok"] is True and result["present"] == 2 and result["failures"] == []


def _dotted(dest: Path) -> str:
    return f"{dest.parent}/./{dest.name}"


def _linked(dest: Path) -> str:
    link = dest.parent / "dest-link.db"
    link.symlink_to(dest)
    return str(link)


@pytest.mark.parametrize("spelling", [_dotted, _linked], ids=["dot-segment", "symlink"])
def test_equivalent_destination_path_does_not_hide_an_ambiguous_identity(moved, spelling):
    # Red run: on 1061b4a4 the raw path keyed the groups, so this plan gave ok true, present 2.
    twin = _variant(moved["move"], confidence_score=0.3)
    twin["destination_db"] = spelling(moved["dest"])
    result = verify_plan([moved["move"], twin])
    assert result["ok"] is False and result["present"] == 0
    assert _reasons(result) == ["ambiguous_stable_identity"] * 2


@pytest.mark.parametrize("spelling", [_dotted, _linked], ids=["dot-segment", "symlink"])
def test_equivalent_destination_path_does_not_hide_a_reused_row(moved, spelling):
    other = _variant(moved["move"])
    del other["pre_image"]["row"]["title"]
    other["destination_db"] = spelling(moved["dest"])
    result = verify_plan([moved["move"], other])
    assert result["ok"] is False and result["present"] == 1
    assert _reasons(result) == ["destination_row_reused"]
