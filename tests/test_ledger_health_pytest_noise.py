"""ledger_health reads the ledger side without pytest noise (ledger-is-falsifiable, PR 3).

A registry id whose only receipt is a pytest row is not covered: it must sit in
``gaps`` (``missing_receipt_dispatch_ids``). The registry side stays unfiltered.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for _p in (SCRIPTS, SCRIPTS / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ledger_health as lh  # noqa: E402

PROJECT = "proj-pytest-noise"


@pytest.fixture(autouse=True)
def _project_id(monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def _write(path: Path, *records) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _register(dispatch_id: str) -> dict:
    return {"timestamp": "2026-11-07T10:00:00Z", "event": "dispatch_created", "dispatch_id": dispatch_id}


def _receipt(dispatch_id: str, **extra) -> dict:
    rec = {"timestamp": "2026-11-07T10:05:00Z", "event_type": "task_complete",
           "dispatch_id": dispatch_id, "status": "success"}
    rec.update(extra)
    return rec


def test_registry_id_with_only_a_pytest_row_is_a_gap(state_dir):
    _write(state_dir / lh.REGISTER_NAME, _register("d-real"), _register("d-noise"))
    _write(state_dir / lh.LEDGER_NAME, _receipt("d-real"), _receipt("d-noise", source="pytest"))

    result = lh.check_receipt_coverage(state_dir)

    assert result["status"] == lh.STATUS_FINDING
    assert result["missing_receipt_dispatch_ids"] == ["d-noise"]
    assert result["receipted_dispatch_count"] == 1


def test_pytest_source_is_matched_case_insensitively(state_dir):
    _write(state_dir / lh.REGISTER_NAME, _register("d-noise"))
    _write(state_dir / lh.LEDGER_NAME, _receipt("d-noise", source=" PyTest "))

    assert lh.check_receipt_coverage(state_dir)["missing_receipt_dispatch_ids"] == ["d-noise"]


def test_real_receipt_next_to_a_pytest_row_still_covers(state_dir):
    _write(state_dir / lh.REGISTER_NAME, _register("d-both"))
    _write(state_dir / lh.LEDGER_NAME, _receipt("d-both", source="pytest"), _receipt("d-both"))

    result = lh.check_receipt_coverage(state_dir)

    assert result["status"] == lh.STATUS_OK
    assert result["missing_receipt_dispatch_ids"] == []


@pytest.mark.parametrize("source", [None, "", "dispatch", "pytest-like", 7])
def test_other_source_values_are_not_noise(state_dir, source):
    _write(state_dir / lh.REGISTER_NAME, _register("d-1"))
    _write(state_dir / lh.LEDGER_NAME, _receipt("d-1", source=source))

    assert lh.check_receipt_coverage(state_dir)["status"] == lh.STATUS_OK


def test_registry_side_stays_unfiltered(state_dir):
    _write(state_dir / lh.REGISTER_NAME, _register("d-reg"), {**_register("d-reg-2"), "source": "pytest"})

    ids, lines, errors = lh._read_unique_dispatch_ids(state_dir / lh.REGISTER_NAME)

    assert ids == {"d-reg", "d-reg-2"}
    assert (lines, errors) == (2, 0)


def test_reader_without_project_id_does_not_filter(state_dir):
    _write(state_dir / lh.LEDGER_NAME, _receipt("d-noise", source="pytest"))

    ids, _, _ = lh._read_unique_dispatch_ids(state_dir / lh.LEDGER_NAME, include_cmd_id=True)

    assert ids == {"d-noise"}


def test_reader_with_project_id_drops_noise_and_foreign_rows(state_dir):
    _write(
        state_dir / lh.LEDGER_NAME,
        _receipt("d-keep"),
        _receipt("d-pytest", source="pytest"),
        _receipt("d-foreign", project_id="another-project"),
        _receipt("d-mock", note="<MagicMock id='1'>"),
    )

    ids, lines, errors = lh._read_unique_dispatch_ids(
        state_dir / lh.LEDGER_NAME, include_cmd_id=True, project_id=PROJECT)

    assert ids == {"d-keep"}
    assert (lines, errors) == (4, 0)


def test_malformed_line_still_counts_as_parse_error(state_dir):
    (state_dir / lh.LEDGER_NAME).write_text(
        json.dumps(_receipt("d-1", source="pytest")) + "\nnot json\n[1]\n", encoding="utf-8")

    ids, lines, errors = lh._read_unique_dispatch_ids(
        state_dir / lh.LEDGER_NAME, project_id=PROJECT)

    assert ids == set()
    assert (lines, errors) == (3, 2)
