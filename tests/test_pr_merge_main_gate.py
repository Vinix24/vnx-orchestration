#!/usr/bin/env python3
"""The ``main`` preflight of the merge door (ADR-073, ci-cost-and-merge-cadence PR 3b part b).

Off without ``VNX_MAIN_UNVERIFIED_MAX``; with it a red, unmeasurable or full window is a NO-GO
that stops the door before it publishes a permit. ``--override-main`` is its own escape hatch.
``read_admission_config`` and ``read_window`` are patched: no ``gh``, no network, no real store.
The door, its fake GitHub and the receipts come from ``test_pr_merge_door_permit``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _path in (VNX_ROOT / "scripts", VNX_ROOT / "scripts" / "lib", Path(__file__).resolve().parent):
    sys.path.insert(0, str(_path))

import main_window as mw  # noqa: E402
import pr_merge  # noqa: E402
from test_pr_merge_door_permit import (  # noqa: E402,F401  (door is a fixture)
    GO, PR, SHA, FakeGitHub, _merged, _refusal, _run, door,
)

RUNNING_SHA, RED_SHA = "a" * 40, "b" * 40


def _entry(sha: str, state: str, run_id: int) -> mw.WindowEntry:
    return mw.WindowEntry(sha=sha, run_id=run_id, status="in_progress", conclusion=None, state=state,
                          pr_number=None, dispatch_id=None, mapping=mw.MAPPING_NO_RECEIPT)


def _window(*entries: mw.WindowEntry, verdict: str = mw.VERDICT_RUNNING, reason: Optional[str] = None) -> mw.MainWindow:
    red = next((e for e in entries if e.state == mw.RUN_RED), None)
    return mw.MainWindow(verdict=mw.VERDICT_RED if red else verdict, base_sha="c" * 40, base_run_id=1,
                         entries=tuple(entries), first_red=red, reason=reason)


def _configure(monkeypatch: pytest.MonkeyPatch, window: Any, config: Tuple[Any, int, Tuple[str, ...]] = (1, 40, ())) -> List[str]:
    """Patch the two reads; the returned list records every ``read_window`` call."""
    reads: List[str] = []
    monkeypatch.setattr(pr_merge, "read_admission_config", lambda project_id, state_dir: config)

    def read(root: Any, **kwargs: Any) -> mw.MainWindow:
        reads.append("read")
        if isinstance(window, Exception):
            raise window
        return window

    monkeypatch.setattr(pr_merge, "read_window", read)
    return reads


def _no_permit(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    calls: List[str] = []
    monkeypatch.setattr(pr_merge, "_run_door_permit", lambda *a, **k: calls.append("permit") or pytest.fail("permit"))
    return calls


def _main_record(receipt: Dict[str, Any]) -> Dict[str, Any]:
    return next(g for g in receipt["preflight_gates"] if g["gate"] == "main")


# --- mode off -----------------------------------------------------------------------------


def test_mode_off_is_go_and_reads_no_window(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _configure(monkeypatch, _window(), config=(None, 40, ()))
    assert _run() == pr_merge.EXIT_OK
    receipt = _merged(door)
    assert reads == []
    assert receipt["main_admission"] == {
        "mode": "off", "verdict": "GO", "reason": "niet geconfigureerd", "max_unverified": None,
        "overridden": False, "override_reason": None, "lock_wait_s": 0,
    }
    record = _main_record(receipt)
    assert (record["verdict"], record["mode"], record["overridden"]) == ("GO", "off", False)
    assert [g["gate"] for g in receipt["preflight_gates"]][-2:] == ["main", "door"]
    assert len(door.of("POST")) == 1, "the merge itself is the same as before: one permit, one merge"


# --- NO-GO -------------------------------------------------------------------------------


def test_full_window_refuses_before_the_permit(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window(_entry(RUNNING_SHA, mw.RUN_RUNNING, 11)))
    permit = _no_permit(monkeypatch)
    assert _run() == pr_merge.EXIT_ERROR
    receipt = _refusal(door)
    assert permit == [] and door.calls == [] and door.receipts("pr_merged") == []
    assert receipt["refused_by"] == "main"
    admission = receipt["main_admission"]
    assert (admission["mode"], admission["verdict"]) == ("on", "NO-GO")
    assert admission["reason"].startswith("unverified_limit") and admission["running_shas"] == [RUNNING_SHA]
    assert (admission["overridden"], admission["override_reason"]) == (False, None)
    gates = {g["gate"]: g for g in receipt["preflight_gates"]}
    assert gates["main"]["verdict"] == "NO-GO" and gates["door"]["verdict"] == "not_evaluated"


def test_a_red_run_names_the_sha_and_run_id(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window(_entry(RED_SHA, mw.RUN_RED, 4242)))
    assert _run() == pr_merge.EXIT_ERROR
    reason = _refusal(door)["main_admission"]["reason"]
    assert reason.startswith("red_run") and RED_SHA in reason and "4242" in reason


def test_an_unmeasurable_window_refuses(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window(verdict=mw.VERDICT_UNMEASURABLE, reason="gh_unreadable"))
    assert _run() == pr_merge.EXIT_ERROR
    admission = _refusal(door)["main_admission"]
    assert (admission["verdict"], admission["reason"]) == ("NO-GO", "gh_unreadable")


@pytest.mark.parametrize("max_n", [None, 3])
def test_config_unreadable_refuses_also_without_n(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, max_n: Optional[int]) -> None:
    invalid = (f"{mw.UNVERIFIED_MAX_KEY}: {mw.CONFIG_UNREADABLE}",)
    reads = _configure(monkeypatch, _window(), config=(max_n, 40, invalid))
    assert _run() == pr_merge.EXIT_ERROR
    assert reads == ["read"], "an unreadable store is not mode off: the window is read and judged"
    assert _refusal(door)["main_admission"]["reason"] == "config_unreadable"


@pytest.mark.parametrize("failing", ["window", "config"])
def test_a_failing_read_is_a_no_go_naming_the_exception_type(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, failing: str,
) -> None:
    _configure(monkeypatch, RuntimeError("boom") if failing == "window" else _window())
    if failing == "config":
        def broken(project_id: Any, state_dir: Any) -> None:
            raise KeyError("store")
        monkeypatch.setattr(pr_merge, "read_admission_config", broken)
    assert _run() == pr_merge.EXIT_ERROR
    admission = _refusal(door)["main_admission"]
    assert admission["verdict"] == "NO-GO"
    assert admission["reason"] == f"window_unreadable: {'RuntimeError' if failing == 'window' else 'KeyError'}"


@pytest.mark.parametrize("verdict", [None, "", "go", "surprise"])
def test_only_a_verdict_that_is_exactly_go_passes(monkeypatch: pytest.MonkeyPatch, verdict: Any) -> None:
    monkeypatch.setattr(pr_merge, "_read_main_admission", lambda: {"mode": "on", "verdict": verdict, "reason": "x"})
    gate = pr_merge._run_main_gate(_Args(None), SHA)
    assert gate["verdict"] == "NO-GO"


class _Args:
    def __init__(self, override: Optional[str]) -> None:
        self.override_main = override


# --- --override-main ----------------------------------------------------------------------


def test_override_main_turns_a_red_window_into_a_go_with_the_reason_on_both_records(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    _configure(monkeypatch, _window(_entry(RED_SHA, mw.RUN_RED, 4242)))
    assert _run("--override-main", "revert_or_fix_forward") == pr_merge.EXIT_OK
    receipt = _merged(door)
    admission = receipt["main_admission"]
    assert (admission["verdict"], admission["overridden"], admission["override_reason"]) == (
        "NO-GO", True, "revert_or_fix_forward")
    record = _main_record(receipt)
    assert (record["verdict"], record["overridden"]) == ("GO", True)
    assert "OVERRIDE: " in capsys.readouterr().out


def test_override_reason_does_not_cover_the_main_gate(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window(_entry(RED_SHA, mw.RUN_RED, 4242)))
    assert _run("--override-reason", "flaky") == pr_merge.EXIT_ERROR
    assert _refusal(door)["refused_by"] == "main"


@pytest.mark.parametrize("reason", ["  ", ""])
def test_an_empty_override_is_refused(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    _configure(monkeypatch, _window(_entry(RED_SHA, mw.RUN_RED, 4242)))
    assert _run("--override-main", reason) == pr_merge.EXIT_ERROR
    receipt = _refusal(door)
    assert receipt["refused_by"] == "main"
    # not overridden; the empty reason stays on the record as the trace of an attempt without one
    assert (receipt["main_admission"]["overridden"], receipt["main_admission"]["override_reason"]) == (False, "")
    assert "vereist een niet-lege reden" in _main_record(receipt)["message"]


def test_an_override_on_a_go_is_reported_unused(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window())
    assert _run("--override-main", "just_in_case") == pr_merge.EXIT_OK
    receipt = _merged(door)
    assert (receipt["main_admission"]["overridden"], receipt["main_admission"]["override_reason"]) == (False, None)
    record = _main_record(receipt)
    assert (record["overridden"], record["override_unnecessary"]) == (False, True)


# --- before the main gate, and after it ---------------------------------------------------


def test_a_refusal_earlier_in_the_order_leaves_main_not_evaluated(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _configure(monkeypatch, _window(_entry(RED_SHA, mw.RUN_RED, 4242)))
    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(GO, verdict="NO-GO", message="ci rood"), None))
    assert _run() == pr_merge.EXIT_ERROR
    receipt = _refusal(door)
    assert reads == [] and receipt["main_admission"] == {"verdict": "not_evaluated"}
    gates = {g["gate"]: g for g in receipt["preflight_gates"]}
    assert gates["main"]["verdict"] == "not_evaluated" and "ci-preflight" in gates["main"]["message"]


def test_a_door_refusal_after_a_main_go_still_carries_the_admission(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window())
    door.merge = lambda: (False, "merge conflict")
    assert _run() == pr_merge.EXIT_ERROR
    receipt = _refusal(door)
    assert receipt["refused_by"] == "merge"
    assert (receipt["main_admission"]["mode"], receipt["main_admission"]["verdict"]) == ("on", "GO")


def test_the_door_evidence_carries_a_main_record(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window(), config=(None, 40, ()))
    seen: List[Dict[str, Any]] = []
    real = pr_merge.forge_gate_publisher.publish_door_permit
    monkeypatch.setattr(pr_merge.forge_gate_publisher, "publish_door_permit", lambda ev, **kw: seen.append(ev) or real(ev, **kw))
    assert _run() == pr_merge.EXIT_OK
    (evidence,) = seen
    assert [g["gate"] for g in evidence["preflight_gates"]][-1] == "main"
    assert pr_merge.PREFLIGHT_ORDER == ("ci", "review", "adr", "contract_invalid", "branch_protection", "main", "door")


def test_a_direct_merge_pr_records_main_admission_not_evaluated(door: FakeGitHub) -> None:
    result = pr_merge.merge_pr(PR, dispatch_id="dlv-door-d4", head_sha=SHA, pr_data={"title": "t", "headRefName": "b"})
    assert result["success"] is True
    assert _merged(door)["main_admission"] == {"verdict": "not_evaluated"}


# --- one lock per project, the window read again under it (part c) -----------------------


import fcntl  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

_REAL_SLEEP = time.sleep  # the door fixture turns time.sleep into a no-op


def _lock_path(door: FakeGitHub) -> Path:
    return door.ledger.parent / pr_merge.MAIN_LOCK_FILE


def _lock_is_free(door: FakeGitHub) -> bool:
    with open(_lock_path(door), "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True


def _receipt_counting_window(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    """N=1; a merge counts the moment its pr_merged receipt exists, the commit list never shows it."""
    monkeypatch.setattr(pr_merge, "read_admission_config", lambda project_id, state_dir: (1, 40, ()))

    def read(root: Any, **kwargs: Any) -> mw.MainWindow:
        merged = door.receipts("pr_merged")
        return _window(*(_entry(RUNNING_SHA, mw.RUN_RUNNING, 11) for _ in merged[:1]))

    monkeypatch.setattr(pr_merge, "read_window", read)


def test_two_parallel_doors_at_n1_give_one_merge_and_one_refusal(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _receipt_counting_window(door, monkeypatch)
    first_in_merge, second_started = threading.Event(), threading.Event()
    merges: List[str] = []

    def slow_merge() -> Tuple[bool, str]:
        merges.append("merge")
        first_in_merge.set()
        second_started.wait(5)
        _REAL_SLEEP(1.0)
        return True, ""

    door.merge = slow_merge
    exits: Dict[str, int] = {}

    def first() -> None:
        exits["a"] = _run()

    def second() -> None:
        first_in_merge.wait(5)
        second_started.set()
        exits["b"] = _run()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(exits.values()) == [pr_merge.EXIT_OK, pr_merge.EXIT_ERROR]
    assert len(merges) == 1 and len(door.receipts("pr_merged")) == 1
    admission = _refusal(door)["main_admission"]
    assert admission["verdict"] == "NO-GO"
    assert admission["reason"].startswith("main_window_changed_under_lock:")
    assert _lock_is_free(door)


def test_a_holder_past_the_limit_refuses_as_main_lock_timeout(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window())
    monkeypatch.setattr(pr_merge, "MAIN_LOCK_TIMEOUT_S", 0.3)
    monkeypatch.setattr(pr_merge, "MAIN_LOCK_POLL_S", 0.02)
    with open(_lock_path(door), "a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        holder.seek(0)
        holder.truncate()
        holder.write("pid=4242 pr=7 since=2026-11-13T00:00:00Z\n")
        holder.flush()
        assert _run() == pr_merge.EXIT_ERROR
    admission = _refusal(door)["main_admission"]
    assert admission["verdict"] == "NO-GO" and admission["reason"].startswith("main_lock_timeout")
    assert "pid=4242" in admission["reason"]
    assert _refusal(door)["refused_by"] == "main" and door.receipts("pr_merged") == []
    assert door.of("POST") == [], "no permit without the lock"


def test_mode_off_opens_no_lock_and_reads_the_admission_once(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window(), config=(None, 40, ()))
    calls: List[str] = []
    real = pr_merge._read_main_admission
    monkeypatch.setattr(pr_merge, "_read_main_admission", lambda: calls.append("read") or real())
    assert _run() == pr_merge.EXIT_OK
    assert calls == ["read"] and not _lock_path(door).exists()
    assert _merged(door)["main_admission"]["lock_wait_s"] == 0


def test_the_lock_is_released_after_a_merge_a_refusal_and_a_raising_merge(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _window())
    assert _run() == pr_merge.EXIT_OK and _lock_is_free(door)
    assert "lock_wait_s" in _merged(door)["main_admission"]

    _configure(monkeypatch, _window(_entry(RUNNING_SHA, mw.RUN_RUNNING, 11)))
    assert _run() == pr_merge.EXIT_ERROR and _lock_is_free(door)

    _configure(monkeypatch, _window())

    def boom() -> Tuple[bool, str]:
        raise RuntimeError("merge exploded")

    door.merge = boom
    with pytest.raises(RuntimeError):
        _run()
    assert _lock_is_free(door)


def test_override_main_applies_to_the_re_read_under_the_lock(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_merge, "read_admission_config", lambda project_id, state_dir: (1, 40, ()))
    windows = iter([_window(), _window(_entry(RUNNING_SHA, mw.RUN_RUNNING, 11))])
    monkeypatch.setattr(pr_merge, "read_window", lambda root, **kw: next(windows))
    assert _run("--override-main", "operator accepts the second place") == pr_merge.EXIT_OK
    admission = _merged(door)["main_admission"]
    assert admission["overridden"] is True and admission["override_reason"] == "operator accepts the second place"
    assert admission["reason"].startswith("unverified_limit")
