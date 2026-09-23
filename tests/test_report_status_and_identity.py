"""A dispatch that delivered is not ``unknown``, and its identity is one identity.

Measured 2026-09-23 on ``~/.vnx-data/vnx-dev/state/t0_receipts.ndjson``: of the
dispatch receipts that carry a ``cqs`` field, all but two were excluded from the
quality score on ``status=unknown``. The report contract asks for four sections
and a dispatch id; it does not ask for a status, so a report can satisfy all of
it and declare none. ``report_parser.py`` then stamped ``unknown`` and the
converter ``no_signal``, both of which the score excludes.

The same reports showed two more defects in how the identity block is read:

* ``Dispatch-ID: **x**`` is read as a dispatch named ``**x**``: a phantom twin
  beside the real dispatch (2 of 5447 receipts). The parser must hand back ``x``
  whichever of the three shapes a worker writes.
* An identity block that CLOSES the report was invisible: the converter scans
  the first 3000 characters and booked such a report
  ``report_contract_invalid`` / ``missing_content_dispatch_id``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import append_receipt  # registers the facade
import quality_db_init
import report_parser
import report_to_receipt_converter as rtc
from report_body_contract import (
    IDENTITY_WINDOW,
    clean_identity_value,
    identity_windows,
    resolve_undeclared_status,
)

DISPATCH_ID = "20260923-status-identity"

# The body the contract asks for: four sections, a Summary of at least 50
# non-whitespace characters. No status anywhere.
CONTRACT_BODY = (
    "## Summary\n\n"
    "De converter en de parser lezen het identiteitsblok nu op elke plek en in elke vorm. "
    "Het rapport voldoet aan het contract.\n\n"
    "## Changes\n\n- scripts/lib/example.py: aangepast\n\n"
    "## Verification\n\npytest tests/test_example.py: 3 passed\n\n"
    "## Open Items\n\nNone\n"
)

# The three shapes workers write the identity block in (measured on one day of
# reports from one model: 5x, 2x, 2x).
IDENTITY_FORMS = {
    "bold-then-colon": (
        f"**Dispatch-ID**: {DISPATCH_ID}\n**Model**: sonnet\n**Provider**: claude\n"
    ),
    "plain-key-bold-value": (
        f"Dispatch-ID: **{DISPATCH_ID}**\nModel: **sonnet**\nProvider: **claude**\n"
    ),
    "colon-inside-bold": (
        f"**Dispatch-ID:** {DISPATCH_ID}\n**Model:** sonnet\n**Provider:** claude\n"
    ),
}


def _long_filler(chars: int = 4000) -> str:
    """Prose long enough to push everything after it out of the head window."""
    paragraph = "Dit is een regel proza zonder identiteit of status erin.\n"
    return paragraph * (chars // len(paragraph) + 1)


def _report_with_identity_on_top(form: str) -> str:
    return IDENTITY_FORMS[form] + "\n" + CONTRACT_BODY


def _report_with_identity_at_the_bottom(form: str) -> str:
    return CONTRACT_BODY + "\n" + _long_filler() + "\n" + IDENTITY_FORMS[form]


# ---------------------------------------------------------------------------
# The helpers themselves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, cleaned",
    [
        ("20260922-x", "20260922-x"),
        ("**20260922-x**", "20260922-x"),
        ("  **glm-5.2**  ", "glm-5.2"),
        ("20260922-x:", "20260922-x"),
        (":**x**:", "x"),
        (None, ""),
        ("", ""),
        ("**", ""),
        # A backtick-wrapped model is refused on purpose by the parser's
        # plausibility guard (OI-1194); cleaning must not launder it.
        ("`sonnet`", "`sonnet`"),
    ],
)
def test_clean_identity_value(raw, cleaned):
    assert clean_identity_value(raw) == cleaned


def test_identity_windows_short_text_is_one_window():
    assert identity_windows("kort") == ["kort"]


def test_identity_windows_long_text_is_head_then_tail():
    text = "a" * 5000 + "\n" + ("b" * 99 + "\n") * 60
    head, tail = identity_windows(text)
    assert head == text[:IDENTITY_WINDOW]
    assert tail.endswith("b" * 99 + "\n")
    assert len(tail) <= IDENTITY_WINDOW


def test_identity_tail_never_opens_mid_line():
    """A window that starts inside a long line must not offer that line's tail
    as a line of its own."""
    fake = "Dispatch-ID: not-the-dispatch"
    prefix = "x" * 4000
    suffix = "y" * (IDENTITY_WINDOW - len(fake))
    text = prefix + fake + suffix  # the tail window opens exactly at ``fake``
    assert text[-IDENTITY_WINDOW:].startswith(fake)

    _, tail = identity_windows(text)

    assert "Dispatch-ID" not in tail


@pytest.mark.parametrize("declared", [None, "", "unknown", "None", "n/a", "-", "  Unknown "])
def test_resolve_undeclared_status_derives_done_only_from_a_valid_body(declared):
    assert resolve_undeclared_status(declared, body_valid=True) == "done"
    assert resolve_undeclared_status(declared, body_valid=False) is None


@pytest.mark.parametrize("declared", ["success", "failed", "blocked", "partial", "in_progress"])
def test_resolve_undeclared_status_never_overrides_a_declared_status(declared):
    assert resolve_undeclared_status(declared, body_valid=True) is None


# ---------------------------------------------------------------------------
# PUNT 2: the three shapes, in both readers
# ---------------------------------------------------------------------------

# The converter has no plain-text ``Model:`` / ``Provider:`` reader (bold fields
# only; the lane's route decision is its primary source for both). Only the
# Dispatch-ID has a plain-text form there, so model and provider are asserted
# for the two bold shapes.
_BOLD_IDENTITY_FORMS = ("bold-then-colon", "colon-inside-bold")


@pytest.mark.parametrize("form", IDENTITY_FORMS)
def test_converter_reads_every_identity_shape_clean(form):
    fields = rtc._extract_body_fields(_report_with_identity_on_top(form))

    assert fields.get("dispatch_id") == DISPATCH_ID
    if form in _BOLD_IDENTITY_FORMS:
        assert fields.get("model") == "sonnet"
        assert fields.get("provider") == "claude"


@pytest.mark.parametrize("form", IDENTITY_FORMS)
def test_parser_reads_every_identity_shape_clean(form):
    parser = report_parser.ReportParser()

    metadata = parser.extract_metadata(_report_with_identity_on_top(form))

    assert metadata.get("dispatch_id") == DISPATCH_ID
    assert metadata.get("model") == "sonnet"
    assert metadata.get("provider") == "claude"


@pytest.mark.parametrize("form", IDENTITY_FORMS)
def test_no_phantom_dispatch_identity_reaches_the_receipt(form, tmp_path):
    """The receipt's dispatch_id carries no markdown, from either writer."""
    path = tmp_path / f"{DISPATCH_ID}.md"
    text = _report_with_identity_on_top(form)
    path.write_text(text, encoding="utf-8")

    parsed = report_parser.ReportParser().parse_report(str(path))
    converted = rtc.build_receipt_from_report(path, text)

    assert parsed.get("dispatch_id") == DISPATCH_ID
    assert converted is not None
    assert converted.get("dispatch_id") == DISPATCH_ID
    assert converted["event_type"] != "report_contract_invalid", converted.get("contract_violations")
    assert parsed.get("model") == "sonnet"
    assert parsed.get("provider") == "claude"


def test_a_lowercase_plain_model_line_is_still_not_an_identity_stamp():
    """``model:`` in lowercase is the frontmatter form; the plain-text header
    fallback stays case-sensitive so prose is never adopted."""
    parser = report_parser.ReportParser()

    metadata = parser.extract_metadata(
        f"**Dispatch-ID**: {DISPATCH_ID}\n\nmodel: gpt-of-nonsense\n\n" + CONTRACT_BODY
    )

    assert metadata.get("model") != "gpt-of-nonsense"


def test_a_quoted_diff_line_is_not_an_identity_stamp():
    parser = report_parser.ReportParser()
    diff = "```diff\n-    Dispatch-ID: 20200101-old-stale\n+    Dispatch-ID: 20200101-new\n```\n"

    metadata = parser.extract_metadata(
        f"**Dispatch-ID**: {DISPATCH_ID}\n\n" + CONTRACT_BODY + "\n" + diff
    )

    assert metadata.get("dispatch_id") == DISPATCH_ID


# ---------------------------------------------------------------------------
# Position: the identity block on top or at the bottom of the report
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("form", IDENTITY_FORMS)
@pytest.mark.parametrize("position", ["top", "bottom"])
def test_converter_finds_the_identity_block_on_top_and_at_the_bottom(form, position):
    text = (
        _report_with_identity_on_top(form)
        if position == "top"
        else _report_with_identity_at_the_bottom(form)
    )
    if position == "bottom":
        assert DISPATCH_ID not in text[:IDENTITY_WINDOW], "fixture must push the block out of the head"

    fields = rtc._extract_body_fields(text)

    assert fields.get("dispatch_id") == DISPATCH_ID
    if form in _BOLD_IDENTITY_FORMS:
        assert fields.get("model") == "sonnet"
        assert fields.get("provider") == "claude"


def test_a_report_closing_with_its_identity_block_is_not_contract_invalid(tmp_path):
    """The measured failure: identity block at the bottom read as
    ``missing_content_dispatch_id``."""
    path = tmp_path / f"{DISPATCH_ID}.md"
    text = _report_with_identity_at_the_bottom("bold-then-colon")
    path.write_text(text, encoding="utf-8")

    receipt = rtc.build_receipt_from_report(path, text)

    assert receipt is not None
    assert receipt["event_type"] == "task_complete", receipt.get("contract_violations")
    assert receipt["dispatch_id"] == DISPATCH_ID


def test_the_head_wins_over_the_tail():
    text = (
        "**Dispatch-ID**: 20260923-the-head\n\n" + CONTRACT_BODY + "\n" + _long_filler()
        + "\n**Dispatch-ID**: 20260923-the-tail\n"
    )

    assert rtc._extract_body_fields(text)["dispatch_id"] == "20260923-the-head"


def test_an_id_quoted_in_the_middle_of_a_long_report_is_not_adopted():
    """No stamp on top, none at the bottom, a foreign id quoted in the body:
    the report has no identity, and must not borrow the quoted one."""
    text = (
        CONTRACT_BODY + "\n" + _long_filler(3500)
        + "\n**Dispatch-ID**: 20260101-quoted-in-prose\n\n" + _long_filler(6000)
    )
    assert "quoted-in-prose" not in text[:IDENTITY_WINDOW]
    assert "quoted-in-prose" not in text[-IDENTITY_WINDOW:]

    assert "dispatch_id" not in rtc._extract_body_fields(text)


def test_only_identity_keys_are_read_from_the_tail():
    """A status stamped at the bottom is not read: only the identity block is."""
    text = CONTRACT_BODY + "\n" + _long_filler() + "\n**Status**: failed\n**Model**: sonnet\n"

    fields = rtc._extract_body_fields(text)

    assert "status" not in fields
    assert fields.get("model") == "sonnet"


@pytest.mark.parametrize("form", IDENTITY_FORMS)
def test_parser_finds_the_identity_block_at_the_bottom(form):
    parser = report_parser.ReportParser()
    # No filename fallback: the id has to come out of the report itself.
    parser._current_filename = None

    metadata = parser.extract_metadata(_report_with_identity_at_the_bottom(form))

    assert metadata.get("dispatch_id") == DISPATCH_ID
    assert metadata.get("model") == "sonnet"


# ---------------------------------------------------------------------------
# PUNT 1: a report that satisfies the contract and declares no status
# ---------------------------------------------------------------------------

def _state_with_quality_db(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    assert quality_db_init.bootstrap_qi_db(state / "quality_intelligence.db")
    return state


def _book_through_the_parser(tmp_path: Path, monkeypatch, text: str, dispatch_id: str) -> dict:
    """Report on disk -> report_parser -> append_receipt (with enrichment),
    exactly the receipt processor's route. Returns the line that hit the ledger."""
    state = _state_with_quality_db(tmp_path, monkeypatch)
    path = tmp_path / f"{dispatch_id}.md"
    path.write_text(text, encoding="utf-8")
    receipt = report_parser.ReportParser().parse_report(str(path))
    assert "error" not in receipt, receipt

    result = append_receipt.append_receipt_payload(
        receipt, receipts_file=str(state / "t0_receipts.ndjson"),
    )

    assert result.status == "appended"
    return json.loads((state / "t0_receipts.ndjson").read_text().strip().splitlines()[-1])


def test_a_contract_valid_report_without_status_gets_a_quality_score(tmp_path, monkeypatch):
    booked = _book_through_the_parser(
        tmp_path, monkeypatch,
        _report_with_identity_on_top("bold-then-colon"), DISPATCH_ID,
    )

    assert "excluded_reason" not in booked["cqs"]["components"], booked["cqs"]
    assert isinstance(booked["cqs"]["cqs"], float)
    assert booked["cqs"]["normalized_status"] == "success"
    assert booked["status"] == "done"
    assert booked["status_source"] == "report_contract"


def test_a_report_that_fails_the_contract_gets_no_derived_status(tmp_path, monkeypatch):
    """The control: without evidence there is no status, and the score stays
    excluded. The fix does not mint a success out of nothing."""
    broken = IDENTITY_FORMS["bold-then-colon"] + "\n## Summary\n\nKort.\n"

    booked = _book_through_the_parser(tmp_path, monkeypatch, broken, "20260923-no-evidence")

    assert booked["status"] == "unknown"
    assert "status_source" not in booked
    assert booked["cqs"]["cqs"] is None
    assert "excluded_reason" in booked["cqs"]["components"]


@pytest.mark.parametrize("declared, normalized", [("failed", "failure"), ("partial", "partial")])
def test_a_declared_status_is_kept_and_scored(tmp_path, monkeypatch, declared, normalized):
    text = IDENTITY_FORMS["bold-then-colon"] + f"**Status**: {declared}\n\n" + CONTRACT_BODY

    booked = _book_through_the_parser(
        tmp_path, monkeypatch, text, f"20260923-declared-{declared}",
    )

    assert booked["status"] == declared
    assert "status_source" not in booked
    assert booked["cqs"]["normalized_status"] == normalized
    assert booked["cqs"]["cqs"] is not None


def test_no_status_is_minted_when_the_contract_validator_could_not_run():
    """``_body_contract_valid`` defaults to True when validate_body is
    unavailable; only a validator verdict counts as evidence."""
    parser = report_parser.ReportParser()
    extracted = {
        "metadata": {"status": "unknown", "dispatch_id": DISPATCH_ID, "terminal": "T1"},
        "recommendations": {},
        "validation": {},
        "intelligence": {},
        "_body_contract_valid": True,
        "_body_contract_evaluated": False,
    }

    receipt = parser._build_enhanced_receipt(extracted, str(ROOT / "README.md"))

    assert receipt["status"] == "unknown"
    assert "status_source" not in receipt


# ---------------------------------------------------------------------------
# PUNT 1, converter side
# ---------------------------------------------------------------------------

def test_converter_derives_done_for_a_contract_valid_report_without_status(tmp_path):
    path = tmp_path / f"{DISPATCH_ID}.md"
    text = _report_with_identity_on_top("bold-then-colon")
    path.write_text(text, encoding="utf-8")

    receipt = rtc.build_receipt_from_report(path, text)

    assert receipt is not None
    assert receipt["event_type"] == "task_complete"
    assert receipt["status"] == "done"
    assert receipt["status_source"] == "report_contract"


def test_converter_derived_done_does_not_run_the_success_claim_checks(tmp_path, monkeypatch):
    """A derived status is not a success CLAIM: nothing was asserted for the
    delivery check (branch on origin, verified PR) to refute."""
    path = tmp_path / f"{DISPATCH_ID}.md"
    text = _report_with_identity_on_top("bold-then-colon")
    path.write_text(text, encoding="utf-8")
    calls = []
    monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda did: calls.append(did) or False)

    receipt = rtc.build_receipt_from_report(path, text)

    assert calls == []
    assert receipt["status"] == "done"


def test_converter_declared_success_still_runs_the_claim_checks(tmp_path, monkeypatch):
    path = tmp_path / f"{DISPATCH_ID}.md"
    text = IDENTITY_FORMS["bold-then-colon"] + "**Status**: success\n\n" + CONTRACT_BODY
    path.write_text(text, encoding="utf-8")
    calls = []
    monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda did: calls.append(did) or False)

    receipt = rtc.build_receipt_from_report(path, text)

    assert calls == [DISPATCH_ID]
    assert receipt["event_type"] == "task_failed"
    assert "status_source" not in receipt


def test_converter_without_evidence_derives_nothing(tmp_path):
    path = tmp_path / f"{DISPATCH_ID}.md"
    text = IDENTITY_FORMS["bold-then-colon"] + "\n## Summary\n\nKort.\n"
    path.write_text(text, encoding="utf-8")

    receipt = rtc.build_receipt_from_report(path, text)

    assert receipt["event_type"] == "report_contract_invalid"
    assert "status_source" not in receipt


def test_converter_exit_code_still_outranks_the_contract(tmp_path):
    path = tmp_path / f"{DISPATCH_ID}.md"
    text = (
        "---\ndispatch_id: " + DISPATCH_ID + "\nmodel: sonnet\nprovider: claude\nexit_code: 1\n---\n\n"
        + CONTRACT_BODY
    )
    path.write_text(text, encoding="utf-8")

    receipt = rtc.build_receipt_from_report(path, text)

    assert receipt["event_type"] == "task_failed"
    assert receipt["status"] == "failed"


# ---------------------------------------------------------------------------
# The real shapes measured on 22-09 (one worker, one model, three writings)
# ---------------------------------------------------------------------------

def test_the_three_real_report_openings_of_22_09(tmp_path):
    """Each opening is copied from a report of that day; all three must resolve
    to a real dispatch identity with no markdown in it."""
    openings = {
        "20260922-jev-arm-live-openrouter":
            "Dispatch-ID: **20260922-jev-arm-live-openrouter**\n**Model**: glm-5.2\n**Provider**: glm-harness\n",
        "20260922-oi1546-lane-identiteit":
            "**Dispatch-ID**: 20260922-oi1546-lane-identiteit\n**Model**: glm-5.2\n**Provider**: glm-harness\n",
        "20260922-laya-mlx-1024-herkansing":
            "# Dispatch rapport\n\n**Dispatch-ID:** 20260922-laya-mlx-1024-herkansing\n"
            "**Model:** glm-5.2\n**Provider:** glm-harness\n",
    }
    for dispatch_id, opening in openings.items():
        text = opening + "\n" + CONTRACT_BODY
        path = tmp_path / f"{dispatch_id}.md"
        path.write_text(text, encoding="utf-8")

        converted = rtc.build_receipt_from_report(path, text)
        parsed = report_parser.ReportParser().parse_report(str(path))

        assert converted["dispatch_id"] == dispatch_id
        assert converted["event_type"] == "task_complete"
        assert converted["status"] == "done"
        assert parsed["dispatch_id"] == dispatch_id
        assert parsed["status"] == "done"
        assert parsed["model"] == "glm-5.2"
        assert parsed["provider"] == "glm-harness"
