"""GE-6 (dlv-6477c84d40bb): the local half of ci_gate and wiring_gate is a
``deterministic`` adapter row in the gate register (ADR-046 §4).

What this pins, one block per acceptance point:

1. A ``kind=deterministic`` gate produces its outcome from a fixture checkout with
   NO ``gh`` subprocess: ``subprocess.run`` is replaced by a recorder and no argv
   starts with ``gh``.
2. Its outcome is booked through ``commit_gate_outcome`` once: one result record,
   one ``review_gate_result`` receipt, one ended request — counted on disk.
3. A local check that cannot run is ``not_executable`` with the reason, never a pass.
4. The records and the report are the ones the OLD code wrote for the same fixture
   checkout, compared per field apart from the named volatile fields and ``kind``.
   The goldens under ``tests/fixtures/deterministic_gate/`` were produced on the
   branch base (03eb9d61) by driving the UNMOVED writers
   (``_request_wiring_gate`` / ``_execute_ci_gate``) with the same inputs.

Nothing here mocks the adapter under test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts", ROOT / "scripts" / "lib", ROOT):
    sys.path.insert(0, str(_p))

import deterministic_gate  # noqa: E402  (after the sys.path pins above)
import forge_gate_publisher  # noqa: E402
import gate_recorder  # noqa: E402
import gate_request_handler  # noqa: E402
import gate_runner  # noqa: E402
import wiring_gate  # noqa: E402
from gate_executor import GateExecutorMixin  # noqa: E402
from gate_request_handler import GateRequestHandlerMixin  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "deterministic_gate"
PR, BRANCH, HEAD = 4242, "dispatch/ge-6-fixture", "4f2c9a1b7e6d5c4a3b2f1e0d9c8b7a6958473625"
# Named one by one, never "everything that differs": a NEW random field must fail.
# ``dispatch_id`` is the run identity the caller may add, so it is ignored when
# present and never demanded.
WIRING_IGNORED = frozenset({"requested_at", "completed_at", "dispatch_id"})
WIRING_PRESENT = frozenset({"requested_at", "completed_at"})
CI_IGNORED = frozenset({"recorded_at", "duration_seconds", "dispatch_id"})
CI_PRESENT = frozenset({"recorded_at", "duration_seconds"})
NEW_KIND_FIELD = "kind"

_CHECKS = [
    {"name": "ci/test", "bucket": "pass", "state": "SUCCESS"},
    {"name": "ci/security", "bucket": "fail", "state": "FAILURE"},
    {"name": "ci/docs", "bucket": "skipping", "state": "SKIPPED"},
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway project store; nothing leaves this machine."""
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (state_dir / "review_gates" / "requests", state_dir / "review_gates" / "results", reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_HOME", str(ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.setattr(forge_gate_publisher, "publish_for_record", lambda *a, **k: None)
    monkeypatch.setattr(forge_gate_publisher, "publish_review_summary", lambda *a, **k: None)
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


class _Writers(GateExecutorMixin, GateRequestHandlerMixin):
    """The real writers, with only the path helpers a manager provides."""

    def __init__(self, env):
        self.requests_dir, self.results_dir = env["requests_dir"], env["results_dir"]
        self.state_dir, self.reports_dir = env["state_dir"], env["reports_dir"]

    def _request_path(self, gate, pr_number):
        return self.requests_dir / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate, pr_number):
        return self.results_dir / f"pr-{pr_number}-{gate}.json"


def _git(checkout: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *argv], capture_output=True, text=True, check=True,
    ).stdout.strip()


def _fixture_checkout(tmp_path: Path) -> tuple[Path, str, str]:
    """A checkout whose diff is the fixture diff: base has the caller, head adds both defs."""
    checkout = tmp_path / "checkout"
    (checkout / "app").mkdir(parents=True)
    (checkout / "app" / "unwired.py").write_text(
        '"""Fixture module for the wiring-gate golden: two new public definitions."""\n', encoding="utf-8",
    )
    (checkout / "app" / "caller.py").write_text(
        (FIXTURES / "checkout" / "app" / "caller.py").read_text(encoding="utf-8"), encoding="utf-8",
    )
    _git(checkout, "init", "-q")
    _git(checkout, "add", "-A")
    _git(checkout, "-c", "user.email=ge6@example.invalid", "-c", "user.name=ge6", "commit", "-qm", "base")
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "app" / "unwired.py").write_text(
        (FIXTURES / "checkout" / "app" / "unwired.py").read_text(encoding="utf-8"), encoding="utf-8",
    )
    _git(checkout, "add", "-A")
    _git(checkout, "-c", "user.email=ge6@example.invalid", "-c", "user.name=ge6", "commit", "-qm", "head")
    return checkout, base, _git(checkout, "rev-parse", "HEAD")


def _wiring_ctx(checkout: Path, base: str, head: str, **over):
    kwargs = dict(
        checkout=checkout, base_sha=base, head_sha=head, required=False,
        pr_number=PR, branch=BRANCH, requested_at="2026-10-27T10:00:00Z", recorded_at="2026-10-27T10:00:01Z",
        state_dir=checkout.parent / "state",
    )
    kwargs.update(over)
    return deterministic_gate.LocalContext(**kwargs)


# ---------------------------------------------------------------------------
# 1. A deterministic gate on a fixture checkout: no `gh`, ever
# ---------------------------------------------------------------------------


def test_the_wiring_gate_produces_its_outcome_without_a_gh_subprocess(tmp_path, monkeypatch):
    checkout, base, head = _fixture_checkout(tmp_path)
    seen: list[list[str]] = []
    real_run = subprocess.run

    def _recorder(argv, *a, **kwargs):
        seen.append([str(part) for part in argv])
        return real_run(argv, *a, **kwargs)

    monkeypatch.setattr(subprocess, "run", _recorder)

    run = deterministic_gate.run("wiring_gate", _wiring_ctx(checkout, base, head))

    assert run.outcome is not None
    assert run.record["kind"] == deterministic_gate.DETERMINISTIC_KIND
    assert run.record["status"] == "advisory", "one unwired symbol in shadow mode, as before"
    assert run.outcome.status == "pass", "advisory is the wiring gate's shadow-mode pass on the outcome axis"
    assert seen, "the local check must actually run: git for the diff, grep for the callers"
    assert [argv[0] for argv in seen if argv[0] == "gh"] == [], f"a gh subprocess ran: {seen}"
    assert {"git", "grep"} <= {argv[0] for argv in seen}


def test_the_ci_gate_produces_its_outcome_without_a_gh_subprocess(store, monkeypatch):
    seen: list[list[str]] = []

    def _recorder(argv, *a, **kwargs):
        seen.append([str(part) for part in argv])
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _recorder)

    run = deterministic_gate.run("ci_gate", deterministic_gate.LocalContext(
        pr_number=PR, branch=BRANCH, checks=_CHECKS, contract_hash="ge6fixturehash01",
        recorded_at="2026-10-27T10:00:00Z", report_path=str(store["reports_dir"] / "ci.md"),
    ))

    assert run.record["status"] == "fail"
    assert run.outcome is not None and run.outcome.status == "fail"
    assert seen == [], f"the ci local half shells out: {seen}"


# ---------------------------------------------------------------------------
# 2. Booked through commit_gate_outcome: one record, one receipt, one request end
# ---------------------------------------------------------------------------


def _gh_checks(monkeypatch):
    monkeypatch.setattr("gate_executor.shutil.which", lambda *a, **k: "/usr/bin/gh")
    monkeypatch.setattr("gate_executor.subprocess.run", lambda *a, **k: mock.Mock(
        returncode=0, stdout=json.dumps(_CHECKS), stderr=""))


def test_the_outcome_lands_once_through_the_one_writer(store, monkeypatch):
    _gh_checks(monkeypatch)
    writer = _Writers(store)
    request = {
        "gate": "ci_gate", "status": "requested", "provider": "gh_cli", "branch": BRANCH,
        "pr_number": PR, "requested_at": "2026-10-27T10:00:00Z",
        "report_path": str(store["reports_dir"] / f"ci_gate-pr-{PR}.md"),
        "contract_hash": "ge6fixturehash01", "commit_sha": HEAD,
    }

    result = writer._execute_ci_gate(gate="ci_gate", pr_number=PR, pr_id="", request_payload=request)

    assert (result["status"], result["kind"]) == ("fail", deterministic_gate.DETERMINISTIC_KIND)
    records = sorted(p.name for p in store["results_dir"].iterdir() if p.suffix == ".json")
    assert records == [f"pr-{PR}-ci_gate.json"], "exactly one result record"
    receipts = [
        json.loads(line) for line in (store["state_dir"] / "gate_events.ndjson").read_text().splitlines()
    ]
    assert [r["event_type"] for r in receipts] == ["review_gate_result"], "exactly one receipt"
    assert receipts[0]["status"] == "failed"
    requests = sorted(p.name for p in store["requests_dir"].iterdir() if p.suffix == ".json")
    assert requests == [f"pr-{PR}-ci_gate.json"], "exactly one request"
    on_disk = json.loads((store["requests_dir"] / f"pr-{PR}-ci_gate.json").read_text())
    assert on_disk["status"] == "completed", "the one request ended terminal"


# ---------------------------------------------------------------------------
# 3. A check that cannot run is not_executable, never pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over,detail",
    [
        ({"base_sha": ""}, "no diff text was handed"),
        ({"base_sha": "0" * 40}, "exited 128"),
    ],
)
def test_a_local_check_that_cannot_run_is_not_executable(tmp_path, over, detail):
    checkout, base, head = _fixture_checkout(tmp_path)

    run = deterministic_gate.run("wiring_gate", _wiring_ctx(checkout, base, head, **over))

    assert run.record["status"] == "not_executable"
    assert run.outcome is not None and run.outcome.status == "not_executable"
    assert run.record["reason"] == "diff_unavailable"
    assert detail in run.record["reason_detail"]
    assert run.record["status"] != "pass"


def test_an_unknown_gate_has_no_row():
    with pytest.raises(KeyError):
        deterministic_gate.run("gemini_review", deterministic_gate.LocalContext())


# ---------------------------------------------------------------------------
# 4. The goldens: the records and the report of the old code, per field
# ---------------------------------------------------------------------------


def test_the_wiring_record_is_the_pre_move_record_plus_kind(tmp_path):
    checkout, base, head = _fixture_checkout(tmp_path)
    golden = json.loads((FIXTURES / "wiring-record.json").read_text(encoding="utf-8"))

    record = deterministic_gate.run("wiring_gate", _wiring_ctx(checkout, base, head)).record

    assert {k: v for k, v in record.items() if k not in WIRING_IGNORED | {NEW_KIND_FIELD}} == golden
    assert WIRING_PRESENT <= set(record)
    assert record[NEW_KIND_FIELD] == deterministic_gate.DETERMINISTIC_KIND


def test_the_ci_record_and_report_are_the_pre_move_ones_plus_kind(store, monkeypatch):
    _gh_checks(monkeypatch)
    report_path = store["reports_dir"] / f"ci_gate-pr-{PR}.md"
    golden_record = json.loads((FIXTURES / "ci-record.json").read_text(encoding="utf-8"))

    writer = _Writers(store)
    writer._execute_ci_gate(gate="ci_gate", pr_number=PR, pr_id="", request_payload={
        "gate": "ci_gate", "status": "requested", "provider": "gh_cli", "branch": BRANCH,
        "pr_number": PR, "review_mode": "per_pr", "risk_class": "medium", "changed_files": [],
        "requested_at": "2026-10-27T10:00:00Z", "report_path": str(report_path),
        "contract_hash": "ge6fixturehash01", "commit_sha": HEAD,
    })

    on_disk = json.loads((store["results_dir"] / f"pr-{PR}-ci_gate.json").read_text())
    comparable = {k: v for k, v in on_disk.items() if k not in CI_IGNORED | {NEW_KIND_FIELD}}
    comparable["report_path"] = "<reports>/ci_gate-pr-4242.md"
    assert comparable == golden_record
    assert CI_PRESENT <= set(on_disk)
    assert on_disk[NEW_KIND_FIELD] == deterministic_gate.DETERMINISTIC_KIND
    report = report_path.read_text(encoding="utf-8").replace(on_disk["recorded_at"], "<generated_at>")
    assert report == (FIXTURES / "ci-report.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The register: one row per deterministic gate, one kind name
# ---------------------------------------------------------------------------


def test_the_register_and_the_adapter_rows_agree():
    kind = gate_recorder.GATE_PROVIDER_DETERMINISTIC
    assert kind == deterministic_gate.DETERMINISTIC_KIND
    registered = {gate for gate, (k, _n) in gate_recorder.GATE_PROVIDERS.items() if k == kind}
    assert registered == set(deterministic_gate.DETERMINISTIC_GATES) == {"ci_gate", "wiring_gate"}
    for gate in registered:
        assert gate_recorder.GATE_PROVIDERS[gate][1] == "local"
        assert gate_recorder.gate_is_available(gate) is True, "a deterministic gate is available by registration"


def test_the_subprocess_runner_refuses_a_deterministic_gate_by_name(store):
    """GateRunner drives prompted CLIs; it must not look up a binary named after the kind."""
    runner = gate_runner.GateRunner(store["state_dir"], store["reports_dir"], project_root=store["project_root"])

    result = runner.run(gate="wiring_gate", request_payload={"gate": "wiring_gate", "status": "requested"},
                        pr_number=PR)

    assert (result["status"], result["reason"]) == ("not_executable", "gate_not_subprocess_routable")
    assert "deterministic" in result["reason_detail"]
