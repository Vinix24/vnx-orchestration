#!/usr/bin/env python3
"""The ``pr_size`` preflight of the merge door (ADR-088, pr-grootte-afgedwongen PG-1d).

After ``ci`` and before ``review``: ``pr_size_policy.verdict`` on the door's pinned head, measured
with git in the door's temp target repo (the fetch is stubbed as a success, no network). The
door, its fake GitHub and the receipts come from ``test_pr_merge_door_permit``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _path in (VNX_ROOT / "scripts", VNX_ROOT / "scripts" / "lib", Path(__file__).resolve().parent):
    sys.path.insert(0, str(_path))

import config_registry as cr
import pr_merge
import pr_size_policy as psp
import review_risk_class
from test_pr_merge_door_permit import GO, PR_DATA, FakeGitHub, _merged, _refusal, _run, door

FIELDS = {"verdict", "lines", "limit", "limit_kind", "security_paths_hit", "mode", "source", "reason", "waived",
          "detail", "errors"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def sized(door: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[str]]:
    """A head of 700 changed lines on top of ``origin/main`` in the door's target; review calls recorded."""
    for key in psp.KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(key)}", raising=False)
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "no-global" / "config.json"))
    monkeypatch.setattr(cr, "_db_resolver", None)
    target = tmp_path / "target"
    (target / "README").write_text("base\n")
    _git(target, "add", "-A")
    _git(target, "commit", "-qm", "base")
    _git(target, "update-ref", "refs/remotes/origin/main", "HEAD")
    (target / "notes").mkdir()
    (target / "notes" / "big.txt").write_text("".join(f"line {i}\n" for i in range(700)))
    _git(target, "add", "-A")
    _git(target, "commit", "-qm", "head")
    head = _git(target, "rev-parse", "HEAD")
    monkeypatch.setattr(review_risk_class, "fetch_merge_refs", lambda *a, **k: "")
    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(GO), dict(PR_DATA, headRefOid=head)))
    calls: Dict[str, List[str]] = {"review": []}
    monkeypatch.setattr(pr_merge, "_run_review_gate",
                        lambda pr, **k: calls["review"].append("review") or (dict(GO), dict(PR_DATA)))
    return calls


def _blocks(receipt: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """The ``pr_size`` block of the preflight record and of the receipt; both always carry every field."""
    record = next(g for g in receipt["preflight_gates"] if g["gate"] == "pr_size")
    assert set(record["pr_size"]) == FIELDS and set(receipt["pr_size"]) == FIELDS
    assert record["pr_size"] == receipt["pr_size"]
    return record, receipt["pr_size"]


def test_refuse_with_700_lines_refuses_on_pr_size_before_review(door, sized, monkeypatch) -> None:
    monkeypatch.setenv("VNX_PR_SIZE_ENFORCE", "refuse")
    assert _run() == pr_merge.EXIT_ERROR
    receipt = _refusal(door)
    record, block = _blocks(receipt)
    assert receipt["refused_by"] == "pr_size" and sized["review"] == []
    assert (record["verdict"], record["reason_code"]) == ("NO-GO", "pr_size_above_limit")
    assert (block["lines"], block["limit"], block["limit_kind"], block["mode"]) == (700, 450, "hold", "refuse")
    assert (block["source"], block["security_paths_hit"]) == ("default", [])
    gates = {g["gate"]: g["verdict"] for g in receipt["preflight_gates"]}
    assert gates["review"] == "not_evaluated" and door.receipts("pr_merged") == []


def test_warn_with_700_lines_merges_with_the_reason_on_the_receipt(door, sized) -> None:
    assert _run() == pr_merge.EXIT_OK
    record, block = _blocks(_merged(door))
    assert (record["verdict"], record["reason_code"]) == ("GO", "pr_size_above_limit")
    assert (block["verdict"], block["reason"], block["lines"], block["mode"]) == ("WARN", "pr_size_above_limit", 700, "warn")
    assert sized["review"] == ["review"]


def test_off_waives_without_a_measurement(door, sized, monkeypatch) -> None:
    monkeypatch.setenv("VNX_PR_SIZE_ENFORCE", "off")
    monkeypatch.setattr(review_risk_class, "fetch_merge_refs", lambda *a, **k: pytest.fail("measured under off"))
    assert _run() == pr_merge.EXIT_OK
    _, block = _blocks(_merged(door))
    assert (block["verdict"], block["waived"], block["mode"], block["lines"], block["limit"]) == ("GO", "pr_size", "off", None, None)


def test_a_measurement_error_under_refuse_names_the_step(door, sized, monkeypatch) -> None:
    monkeypatch.setenv("VNX_PR_SIZE_ENFORCE", "refuse")
    monkeypatch.setattr(review_risk_class, "fetch_merge_refs", lambda *a, **k: "git fetch exited 128: offline")
    assert _run() == pr_merge.EXIT_ERROR
    record, block = _blocks(_refusal(door))
    assert (record["reason_code"], block["lines"], block["mode"]) == ("pr_size_unmeasured", None, "refuse")
    assert block["detail"].startswith("fetch: ") and sized["review"] == []


def test_a_failing_store_refuses_config_unreadable(door, sized, tmp_path, monkeypatch) -> None:
    state = tmp_path / "data" / "state"
    (state / "runtime_coordination.db").write_bytes(b"this is no sqlite database" * 64)
    monkeypatch.setattr(pr_merge, "active_store", lambda: (state, "acme"))
    assert _run() == pr_merge.EXIT_ERROR
    record, block = _blocks(_refusal(door))
    assert record["reason_code"] == "pr_size_config_unreadable" and "project_config_error" in block["detail"]


def test_an_exception_in_the_gate_is_a_no_go_naming_its_type(door, sized, monkeypatch) -> None:
    monkeypatch.setattr(pr_merge, "active_store", lambda: (_ for _ in ()).throw(KeyError("VNX_STATE_DIR")))
    assert _run() == pr_merge.EXIT_ERROR
    record, block = _blocks(_refusal(door))
    assert record["reason_code"] == "pr_size_unmeasured" and block["detail"].startswith("door: KeyError")


def test_a_failing_ci_stops_before_pr_size(door, sized, monkeypatch) -> None:
    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(GO, verdict="NO-GO"), dict(PR_DATA)))
    monkeypatch.setattr(pr_merge, "_run_pr_size_gate", lambda *a, **k: pytest.fail("pr_size ran after a ci NO-GO"))
    assert _run() == pr_merge.EXIT_ERROR
    record, block = _blocks(_refusal(door))
    assert record["verdict"] == "not_evaluated" and block["verdict"] == "not_evaluated" and block["lines"] is None


def test_pr_size_sits_after_ci_and_before_review() -> None:
    order = pr_merge.PREFLIGHT_ORDER
    assert order.index("ci") + 1 == order.index("pr_size") == order.index("review") - 1
