"""GE-4c-2 (dlv-636dc9d85875): gate_request_handler's result writers, its request writers and its
``review_gate_request`` emitters through one path each.

On 9ce62b5f (this PR's base) the request writers take no slot lock (Q5-Q7, three of them a bare
``write_text``), the takeover annotation reads, merges and writes with the lock only around the
write (R11), and four emitters book four slightly different ``review_gate_request`` lines (E4).
The four goldens under ``tests/fixtures/ge4c2/`` were taken on that head, before any of this
changed, so each call path can be compared field by field.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_recorder
import review_stack_support
from atomic_io import slot_lock
from gate_request_handler import REVIEW_REQUEST_RECEIPT_FIELDS
from review_contract import ReviewContract

PR, BRANCH, HEAD = review_stack_support.PR, review_stack_support.BRANCH, review_stack_support.HEAD_SHA
HEAD_B = "b" * 40
FIXED_NOW = "2026-10-27T10:00:00Z"
CONTRACT = dict(pr_id="PR-4", branch=BRANCH, risk_class="medium",
                changed_files=["scripts/lib/gate_request_handler.py"], content_hash="contract-hash")
GOLDEN = json.loads((VNX_ROOT / "tests/fixtures/ge4c2/ge4c2_receipts_golden.json").read_text(encoding="utf-8"))
# Random (or process) by design: the append time, its ingest time and the line's hash over both.
RANDOM_FIELDS = {"timestamp", "ingested_at", "outcome_id"}
# What the append layer adds around whatever the emitter hands it.
APPEND_FIELDS = {"event_type", "receipt_kind", "terminal", "source", "schema_version", "verdict",
                 "open_items_created", "timestamp", "ingested_at", "outcome_id"}
STORE_PATH = re.compile(r"[^\s\"]*/project/\.vnx-data")


@pytest.fixture
def env(tmp_path, monkeypatch):
    # The append layer stamps these from the process; the goldens were taken without them.
    for name in ("VNX_PARENT_DISPATCH", "VNX_TASK_CLASS", "VNX_TIER_FROM", "VNX_TIER_TO"):
        monkeypatch.delenv(name, raising=False)
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    headless = store["data_dir"] / "unified_reports" / "headless"
    headless.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_HEADLESS_REPORTS_DIR", str(headless))
    store["headless_reports_dir"] = headless
    # The PR base is a gh round-trip and the manager clock would leak into report_path.
    monkeypatch.setattr("gate_request_handler.get_pr_base_branch", lambda _pr: "")
    import review_gate_manager as rgm
    monkeypatch.setattr(rgm, "_utc_now", lambda: FIXED_NOW)
    monkeypatch.setattr(rgm.secrets, "token_hex", lambda n: "0" * (2 * n))
    return store


def _manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _lines(env, event_type):
    lines = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):  # a receipt without dispatch_id is rerouted
        path = env["state_dir"] / name
        lines += path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [r for r in map(json.loads, filter(str.strip, lines)) if r.get("event_type") == event_type]


def _request_receipts(env):
    return _lines(env, "review_gate_request")


def _result_receipts(env):
    return _lines(env, "review_gate_result")


def _contract():
    return ReviewContract(**CONTRACT)


def _normalise(line):
    text = STORE_PATH.sub("<store>/project/.vnx-data", json.dumps(line, sort_keys=True))
    return json.loads(text)


def _request_slot(env, gate="claude_github_optional"):
    return env["requests_dir"] / f"pr4-{gate}-contract.json"


def _pr_slot(env, gate):
    return env["requests_dir"] / f"pr-{PR}-{gate}.json"


# ---------------------------------------------------------------------------
# E4: one emitter, one field set — four call paths against the goldens
# ---------------------------------------------------------------------------


def _stack(env):
    manager = _manager()
    with mock.patch("review_gate_manager.shutil.which", side_effect=lambda t: "/usr/bin/fake" if t == "codex" else None), \
            mock.patch.dict(os.environ, {"VNX_CODEX_HEADLESS_ENABLED": "1", "VNX_GEMINI_REVIEW_ENABLED": "0",
                                         "VNX_CLAUDE_GITHUB_REVIEW_ENABLED": "0", "VNX_CI_GATE_REQUIRED": "0"}):
        manager.request_reviews(
            pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="medium",
            changed_files=["scripts/lib/gate_request_handler.py"], mode="per_pr",
            dispatch_id="dlv-golden-stack", head_sha=HEAD)


def _gemini(env):
    manager = _manager()
    with mock.patch("gate_request_handler.render_gemini_prompt", lambda _c: "rendered prompt"), \
            mock.patch("review_gate_manager.shutil.which", return_value="/usr/bin/fake"), \
            mock.patch.dict(os.environ, {"VNX_GEMINI_REVIEW_ENABLED": "1"}):
        manager.request_gemini_with_contract(contract=_contract(), mode="per_pr", dispatch_id="dlv-golden-gemini")


def _claude_github(env):
    manager = _manager()
    with mock.patch.dict(os.environ, {"VNX_CLAUDE_GITHUB_REVIEW_ENABLED": "0"}):
        manager.request_claude_github_with_contract(contract=_contract(), mode="per_pr",
                                                    dispatch_id="dlv-golden-claude-gh")


def _ci_gate(env):
    manager = _manager()
    with mock.patch("review_gate_manager.shutil.which", side_effect=lambda t: "/usr/bin/gh" if t == "gh" else None), \
            mock.patch.dict(os.environ, {"VNX_CI_GATE_REQUIRED": "1"}):
        manager.request_ci_gate_with_contract(contract=_contract(), pr_number=PR, mode="per_pr",
                                             dispatch_id="dlv-golden-ci")


ROWS = {"stack": _stack, "gemini_contract": _gemini,
        "claude_github_contract": _claude_github, "ci_gate_contract": _ci_gate}


@pytest.mark.parametrize("row", sorted(ROWS))
def test_each_call_path_books_exactly_one_line_with_the_goldens_fields(env, row):
    ROWS[row](env)
    (line,) = _request_receipts(env)
    golden = GOLDEN["receipts"][row]
    line = _normalise(line)
    assert set(golden) <= set(line), f"dropped: {sorted(set(golden) - set(line))}"
    changed = {k for k in golden if k not in RANDOM_FIELDS and line[k] != golden[k]}
    assert changed == set(), {k: (golden[k], line[k]) for k in changed}
    emitted = set(line) - APPEND_FIELDS - {"status"}  # status is the emitter's own kwarg, not a field
    assert emitted <= set(REVIEW_REQUEST_RECEIPT_FIELDS), (
        "the one field set is the whole vocabulary: " + str(sorted(emitted - set(REVIEW_REQUEST_RECEIPT_FIELDS)))
    )


def test_the_four_call_paths_emit_the_same_event_from_one_emitter():
    import inspect

    import gate_request_handler

    source = inspect.getsource(gate_request_handler)
    assert source.count('"review_gate_request"') == 1, "one spelling: _REVIEW_REQUEST_EVENT"
    assert gate_request_handler.REVIEW_REQUEST_RECEIPT_FIELDS  # the field set is named in one place


# ---------------------------------------------------------------------------
# R10: the optional-gate result mirror
# ---------------------------------------------------------------------------


def test_r10_the_mirror_books_one_record_no_receipt_and_a_refusal_changes_no_request(env):
    manager = _manager()
    with mock.patch.dict(os.environ, {"VNX_CLAUDE_GITHUB_REVIEW_ENABLED": "0"}):
        manager.request_claude_github_with_contract(contract=_contract(), dispatch_id="dlv-r10")
    request_slot, result_slot = _request_slot(env), env["results_dir"] / _request_slot(env).name
    request, mirror = _json(request_slot), _json(result_slot)
    assert (request["state"], mirror["state"]) == ("not_configured", "not_configured")
    assert "attempt_seq" not in mirror, "a mirror of a request state is no outcome"
    assert [r["status"] for r in _result_receipts(env)] == [], "no decided verdict, so no outcome receipt"
    assert (env["requests_dir"] / f".{request_slot.name}.lock").exists(), "the request went through its slot lock"

    result_slot.write_text('{"state": "not_conf', encoding="utf-8")
    before_request = request_slot.read_bytes()
    with mock.patch.dict(os.environ, {"VNX_CLAUDE_GITHUB_REVIEW_ENABLED": "0"}):
        manager.request_claude_github_with_contract(contract=_contract(), dispatch_id="dlv-r10")
    assert result_slot.read_text(encoding="utf-8") == '{"state": "not_conf'
    assert request_slot.read_bytes() == before_request, "a refused mirror changes no request"
    assert _result_receipts(env) == [], "a refused mirror books nothing"


# ---------------------------------------------------------------------------
# R11: the takeover annotation on a record someone else decided
# ---------------------------------------------------------------------------


def _seed_decided(env, status="pass", head=HEAD):
    from gate_outcome import GateOutcome, commit_gate_outcome

    record = {"gate": "codex_gate", "pr_id": "", "pr_number": PR, "status": status, "commit_sha": head,
              "contract_hash": "hash-1", "report_path": f"/tmp/codex-{head[:4]}.md",
              "blocking_findings": [], "dispatch_id": f"codex-gate-pr{PR}"}
    commit_gate_outcome(GateOutcome.from_record("codex_gate", record, pr_id=""), results_dir=env["results_dir"])
    return record


def _annotate(env, payload=None):
    manager = _manager()
    payload = payload if payload is not None else {"gate": "codex_gate", "status": "requested", "pr_number": PR}
    return manager._stamp_takeover_annotations(
        payload, pr_number=PR, takeover_from="claude_gate", failure_reason="lane_exhausted: weekly limit",
        takeover_reason="lane_exhausted", takeover_source_status="unavailable",
    )


def _seed_request(env, **fields):
    payload = {"gate": "codex_gate", "status": "requested", "pr_number": PR, "branch": BRANCH,
               "commit_sha": HEAD, **fields}
    gate_recorder.persist_request(env["requests_dir"], "codex_gate", payload, pr_number=PR, pr_id="")
    return payload


def test_r11_an_annotation_keeps_the_one_record_and_its_one_receipt(env):
    _seed_decided(env)
    _seed_request(env)
    returned = _annotate(env)
    on_disk = _json(env["results_dir"] / f"pr-{PR}-codex_gate.json")
    assert (on_disk["status"], on_disk["attempt_seq"]) == ("pass", 1), "an annotation decides no status and no attempt"
    assert on_disk["takeover"] is True and on_disk["failure_reason"] == "lane_exhausted: weekly limit"
    assert on_disk["takeover_path"][0]["gate"] == "claude_gate"
    assert returned["takeover_from"] == "claude_gate"
    receipts = _result_receipts(env)
    assert len(receipts) == 1 and receipts[0]["attempt_seq"] == 1, "one record, at most one receipt, no second line"
    request = _json(_pr_slot(env, "codex_gate"))
    assert (request["status"], request["takeover"], request["failure_reason"]) == (
        "requested", True, "lane_exhausted: weekly limit")


def test_r11_an_unreadable_record_is_never_annotated_and_books_nothing(env):
    slot = env["results_dir"] / f"pr-{PR}-codex_gate.json"
    slot.write_text('{"status": "pa', encoding="utf-8")
    _seed_request(env)
    _annotate(env)
    assert slot.read_text(encoding="utf-8") == '{"status": "pa', "an unreadable record is never overwritten"
    assert _result_receipts(env) == [], "a refused annotation books nothing"


def test_r11_an_annotation_with_no_record_on_disk_invents_none(env):
    _seed_request(env)
    _annotate(env)
    assert not (env["results_dir"] / f"pr-{PR}-codex_gate.json").exists(), "no record, none invented"
    assert _result_receipts(env) == []
    assert _json(_pr_slot(env, "codex_gate"))["takeover"] is True


# ---------------------------------------------------------------------------
# Q5-Q7: one request writer, atomic under the request path's slot lock
# ---------------------------------------------------------------------------


def _request_codex(env):
    manager = _manager()
    with mock.patch.dict(os.environ, {"VNX_CODEX_HEADLESS_ENABLED": "0"}):
        return manager._request_codex(PR, BRANCH, "medium", ["a.py"], "per_pr", "dlv-q6")


def _request_seat_reader(env):
    from gate_request_handler import _PrHead

    manager = _manager()
    with mock.patch("review_gate_manager.shutil.which", side_effect=lambda t: "/usr/bin/fake" if t == "codex" else None), \
            mock.patch.dict(os.environ, {"VNX_CODEX_HEADLESS_ENABLED": "1"}):
        return manager._request_seat_reader(
            "codex_gate", None, [], pr_number=PR, branch=BRANCH, risk_class="medium", changed_files=["a.py"],
            mode="per_pr", dispatch_id="dlv-q6", head=_PrHead(PR, HEAD), seat_fields={"seat": "codex_gate"},
        )


def _book_switched_off_seat(env):
    from gate_request_handler import _PrHead
    from provider_switch import SwitchDecision

    manager = _manager()
    decision = SwitchDecision(identifier="kimi", allowed=False, family="kimi", source="operator",
                              scope="global", consumer="review_gate_request")
    return manager._book_switched_off_seat(
        "kimi_gate", decision, pr_number=PR, branch=BRANCH, risk_class="medium", changed_files=["a.py"],
        mode="per_pr", dispatch_id="dlv-q6", head=_PrHead(PR, HEAD), seat_fields={"seat": "kimi_gate"},
    )


def _gemini_contract_request(env):
    manager = _manager()
    with mock.patch("gate_request_handler.render_gemini_prompt", lambda _c: "rendered prompt"), \
            mock.patch.dict(os.environ, {"VNX_GEMINI_REVIEW_ENABLED": "0"}):
        manager.request_gemini_with_contract(contract=_contract(), dispatch_id="dlv-q7")
    return _request_slot(env, "gemini_review")


def _claude_github_contract_request(env):
    manager = _manager()
    with mock.patch.dict(os.environ, {"VNX_CLAUDE_GITHUB_REVIEW_ENABLED": "0"}):
        manager.request_claude_github_with_contract(contract=_contract(), dispatch_id="dlv-q7")
    return _request_slot(env)


def _ci_contract_request(env):
    manager = _manager()
    with mock.patch.dict(os.environ, {"VNX_CI_GATE_REQUIRED": "0"}):
        manager.request_ci_gate_with_contract(contract=_contract(), pr_number=PR, dispatch_id="dlv-q7")
    return _request_slot(env, "ci_gate")


WRITERS = {
    "Q5-annotation": (lambda env: _seed_request(env) and _annotate(env), lambda env: _pr_slot(env, "codex_gate")),
    "Q6-claude_github": (lambda env: _request_claude_github_request(env), lambda env: _pr_slot(env, "claude_github_optional")),
    "Q6-codex": (_request_codex, lambda env: _pr_slot(env, "codex_gate")),
    "Q6-seat_reader": (_request_seat_reader, lambda env: _pr_slot(env, "codex_gate")),
    "Q6-switched_off_seat": (_book_switched_off_seat, lambda env: _pr_slot(env, "kimi_gate")),
    "Q7-gemini_contract": (_gemini_contract_request, lambda env: _request_slot(env, "gemini_review")),
    "Q7-claude_github_contract": (_claude_github_contract_request, lambda env: _request_slot(env)),
    "Q7-ci_contract": (_ci_contract_request, lambda env: _request_slot(env, "ci_gate")),
}


def _request_claude_github_request(env):
    manager = _manager()
    with mock.patch.dict(os.environ, {"VNX_CLAUDE_GITHUB_REVIEW_ENABLED": "0"}):
        return manager._request_claude_github(PR, BRANCH, "medium", ["a.py"], "per_pr", "dlv-q6")


@pytest.mark.parametrize("row", sorted(WRITERS))
def test_a_request_write_waits_for_the_request_slot(env, row):
    write, slot = WRITERS[row]
    path, started = slot(env), threading.Event()

    def run():
        started.set()
        write(env)

    with slot_lock(path):
        thread = threading.Thread(target=run)
        thread.start()
        started.wait(5)
        thread.join(0.5)
        assert thread.is_alive(), f"{row}: the loser waits for the request slot"
        assert not path.exists() or "takeover" in _json(path) and row.startswith("Q5"), (
            f"{row}: nothing lands while another writer holds the request slot"
        )
    thread.join(10)
    assert "gate" in _json(path)


@pytest.mark.parametrize("row", sorted(WRITERS))
def test_no_request_write_uses_a_bare_write_text(env, monkeypatch, row):
    write, slot = WRITERS[row]
    real = Path.write_text

    def guarded(self, *args, **kwargs):
        assert self.parent != env["requests_dir"], f"{row}: {self.name} was written outside the request helper"
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded)
    write(env)
    assert _json(slot(env))["gate"]


def _request_codex_available(env):
    manager = _manager()
    with mock.patch("review_gate_manager.shutil.which", side_effect=lambda t: "/usr/bin/fake" if t == "codex" else None), \
            mock.patch.dict(os.environ, {"VNX_CODEX_HEADLESS_ENABLED": "1"}):
        return manager._request_codex(PR, BRANCH, "medium", ["a.py"], "per_pr", "dlv-q7")


def _gemini_contract_request_available(env):
    manager = _manager()
    with mock.patch("gate_request_handler.render_gemini_prompt", lambda _c: "rendered prompt"), \
            mock.patch("review_gate_manager.shutil.which", side_effect=lambda t: "/usr/bin/fake" if t == "gemini" else None), \
            mock.patch.dict(os.environ, {"VNX_GEMINI_REVIEW_ENABLED": "1"}):
        manager.request_gemini_with_contract(contract=_contract(), dispatch_id="dlv-q7")
    return _request_slot(env, "gemini_review")


PARTIAL = {
    "Q6-codex": (_request_codex_available, lambda env: _pr_slot(env, "codex_gate")),
    "Q7-gemini_contract": (_gemini_contract_request_available, lambda env: _request_slot(env, "gemini_review")),
}


@pytest.mark.parametrize("row", sorted(PARTIAL))
def test_a_failed_request_write_leaves_the_previous_record_whole(env, monkeypatch, row):
    write, slot = PARTIAL[row]
    path = slot(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = {"gate": "codex_gate", "status": "requested", "marker": "previous"}
    path.write_text(json.dumps(previous), encoding="utf-8")

    def dying_replace(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", dying_replace)
    with pytest.raises(OSError):
        write(env)
    assert _json(path) == previous, "a write that cannot land leaves the record that was there"
    assert list(path.parent.glob("*.tmp")) == [], "a died write leaves no scratch file behind"


def test_two_threads_on_one_request_path_never_interleave(env):
    """The reason the lock is there: a read-modify-write by one writer must not be split by another."""
    path, seen = _pr_slot(env, "claude_github_optional"), []
    manager = _manager()

    def slow_write():
        with slot_lock(path):
            seen.append("first holds")
            path.write_text(json.dumps({"gate": "claude_github_optional", "status": "first"}), encoding="utf-8")

    with mock.patch.dict(os.environ, {"VNX_CLAUDE_GITHUB_REVIEW_ENABLED": "0"}):
        thread = threading.Thread(target=slow_write)
        thread.start()
        thread.join(5)
        manager._request_claude_github(PR, BRANCH, "medium", ["a.py"], "per_pr", "dlv-q6")
    assert seen == ["first holds"]
    assert _json(path)["status"] == "not_configured", "the second writer lands after the first, whole"
