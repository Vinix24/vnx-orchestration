"""The open-outcomes list shows a fresh reading of the test evidence (poort-is-bewijs).

A1/A2: ten real Verification sections pin what the reader gives, including the
cases that must stay ``investigate``. A3-A5: ``open-outcomes --recount`` shows the
reading of the report today next to the stored one, writes nothing and leaves the
output without the flag as it was. ADR-007: a second project reuses the dispatch ids.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
for _p in (_SCRIPTS, _SCRIPTS / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import open_outcomes as oo
from envelope_govern_support import _verification_from_report
from receipt_verdict import compute_verdict
from verification_runs import label_color

FIXTURES = _REPO / "tests" / "fixtures" / "verification_reports"
PROJECT = "proj-a"
OTHER = "proj-b"
TS = "2026-09-29T10:00:00Z"
STALE = {"method": "unknown", "tests_run": None, "tests_passed": None, "tests_failed": None}

# (report id, (passed, failed) of the last counted run, what it pins)
FIXTURE_READINGS: List[Tuple[str, Tuple[Any, Any], str]] = [
    ("20260929-fsh-d1-verification-kop-ff1", (69, 0), "Red run / Green run"),
    ("20260929-fsh-d2-indexfouten", (108, 0), "Rode run / Groene run"),
    ("20260929-fsh-d7-gate-bundles-sonnet", (174, 0), "Red run on the old code"),
    ("20261003-ff-2030-ci-probe", (63, 0), "Green: under a red line"),
    ("20260929-fsh-d4a-lezers-receipt-outcome-ff1", (59, 12), "Red ( is not a label"),
    ("20260929-fsh-d4b2-geen-eindstatus-zonder-uitkomst", (210, 5), "Rood voor en na is no label"),
    ("20261003-pib-oi1943-afkeuring-is-bewijs", (25, 1), "a base-red failure in prose counts"),
    ("20261004-t0-map-rol-en-hook", (0, 2), "an intermediate failure in prose counts"),
    ("plan-tiebreak-t0-start-in-eigen-map-c023ec1f", (None, None), "no test run"),
    ("20260929-fsh-d6-health-headless", (80, 4), "a failing neighbour run counts"),
]


@pytest.mark.parametrize("report_id,expected,_pins", FIXTURE_READINGS,
                         ids=[f[0] for f in FIXTURE_READINGS])
def test_a1_fixture_reading(report_id: str, expected: Tuple[Any, Any], _pins: str) -> None:
    reading = _verification_from_report(FIXTURES / f"{report_id}.md")
    assert (reading["tests_passed"], reading["tests_failed"]) == expected


def test_a1_report_without_a_test_run_reads_unknown() -> None:
    reading = _verification_from_report(FIXTURES / "plan-tiebreak-t0-start-in-eigen-map-c023ec1f.md")
    assert reading["method"] == "unknown"


def test_a2_report_without_a_test_run_never_becomes_an_accept() -> None:
    reading = _verification_from_report(FIXTURES / "plan-tiebreak-t0-start-in-eigen-map-c023ec1f.md")
    verdict = compute_verdict({"status": "success", "verification": reading})
    assert verdict["decision"] == "investigate"


@pytest.mark.parametrize("label", ["Green:", "**Green**:", "**Green:**", "Groen:", "- Groen :"])
def test_green_colon_label_ends_a_red_reach(tmp_path: Path, label: str) -> None:
    report = tmp_path / "r.md"
    report.write_text(
        "## Verification\n**Red run** on abc: `pytest x` gave 1 failed, 29 passed.\n"
        f"{label} `pytest x` gave 30 passed.\n", encoding="utf-8")
    reading = _verification_from_report(report)
    assert (reading["tests_passed"], reading["tests_failed"]) == (30, 0)


@pytest.mark.parametrize("prose", ["Green light: 3 failed", "Greenfield: 3 failed", "Groen licht 3 failed",
                                   "Green 3 failed", "The Green: 3 failed"])
def test_green_word_without_colon_form_is_no_label(prose: str) -> None:
    assert label_color(prose) is None


@pytest.mark.parametrize("line", ["Red (old code): 3 failed", "Rood: 3 failed", "Rood vóór en na: 3 failed"])
def test_no_new_red_label(line: str) -> None:
    assert label_color(line) is None


def test_no_new_red_label_a_failing_run_after_prose_red_still_counts(tmp_path: Path) -> None:
    report = tmp_path / "r.md"
    report.write_text(
        "## Verification\nRood: `pytest x` gave 2 failed, 10 passed.\n", encoding="utf-8")
    assert _verification_from_report(report)["tests_failed"] == 2


# ---------------------------------------------------------------------------
# --recount
# ---------------------------------------------------------------------------

def _line(did: str, project: str, status: str, **kw: Any) -> Dict[str, Any]:
    return {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
            "status": status, "timestamp": TS, "project_id": project, **kw}


def _receipts(did: str, project: str, report: Path) -> List[Dict[str, Any]]:
    return [_line(did, project, "success"),
            _line(did, project, "done", report_file=str(report), verification=STALE)]


def _report(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"## Verification\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path: Path) -> Dict[str, Path]:
    """Three dispatches of PROJECT, and OTHER reusing the same ids with a failing report."""
    root = tmp_path / "vnx"
    state = root / "state"
    state.mkdir(parents=True)
    reports = root / "unified_reports"
    foreign = tmp_path / "other" / "unified_reports"
    clean = _report(reports / "d-clean.md", "`pytest t -q` gave 12 passed.")
    failing = _report(reports / "d-failing.md", "`pytest t -q` gave 9 passed, 2 failed.")
    receipts: List[Dict[str, Any]] = []
    receipts += _receipts("d-clean", PROJECT, clean)
    receipts += _receipts("d-failing", PROJECT, failing)
    receipts += _receipts("d-missing", PROJECT, reports / "d-missing.md")
    for did in ("d-clean", "d-failing", "d-missing"):
        receipts += _receipts(did, OTHER, _report(foreign / f"{did}.md", "`pytest t -q` gave 1 passed, 7 failed."))
    (state / oo.LEDGER_NAME).write_text(
        "".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
    return {"root": root, "state": state, "tmp": tmp_path}


def _cli(state: Path, *extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(state.parent.parent / "home"))
    env.pop("VNX_PROJECT_ID", None)
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "receipt_query.py"), "open-outcomes", "--json",
         "--state-dir", str(state), "--project-id", PROJECT, *extra],
        capture_output=True, text=True, env=env, check=False)


def _items(proc: subprocess.CompletedProcess) -> Dict[str, Dict[str, Any]]:
    assert proc.returncode == 0, proc.stderr
    return {i["dispatch_id"]: i for i in json.loads(proc.stdout)["items"]}


def test_a3_recount_gives_the_fresh_decision_per_dispatch(store: Dict[str, Path]) -> None:
    items = _items(_cli(store["state"], "--recount"))
    assert set(items) == {"d-clean", "d-failing", "d-missing"}
    assert items["d-clean"]["outcome"] == "investigate"
    assert items["d-clean"]["recount"]["fresh_decision"] == "accept"
    assert items["d-clean"]["recount"]["method"] == "pytest"
    assert items["d-clean"]["recount"]["tests_run"] == 12
    assert items["d-clean"]["recount"]["stored"]["method"] == "unknown"
    assert items["d-failing"]["recount"]["fresh_decision"] == "investigate"
    assert items["d-failing"]["recount"]["tests_failed"] == 2
    assert items["d-missing"]["recount"]["fresh_decision"] == "no_report"


def test_a3_second_project_with_colliding_ids_does_not_leak(store: Dict[str, Path]) -> None:
    proc = _cli(store["state"], "--recount")
    assert "proj-b" not in proc.stdout and "other" not in proc.stdout
    items = _items(proc)
    assert items["d-clean"]["recount"]["tests_failed"] == 0
    assert items["d-failing"]["recount"]["tests_failed"] == 2


def test_a3_summary_counts_per_pair(store: Dict[str, Path]) -> None:
    summary = json.loads(_cli(store["state"], "--recount").stdout)["recount_summary"]
    pairs = {(p["stored_decision"], p["fresh_decision"]): p["count"] for p in summary}
    assert pairs == {("investigate", "accept"): 1, ("investigate", "investigate"): 1,
                     ("investigate", "no_report"): 1}


def test_recount_summary_covers_all_items_under_a_limit(store: Dict[str, Path]) -> None:
    result = json.loads(_cli(store["state"], "--recount", "--limit", "1").stdout)
    assert len(result["items"]) == 1 and result["more"] == 2
    assert sum(p["count"] for p in result["recount_summary"]) == 3


def test_recount_table_has_the_fresh_column(store: Dict[str, Path]) -> None:
    env = dict(os.environ)
    env.pop("VNX_PROJECT_ID", None)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "receipt_query.py"), "open-outcomes", "--recount",
         "--state-dir", str(store["state"]), "--project-id", PROJECT],
        capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "fresh=accept" in proc.stdout and "fresh=no_report" in proc.stdout


def _snapshot(root: Path) -> Dict[str, Tuple[bytes, int]]:
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_a4_recount_writes_nothing(store: Dict[str, Path]) -> None:
    before = _snapshot(store["tmp"])
    assert _cli(store["state"], "--recount").returncode == 0
    assert _snapshot(store["tmp"]) == before
    assert not (store["state"] / oo.DECISION_LOG_NAME).exists()


def test_a5_output_without_recount_is_the_old_output(store: Dict[str, Path]) -> None:
    proc = _cli(store["state"])
    expected = json.dumps(oo.build_open_outcomes(store["state"], project_id=PROJECT, limit=None), indent=2)
    assert proc.stdout == expected + "\n"
    assert "recount" not in proc.stdout
