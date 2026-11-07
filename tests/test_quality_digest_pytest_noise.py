"""The quality digest loads receipts without pytest noise (ledger-is-falsifiable, PR 3)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for _p in (SCRIPTS, SCRIPTS / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("VNX_HOME", str(SCRIPTS.parent))
os.environ.setdefault("VNX_DATA_DIR", str(SCRIPTS.parent / ".vnx-data"))
os.environ.setdefault("VNX_STATE_DIR", str(SCRIPTS.parent / ".vnx-data/state"))

from build_t0_quality_digest import _load_recent_receipts  # noqa: E402

NOW = datetime.now(timezone.utc).timestamp()


def _write(state_dir: Path, *records) -> None:
    (state_dir / "t0_receipts.ndjson").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _receipt(dispatch_id: str, **extra) -> dict:
    rec = {"event_type": "task_complete", "dispatch_id": dispatch_id,
           "status": "success", "timestamp": NOW}
    rec.update(extra)
    return rec


def _ids(state_dir: Path) -> list:
    return [r["dispatch_id"] for r in _load_recent_receipts(state_dir, hours=1)]


def test_pytest_row_is_dropped(tmp_path):
    _write(tmp_path, _receipt("d-real"), _receipt("d-noise", source="pytest"))

    assert _ids(tmp_path) == ["d-real"]


def test_pytest_source_is_matched_case_insensitively(tmp_path):
    _write(tmp_path, _receipt("d-noise", source="  PYTEST"))

    assert _ids(tmp_path) == []


def test_other_noise_shapes_are_dropped(tmp_path):
    _write(
        tmp_path,
        _receipt("d-keep"),
        _receipt("d-temp", report_path="/tmp/x/report.md"),
        _receipt("d-mock", note="<MagicMock id='1'>"),
    )

    assert _ids(tmp_path) == ["d-keep"]


def test_foreign_project_dropped_when_project_is_known(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "proj-digest-noise")
    _write(tmp_path, _receipt("d-own", project_id="proj-digest-noise"),
           _receipt("d-unstamped"), _receipt("d-foreign", project_id="other"))

    assert _ids(tmp_path) == ["d-own", "d-unstamped"]


def test_stamped_rows_stay_without_a_project_id(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    _write(tmp_path, _receipt("d-stamped", project_id="whatever"))

    assert _ids(tmp_path) == ["d-stamped"]


def test_non_dict_and_malformed_lines_are_skipped(tmp_path):
    (tmp_path / "t0_receipts.ndjson").write_text(
        json.dumps(_receipt("d-keep")) + "\n[1]\nnot json\n", encoding="utf-8")

    assert _ids(tmp_path) == ["d-keep"]
