"""ADR-056 zelfst PR-6a: cohort terminal membership in the lifecycle count.

Dispatch-ID: dlv-09886da9e18e

A delivery is in the cohort when its ``dlv-`` row is terminal and was created at
or after the period start (D9). ``completed`` is delivered; ``withdrawn``,
``expired`` and ``dead_letter`` are ``not_delivered`` with that reason; a
``completed`` row with a ``pr_ref`` and no ``pr_merged`` receipt is ``unknown``
(``receipt_missing``). Stores are real (``runtime_coordination.db`` and the
ledger under ``tmp_path``); the provenance repo is a throwaway git repository.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import lifecycle_count  # noqa: E402
import project_id_migration  # noqa: E402

PROJECT = "vnx-dev"
PERIOD = "2026-10-30T00:00:00Z"
INSIDE, BEFORE = "2026-11-02T10:00:00.000Z", "2026-10-01T10:00:00.000Z"
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)

_spec_0017 = _ilu.spec_from_file_location(
    "apply_0017_pr6a", SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py")
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "vnx-data" / "state"
    sd.mkdir(parents=True)
    coordination_db.init_schema(sd)
    db_path = coordination_db.db_path_from_state_dir(sd)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        _mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    (sd / "t0_receipts.ndjson").write_text("", encoding="utf-8")
    return sd


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    for args in (("init",), ("config", "user.email", "m@example.invalid"),
                 ("config", "user.name", "M")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "one"], check=True, capture_output=True)
    return path


def _row(state_dir: Path, dispatch_id: str, state: str, *, created_at: str = INSIDE,
         pr_ref=None, project: str = PROJECT) -> None:
    with coordination_db.get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, pr_ref, "
            "created_at, metadata_json) VALUES (?, ?, ?, 1, ?, ?, '{}')",
            (dispatch_id, project, state, pr_ref, created_at))
        conn.commit()


def _receipt(state_dir: Path, **line) -> None:
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _count(state_dir: Path, repo: Path, **kwargs) -> dict:
    return lifecycle_count.compute_lifecycle_count(
        state_dir, project_id=PROJECT, repo=repo, now=NOW, **kwargs)


def _by_id(count: dict) -> dict:
    return {m["dispatch_id"]: m for m in count["membership"]}


def test_withdrawn_not_delivered(state_dir, repo):
    _row(state_dir, "dlv-aaaa00000001", "completed")
    _row(state_dir, "dlv-aaaa00000002", "withdrawn")

    count = _count(state_dir, repo)

    assert count["status"] == "measured", count
    members = {m["dispatch_id"]: m for m in count.get("membership") or []}
    withdrawn = members.get("dlv-aaaa00000002")
    assert getattr(withdrawn, "status", None) == "not_delivered"
    assert withdrawn.reason == "withdrawn"
    assert getattr(members.get("dlv-aaaa00000001"), "status", None) == "delivered"


@pytest.mark.parametrize("state", ["withdrawn", "expired", "dead_letter"])
def test_not_delivered_states_carry_their_reason(state_dir, repo, state):
    _row(state_dir, "dlv-bbbb00000001", state)

    member = _by_id(_count(state_dir, repo))["dlv-bbbb00000001"]

    assert (member["status"], member["reason"]) == ("not_delivered", state)


def test_completed_with_pr_ref_and_no_merge_receipt_is_unknown(state_dir, repo):
    _row(state_dir, "dlv-cccc00000001", "completed", pr_ref="#12")

    member = _by_id(_count(state_dir, repo))["dlv-cccc00000001"]

    assert (member["status"], member["reason"]) == ("unknown", "receipt_missing")


def test_completed_with_pr_ref_and_merge_receipt_is_delivered(state_dir, repo):
    _row(state_dir, "dlv-dddd00000001", "completed", pr_ref="#13")
    _receipt(state_dir, event_type="pr_merged", dispatch_id="dlv-dddd00000001",
             conclusion="merged", project_id=PROJECT)

    assert _by_id(_count(state_dir, repo))["dlv-dddd00000001"]["status"] == "delivered"


@pytest.mark.parametrize("receipt", [
    {"event_type": "pr_merged", "dispatch_id": "dlv-eeee00000001"},  # conclusion missing
    {"event_type": "pr_merged", "dispatch_id": "dlv-eeee00000001", "conclusion": None},
    {"event_type": "pr_merged", "dispatch_id": "dlv-eeee00000001", "conclusion": "refused"},
    {"event_type": "pr_merge_refused", "dispatch_id": "dlv-eeee00000001", "conclusion": "merged"},
    {"event_type": "pr_merged", "dispatch_id": "dlv-eeee00000001", "conclusion": "merged",
     "project_id": "other-project"},
    {"event_type": "pr_merged", "dispatch_id": "dlv-eeee00000002", "conclusion": "merged"},
])
def test_a_receipt_that_is_not_a_merge_of_this_delivery_stays_unknown(state_dir, repo, receipt):
    _row(state_dir, "dlv-eeee00000001", "completed", pr_ref="#14")
    _receipt(state_dir, **receipt)

    member = _by_id(_count(state_dir, repo))["dlv-eeee00000001"]

    assert (member["status"], member["reason"]) == ("unknown", "receipt_missing")


@pytest.mark.parametrize("pr_ref", [None, "", "   "])
def test_completed_without_pr_ref_is_delivered(state_dir, repo, pr_ref):
    _row(state_dir, "dlv-ffff00000001", "completed", pr_ref=pr_ref)

    assert _by_id(_count(state_dir, repo))["dlv-ffff00000001"]["status"] == "delivered"


def test_created_before_the_period_is_out_of_the_cohort(state_dir, repo):
    _row(state_dir, "dlv-1111a0000001", "completed", created_at=BEFORE)
    _row(state_dir, "dlv-1111a0000002", "withdrawn", created_at=BEFORE)
    _row(state_dir, "dlv-1111a0000003", "completed", created_at=INSIDE)
    _row(state_dir, "dlv-1111a0000004", "completed", created_at="2026-10-30T00:00:00.000Z")

    count = _count(state_dir, repo, period_start=PERIOD)
    members = _by_id(count)

    assert members["dlv-1111a0000001"]["status"] == "out_of_cohort"
    assert members["dlv-1111a0000001"]["reason"] == "created_before_period"
    assert members["dlv-1111a0000002"]["status"] == "out_of_cohort"  # the period wins over the state
    assert members["dlv-1111a0000003"]["status"] == "delivered"
    assert members["dlv-1111a0000004"]["status"] == "delivered"  # the start is inclusive
    assert count["period_start"] == PERIOD
    assert count["membership_summary"] == {"delivered": 2, "not_delivered": 0, "unknown": 0,
                                           "out_of_cohort": 2}


@pytest.mark.parametrize("created_at", ["", "not-a-date"])
def test_unreadable_created_at_is_unknown_never_delivered(state_dir, repo, created_at):
    _row(state_dir, "dlv-2222a0000001", "completed", created_at=created_at)

    member = _by_id(_count(state_dir, repo, period_start=PERIOD))["dlv-2222a0000001"]

    assert (member["status"], member["reason"]) == ("unknown", "created_at_unreadable")


def test_without_a_period_nothing_is_excluded_by_age(state_dir, repo):
    _row(state_dir, "dlv-3333a0000001", "completed", created_at=BEFORE)

    count = _count(state_dir, repo)

    assert _by_id(count)["dlv-3333a0000001"]["status"] == "delivered"
    assert count["period_start"] is None


def test_only_terminal_dlv_rows_of_this_project_are_members(state_dir, repo):
    _row(state_dir, "dlv-4444a0000001", "running")
    _row(state_dir, "dlv-4444a0000002", "queued")
    _row(state_dir, "d-not-a-deliverable", "completed")
    _row(state_dir, "dlv-4444a0000003", "completed", project="other-project")
    _row(state_dir, "dlv-4444a0000004", "completed")

    assert list(_by_id(_count(state_dir, repo))) == ["dlv-4444a0000004"]


def test_an_unparseable_period_start_makes_the_count_unmeasured(state_dir, repo):
    _row(state_dir, "dlv-5555a0000001", "completed")

    count = _count(state_dir, repo, period_start="last tuesday")

    assert count["status"] == "unmeasured"
    assert count["membership"] is None
    assert "period start" in count["reason"]


def test_existing_fields_are_kept_and_membership_is_json_safe(state_dir, repo):
    _row(state_dir, "dlv-6666a0000001", "withdrawn")

    count = _count(state_dir, repo)

    assert {"status", "cohort", "counts", "findings", "sweep_heartbeat_age_seconds",
            "sweep_heartbeat_reason", "problems", "evaluated_at"} <= set(count)
    assert json.loads(json.dumps(count))["membership"][0]["status"] == "not_delivered"
    assert lifecycle_count.briefing_line(count, now=NOW).startswith("lifecycle [cohort all_v3]")


def test_unmeasured_count_has_no_membership_instead_of_an_empty_list(tmp_path):
    count = lifecycle_count.compute_lifecycle_count(tmp_path / "x" / "state", project_id="")

    assert count["status"] == "unmeasured"
    assert count["membership"] is None and count["membership_summary"] is None
