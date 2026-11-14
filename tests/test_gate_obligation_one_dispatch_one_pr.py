"""One dispatch is one PR (OI-1563); the runner finds the PR through the
dispatch registration before the branch name (OI-1751). ``-r2`` = same PR,
``-c`` = a split with its own dispatch id (operator decision 2026-11-14)."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    ObligationPrConflict,
    obligation_path,
    register_no_gate_obligation,
    register_obligation,
    update_obligation,
)

D = "dlv-oneprtest"
OWNER_REPO = "Vinix24/vnx-orchestration"


def _read(state_dir: Path, dispatch_id: str) -> dict:
    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


# -- OI-1563: register_obligation ------------------------------------------


def test_second_pr_on_one_dispatch_is_refused_naming_both_prs(tmp_path):
    register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_number=101, pr_id="101")
    with pytest.raises(ObligationPrConflict) as exc:
        register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_number=102, pr_id="102")
    msg = str(exc.value)
    assert "101" in msg and "102" in msg
    assert "one dispatch is one PR" in msg
    assert "new dispatch id" in msg
    assert _read(tmp_path, D)["pr_number"] == 101


@pytest.mark.parametrize("first, stamp, second", [
    (None, 101, "PR-102"),  # against the runner-stamped pr_number
    ("PR-HYG-1", None, "PR-HYG-2"),  # label-only PR ids
])
def test_second_pr_refused_by_number_or_label(tmp_path, first, stamp, second):
    path = register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_id=first)
    update_obligation(path, pr_number=stamp)
    with pytest.raises(ObligationPrConflict):
        register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_id=second)


def test_same_pr_again_stays_idempotent(tmp_path):
    first = register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_number=101, pr_id="PR-101")
    update_obligation(first, status="fulfilled")
    again = register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_number=101, pr_id="101")
    assert again == first
    assert register_obligation(tmp_path, dispatch_id=D, gate="codex_gate") == first  # no PR named
    assert _read(tmp_path, D)["status"] == "fulfilled"


def test_label_with_stamped_number_is_the_same_pr(tmp_path):
    path = register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_id="PR-HYG-1")
    update_obligation(path, pr_number=2800)
    assert register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_id="PR-HYG-1") == path


def test_unreadable_existing_obligation_refuses_a_pr_registration(tmp_path):
    path = obligation_path(tmp_path, D)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_number=102)


def test_r2_round_on_same_pr_registers_fine(tmp_path):
    register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_number=101)
    path = register_obligation(tmp_path, dispatch_id=f"{D}-r2", gate="codex_gate", pr_number=101, pr_id="101")
    assert path is not None and _read(tmp_path, f"{D}-r2")["pr_number"] == 101


def test_c_split_with_own_id_registers_fine(tmp_path):
    register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_number=101)
    path = register_obligation(tmp_path, dispatch_id=f"{D}-c", gate="codex_gate", pr_number=102)
    assert path is not None and _read(tmp_path, f"{D}-c")["pr_number"] == 102
    assert _read(tmp_path, D)["pr_number"] == 101


def test_no_gate_registration_refuses_a_second_pr_too(tmp_path):
    register_no_gate_obligation(tmp_path, dispatch_id=D, pr_number=101)
    with pytest.raises(ObligationPrConflict):
        register_no_gate_obligation(tmp_path, dispatch_id=D, pr_number=102)


def test_door_turns_the_refusal_into_a_blocking_verdict(tmp_path):
    import dispatch_cli

    register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_number=101)
    spec = type("Spec", (), {
        "gate": "codex_gate", "dispatch_id": D, "project_id": "vnx-dev", "pr_id": "102",
    })()
    verdict = dispatch_cli._register_gate_obligation(spec, state_dir=tmp_path)
    assert verdict is not None and verdict.severity == "blocking"
    assert "ObligationPrConflict" in verdict.message and "102" in verdict.message


# -- OI-1751: the runner reads the registration before the branch name -----


def _no_branch_pr(monkeypatch, calls):
    """GitHub knows no PR on dispatch/<id>: the PR lives on another branch."""
    def _pr_from_github(did, owner_repo, snapshot=None):
        calls.append(did)
        return None

    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: OWNER_REPO)
    monkeypatch.setattr(runner, "_pr_from_github", _pr_from_github)
    monkeypatch.setattr(runner, "_branch_exists_on_github", lambda did, o, snapshot=None: False)
    monkeypatch.setattr(runner, "_dispatch_is_live", lambda sd, did: False)


def test_runner_finds_pr_from_registered_pr_id_off_branch(tmp_path, monkeypatch):
    path = register_obligation(tmp_path, dispatch_id=D, gate="codex_gate", pr_id="PR-101")
    update_obligation(path, pr_number=None, branch="fix/not-a-dispatch-branch")
    calls: list = []
    _no_branch_pr(monkeypatch, calls)
    resolution = runner.resolve_pr_number(tmp_path, _read(tmp_path, D))
    assert resolution.status == runner.RESOLUTION_RESOLVED
    assert resolution.pr_number == 101
    assert calls == []


def test_runner_finds_pr_from_dispatch_metadata_before_branch(tmp_path, monkeypatch):
    register_obligation(tmp_path, dispatch_id=D, gate="codex_gate")
    conn = sqlite3.connect(tmp_path / "quality_intelligence.db")
    conn.execute("CREATE TABLE dispatch_metadata (id INTEGER PRIMARY KEY, dispatch_id TEXT, pr_id TEXT)")
    conn.execute("INSERT INTO dispatch_metadata (dispatch_id, pr_id) VALUES (?, ?)", (D, "PR-303"))
    conn.commit()
    conn.close()
    calls: list = []
    _no_branch_pr(monkeypatch, calls)
    resolution = runner.resolve_pr_number(tmp_path, _read(tmp_path, D))
    assert (resolution.status, resolution.pr_number) == (runner.RESOLUTION_RESOLVED, 303)
    assert calls == []


def test_branch_lookup_only_without_registration_and_logged(tmp_path, monkeypatch, caplog):
    register_obligation(tmp_path, dispatch_id=D, gate="codex_gate")
    calls: list = []
    _no_branch_pr(monkeypatch, calls)
    with caplog.at_level(logging.INFO, logger=runner._LOG.name):
        resolution = runner.resolve_pr_number(tmp_path, _read(tmp_path, D))
    assert resolution.status == runner.RESOLUTION_AWAITING
    assert calls == [D]
    lines = [r.getMessage() for r in caplog.records if "no dispatch registration" in r.getMessage()]
    assert len(lines) == 1 and f"dispatch/{D}" in lines[0]


@pytest.mark.parametrize("bad", [None, "", "pr-4d", "garbage", 0, -3, True, "101"])
def test_malformed_registered_pr_is_not_a_resolution(tmp_path, monkeypatch, bad):
    """A missing or malformed registration never resolves on its own; the
    stamped pr_number must be a positive int, the pr_id a PR-shaped string."""
    path = register_obligation(tmp_path, dispatch_id=D, gate="codex_gate")
    update_obligation(path, pr_number=bad, pr_id=None)
    calls: list = []
    _no_branch_pr(monkeypatch, calls)
    resolution = runner.resolve_pr_number(tmp_path, _read(tmp_path, D))
    assert resolution.status == runner.RESOLUTION_AWAITING
    assert calls == [D]
