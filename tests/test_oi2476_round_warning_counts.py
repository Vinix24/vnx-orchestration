"""OI-2476 (track tweede-stoel-op-signaal, PR 1): warnings per verdict in the round ledger.

Plan: ``claudedocs/specs/tweede-stoel-op-signaal/plan.md``, Deliverable 1 and
the input-contract table. Measurement:
``claudedocs/2026-10-30-tweede-stoel-op-waarschuwing.md``.

Every verdict line in ``rounds.ndjson`` and every ``review_gate_result``
receipt carries the counts of that verdict's own report:

* ``warning_count`` -- the advisory findings under ``### Advisory`` that are
  not pre-scan notices;
* ``prescan_notice_count`` -- the deterministic pre-scan's findings
  (``instruction-like text in diff``), which the measurement counts as noise.

Unknown is never zero. An absent field (an old line, a missing/unreadable
report, a takeover) reads as unknown; zero is only booked when the report
really lists no advisory findings. Nothing here changes a merge decision.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry

SECOND_HEAD = "1" * 40
PRESCAN = "instruction-like text in diff at line 7 (pattern: ignore-previous)"


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = build_project_store(tmp_path, monkeypatch)
    import gate_recorder
    import review_gate_manager

    receipts: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        review_gate_manager, "emit_governance_receipt",
        lambda event, **fields: receipts.append({"event": event, **fields}),
    )
    monkeypatch.setattr(
        gate_recorder, "emit_governance_receipt",
        lambda event, **fields: receipts.append({"event": event, **fields}),
    )
    store["receipts"] = receipts
    store["ledger"] = store["state_dir"] / "review_gates" / "rounds.ndjson"
    return store


# ---------------------------------------------------------------------------
# Helpers: reports, payloads, the production writers
# ---------------------------------------------------------------------------


def _report(store, messages: List[str], name: str) -> Path:
    """A codex-style headless report: the normalized ``## Findings`` block gate_artifacts writes."""
    lines = [
        "# codex_gate — Headless Gate Report", "", "## Findings", "",
        "Blocking count: 0", f"Advisory count: {len(messages)}",
    ]
    if messages:
        lines += ["", "### Advisory", ""]
        lines += [f"- {message} (severity: warning)" for message in messages]
    lines += ["", "## Gate Output", "", "codex_gate review — nothing else.", ""]
    path = store["reports_dir"] / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _payload(store, gate: str, report_path: Any, *, head: str = HEAD_SHA, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "gate": gate, "pr_id": str(PR), "pr_number": PR, "status": "completed",
        "summary": f"{gate} execution completed successfully", "contract_hash": f"{gate}-hash",
        "report_path": str(report_path), "findings": [],
        "blocking_findings": [], "advisory_findings": [],
        "commit_sha": head, "branch": BRANCH, "dispatch_id": f"d-{gate}",
        "recorded_at": "2026-10-30T10:00:00Z",
    }
    payload.update(extra)
    return payload


def _write(store, gate: str, payload: Dict[str, Any]):
    """Write through the production result writer: books the ledger line, the record and the receipt."""
    import gate_recorder

    return gate_recorder.write_result_guarded(
        store["results_dir"] / f"pr-{PR}-{gate}.json", payload, gate=gate, pr_ref=str(PR),
    )


def _lines(store) -> List[Dict[str, Any]]:
    path = store["ledger"]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _line(store, seat: str = "codex_gate") -> Dict[str, Any]:
    matches = [line for line in _lines(store) if line.get("seat") == seat]
    assert len(matches) == 1, f"expected one {seat} line, got {matches!r}"
    return matches[0]


def _result_receipt(store, gate: str = "codex_gate") -> Dict[str, Any]:
    matches = [
        receipt for receipt in store["receipts"]
        if receipt.get("event") == "review_gate_result" and receipt.get("gate") == gate
    ]
    assert len(matches) == 1, f"expected one {gate} result receipt, got {matches!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# The counts, in the ledger line AND in the receipt
# ---------------------------------------------------------------------------


def test_two_real_warnings_and_one_prescan_notice_book_2_and_1(env):
    report = _report(env, [
        "Phase range does not cover the work it lists.",
        "A release claim is not an ancestor of this tag.",
        PRESCAN,
    ], "codex-two-warnings.md")

    _write(env, "codex_gate", _payload(env, "codex_gate", report))

    line = _line(env)
    receipt = _result_receipt(env)
    assert (line["warning_count"], line["prescan_notice_count"]) == (2, 1), line
    assert (receipt["warning_count"], receipt["prescan_notice_count"]) == (2, 1), receipt


def test_the_codex_commit_path_books_the_same_counts(env):
    """codex_gate writes through ``commit_gate_outcome`` (gate_artifacts), not ``write_result_guarded``."""
    import gate_outcome

    report = _report(env, ["a real warning", PRESCAN], "codex-commit-path.md")
    payload = _payload(env, "codex_gate", report)
    outcome = gate_outcome.GateOutcome.from_record(
        "codex_gate", payload, pr_id=str(PR),
        receipt_fields={"blocking_findings": [], "advisory_findings": [], "advisory_count": 2},
    )
    gate_outcome.commit_gate_outcome(
        outcome, results_dir=env["results_dir"],
        result_path=env["results_dir"] / f"pr-{PR}-codex_gate.json",
    )

    line = _line(env)
    receipt = _result_receipt(env)
    assert (line["warning_count"], line["prescan_notice_count"]) == (1, 1), line
    assert (receipt["warning_count"], receipt["prescan_notice_count"]) == (1, 1), receipt


def test_a_verdict_with_no_warnings_books_zero_and_zero_not_unknown(env):
    report = _report(env, [], "codex-clean.md")

    _write(env, "codex_gate", _payload(env, "codex_gate", report))

    line = _line(env)
    receipt = _result_receipt(env)
    assert line.get("warning_count") == 0, "zero must be booked, not omitted as unknown"
    assert line.get("prescan_notice_count") == 0
    assert receipt.get("warning_count") == 0
    assert receipt.get("prescan_notice_count") == 0


# ---------------------------------------------------------------------------
# Unknown is never zero
# ---------------------------------------------------------------------------


def test_a_missing_report_books_unknown(env):
    missing = env["reports_dir"] / "does-not-exist.md"

    _write(env, "codex_gate", _payload(env, "codex_gate", missing))

    line = _line(env)
    receipt = _result_receipt(env)
    assert "warning_count" not in line, line
    assert "prescan_notice_count" not in line
    assert "warning_count" not in receipt, receipt
    assert "prescan_notice_count" not in receipt


def test_an_unreadable_report_books_unknown(env):
    unreadable = env["reports_dir"] / "a-directory.md"
    unreadable.mkdir()

    _write(env, "codex_gate", _payload(env, "codex_gate", unreadable))

    line = _line(env)
    receipt = _result_receipt(env)
    assert "warning_count" not in line, line
    assert "warning_count" not in receipt, receipt


def test_a_verdict_without_a_report_is_unknown():
    """A verdict that names no report has nothing to read: unknown, not zero.

    (Such a record is not a ledger verdict in the first place -- the ledger
    needs a populated ``report_path`` as evidence -- so this is pinned on the
    reader the writers share, not on a line that would never be booked.)
    """
    from review_report_counts import counts_for_payload

    assert counts_for_payload({"gate": "codex_gate", "report_path": ""}) is None
    assert counts_for_payload({"gate": "codex_gate"}) is None


def test_a_takeover_verdict_books_unknown(env):
    """The plan sends a takeover of seat 1 to unknown: the door counts a verdict
    a walk filled, and its report is not the seat's own."""
    report = _report(env, ["A real warning."], "codex-takeover.md")

    _write(env, "deepseek_gate", _payload(
        env, "deepseek_gate", report,
        seat="codex_gate", takeover=True, takeover_from="codex_gate",
    ))

    line = _line(env, "codex_gate")
    receipt = _result_receipt(env, "deepseek_gate")
    assert "warning_count" not in line, line
    assert "warning_count" not in receipt, receipt


# ---------------------------------------------------------------------------
# Old lines keep reading, and each verdict keeps its own numbers
# ---------------------------------------------------------------------------


def test_an_old_ledger_line_without_the_fields_reads_as_unknown():
    import review_rounds

    old_line = {
        "project_id": "", "pr_number": PR, "head_sha": HEAD_SHA, "seat": "codex_gate",
        "reviewer": "codex_gate", "takeover_from": "", "verdict": "pass", "blocking_count": 0,
        "report_path": "/r/old-report.md", "dispatch_id": "d-old",
        "recorded_at": "2026-10-01T00:00:00Z", "branch": BRANCH, "source": "gate",
    }

    line = review_rounds.RoundLine.from_dict(old_line)

    assert line.warning_count is None
    assert line.prescan_notice_count is None
    assert "warning_count" not in line.to_dict(), "unknown is not written back as a value"


def test_two_verdicts_on_the_same_pr_keep_their_own_counts(env):
    first = _report(env, ["one real warning", "another real warning"], "codex-head-a.md")
    second = _report(env, [], "codex-head-b.md")

    _write(env, "codex_gate", _payload(env, "codex_gate", first, head=HEAD_SHA))
    _write(env, "codex_gate", _payload(env, "codex_gate", second, head=SECOND_HEAD))

    by_head = {line["head_sha"]: line for line in _lines(env) if line.get("seat") == "codex_gate"}
    assert by_head[HEAD_SHA]["warning_count"] == 2, by_head
    assert by_head[SECOND_HEAD]["warning_count"] == 0, "the second verdict must not inherit the first's numbers"
    assert by_head[SECOND_HEAD]["prescan_notice_count"] == 0


# ---------------------------------------------------------------------------
# The reader itself: scoped to ## Findings, and fail-closed on inconsistency
# ---------------------------------------------------------------------------


def test_the_prescan_prefix_matches_the_gate_prompt_owner():
    from gate_prompt import INSTRUCTION_FINDING_PREFIX
    from review_report_counts import PRESCAN_NOTICE_PREFIX

    assert PRESCAN_NOTICE_PREFIX == INSTRUCTION_FINDING_PREFIX


def test_counts_ignore_an_advisory_heading_quoted_in_the_raw_transcript():
    from review_report_counts import parse_report_counts

    text = (
        "## Findings\n\nBlocking count: 0\nAdvisory count: 0\n\n"
        "## Gate Output\n\nReviewed a report that says:\n\n### Advisory\n\n- not the reviewer's finding\n"
    )

    counts = parse_report_counts(text)

    assert counts is not None
    assert (counts.warnings, counts.notices) == (0, 0)


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "# a report without the normalized findings block\n",
        "## Findings\n\nBlocking count: 0\n\n### Advisory\n\n- a warning\n",
        "## Findings\n\nBlocking count: 0\nAdvisory count: 5\n\n### Advisory\n\n- only one (severity: warning)\n",
        "## Findings\n\nBlocking count: 0\nAdvisory count: two\n",
    ],
)
def test_an_unreadable_or_inconsistent_report_is_unknown(text):
    from review_report_counts import parse_report_counts

    assert parse_report_counts(text) is None


def test_the_reader_does_not_raise_on_an_unreadable_path(tmp_path):
    from review_report_counts import counts_from_report

    directory = tmp_path / "report.md"
    directory.mkdir()

    assert counts_from_report(directory) is None
    assert counts_from_report(tmp_path / "missing.md") is None
    assert counts_from_report("") is None


def test_a_malformed_count_on_an_old_line_is_no_line():
    import review_rounds

    old_line = {
        "project_id": "", "pr_number": PR, "head_sha": HEAD_SHA, "seat": "codex_gate",
        "reviewer": "codex_gate", "takeover_from": "", "verdict": "pass", "blocking_count": 0,
        "report_path": "/r/old-report.md", "dispatch_id": "d-old",
        "recorded_at": "2026-10-01T00:00:00Z", "branch": BRANCH, "source": "gate",
        "warning_count": -1,
    }

    with pytest.raises(ValueError):
        review_rounds.RoundLine.from_dict(old_line)
