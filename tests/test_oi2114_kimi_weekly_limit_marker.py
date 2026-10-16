"""kimi's weekly-limit refusal counts as an exhausted lane (OI-2114).

Measured 2026-10-16 10:46Z: the second review seat of PR #2074 (head
``46b52973``) could not be filled. kimi sits at its weekly limit, and its run
booked ``state/review_gates/results/pr-2074-kimi_gate.json`` under
``~/.vnx-data/vnx-dev`` (read-only here) as ``status=unavailable``,
``reason=harness_lane_no_model_response``, with kimi's own 403 in
``reason_detail``.

No marker in ``governance_emit._LANE_EXHAUSTED_MARKERS`` matched that text:
``reached your usage limit`` is kimi's 22-08 billing-cycle wording, and
``access_terminated_error`` was cut in two by kimi's line wrap (see
``_PR2073_KIMI_80_COLUMN_CHUNK`` below). So ``_classify_lane_log_text`` said
``unreadable_verdict``, the seat state was ``no_response``, and
``_candidate_hop`` let the record hold the seat: kimi was dispatched again on
every later request for that head.

The fixture is the record's ``status``, ``reason`` and ``reason_detail``,
verbatim, and nothing else. Gate, PR, head and ``recorded_at`` are this test's
own. No provider, no real store, no GitHub: the store is a ``tmp_path`` built
by ``review_stack_support`` and the PR head is fixed there.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import HEAD_SHA, PR, build_project_store, isolate_config_registry

from gate_request_handler import (
    GateRequestHandlerMixin,
    _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN,
    _PrHead,
    _hop_text,
    _parse_review_gate_takeover_chain,
    _scan_seat_failure_text,
)
from governance_emit import _classify_lane_log_text

# Verbatim from pr-2074-kimi_gate.json (status, reason, reason_detail only).
_PR2074_KIMI_WEEKLY_LIMIT = {
    "status": "unavailable",
    "reason": "harness_lane_no_model_response",
    "reason_detail": (
        "kimi_gate report carries failure_reason in its frontmatter (spawn layer failed before "
        "the model ran): [quota_or_auth] provider=kimi reason=quota_or_auth msg='Expecting value: "
        "line 1 column 1 (char 0)' raw='Error code: 403 - {\\'error\\': {\\'message\\': \"You\\'ve "
        "reached your weekly (7-day) usage limit. Your quota will reset when the current 7-day "
        "window ends'\n"
        "[quota_or_auth] provider=kimi reason=quota_or_auth msg='Expecting value: line 1 column 1 "
        "(char 0)' raw='. To continue now, purchase extra usage or upgrade your plan: "
        "https://www.kimi.com/membership/subscription?tab=quota\", \\'type\\': \\'access_terminated'\n"
        "Expecting value: line 1 column 1 (char 0)\n"
        "subprocess exited with code 1 before complete event"
    ),
}

# The same refusal wrapped at 80 columns: the second `raw=` chunk of
# pr-2073-kimi_gate.json (16-10 06:44Z), verbatim. At this width the first
# sentence breaks inside "usage" ("...weekly (7-day) usag" | "e limit."); the
# records of that width were caught only by "access_terminated_error" further
# on. Without that type label in the text, this chunk is what remains.
_PR2073_KIMI_80_COLUMN_CHUNK = (
    "[quota_or_auth] provider=kimi reason=quota_or_auth msg='Expecting value: line 1 column 1 "
    "(char 0)' raw='e limit. Your quota will reset when the current 7-day window ends. To continue n'"
)

_WEEKLY_SENTENCE = "reached your weekly (7-day) usage limit"
_RESET_SENTENCE = "your quota will reset when the current 7-day window ends"


def _stamp(age: timedelta) -> str:
    return (datetime.now(timezone.utc) - age).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kimi_record(*, age: timedelta = timedelta(minutes=2)) -> Dict[str, Any]:
    """The measured text on this test's PR and head, recorded ``age`` ago."""
    record = {"gate": "kimi_gate", "pr_id": str(PR), "pr_number": PR, "commit_sha": HEAD_SHA}
    record.update(_PR2074_KIMI_WEEKLY_LIMIT)
    record["recorded_at"] = _stamp(age)
    return record


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def store(tmp_path, monkeypatch):
    return build_project_store(tmp_path, monkeypatch)


def _manager():
    import review_gate_manager
    return review_gate_manager.ReviewGateManager()


def _write_kimi_result(store, record: Dict[str, Any]) -> None:
    path = store["results_dir"] / f"pr-{PR}-kimi_gate.json"
    path.write_text(json.dumps(record), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The classifier
# ---------------------------------------------------------------------------


class TestTheWeeklyRefusalIsLaneExhausted:

    def test_the_measured_record_text_is_lane_exhausted(self):
        state, snippet = _classify_lane_log_text(_PR2074_KIMI_WEEKLY_LIMIT["reason_detail"])

        assert state == "lane_exhausted"
        assert snippet is not None
        assert _WEEKLY_SENTENCE in snippet.lower(), (
            "the lifted reason must carry kimi's own words, so a reader of the hop "
            f"sees the weekly limit: {snippet!r}"
        )
        assert _RESET_SENTENCE in snippet.lower()

    def test_the_80_column_wrap_matches_without_the_type_label(self):
        """The marker is chosen to survive both measured wrap widths. Here the
        weekly sentence is broken and access_terminated_error is absent, so
        only the reset sentence can decide."""
        assert "access_terminated_error" not in _PR2073_KIMI_80_COLUMN_CHUNK
        assert _WEEKLY_SENTENCE not in _PR2073_KIMI_80_COLUMN_CHUNK.lower()

        state, _snippet = _classify_lane_log_text(_PR2073_KIMI_80_COLUMN_CHUNK)

        assert state == "lane_exhausted"

    @pytest.mark.parametrize("prose", [
        "kimi has a weekly (7-day) usage limit; once you've reached your weekly usage limit "
        "you wait for the quota to reset.",
        "The gate reached its usage limit for the week, so the review moved to deepseek.",
        "Rate limits are counted over a rolling 7-day window and reset when it ends.",
    ])
    def test_ordinary_prose_about_usage_limits_does_not_match(self, prose):
        """A model writing about usage limits is not a provider refusing a call."""
        state, snippet = _classify_lane_log_text(prose)

        assert (state, snippet) == ("unreadable_verdict", None)


# ---------------------------------------------------------------------------
# 2. The seat state, behind the recency gate
# ---------------------------------------------------------------------------


class TestTheSeatStateFollowsTheRecencyGate:

    def test_the_scan_of_the_record_fields_finds_the_exhaustion(self):
        state, detail = _scan_seat_failure_text(_kimi_record(), None)

        assert state == "lane_exhausted"
        assert _WEEKLY_SENTENCE in detail.lower()

    def test_a_fresh_record_is_lane_exhausted(self):
        state = GateRequestHandlerMixin._classify_review_seat_failure(None, _kimi_record())

        assert state == "lane_exhausted"

    def test_an_old_record_expires_exactly_as_before(self):
        state = GateRequestHandlerMixin._classify_review_seat_failure(
            None, _kimi_record(age=timedelta(hours=2)),
        )

        assert state == "lane_exhausted_expired"


# ---------------------------------------------------------------------------
# 3. The takeover walk passes kimi over on the PR's own head
# ---------------------------------------------------------------------------


class TestTheWalkPassesKimiOver:

    def test_candidate_hop_passes_kimi_over_with_its_own_words(self, store):
        _write_kimi_result(store, _kimi_record())

        hop, verdict_stands = _manager()._candidate_hop(
            "kimi_gate", None, PR, head=_PrHead(PR, HEAD_SHA), held={},
        )

        assert verdict_stands is False
        assert hop is not None, "kimi read its own seat again: the record held it (OI-2114)"
        assert hop["gate"] == "kimi_gate"
        assert hop["status"] == "unavailable"

    def test_the_walk_from_kimis_seat_reaches_deepseek(self, store):
        _write_kimi_result(store, _kimi_record())
        chain = _parse_review_gate_takeover_chain(_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN)
        assert chain["kimi_gate"] == "deepseek_gate"

        reader, path, refusal = _manager()._walk_takeover_chain(
            "kimi_gate", PR, chain, None, head=_PrHead(PR, HEAD_SHA),
        )

        assert reader == "deepseek_gate", f"kimi was dispatched again instead of passed over: {path!r}"
        assert refusal is None
        assert [hop["gate"] for hop in path] == ["kimi_gate"]
        hop = path[0]
        assert hop["reason"] == "harness_lane_no_model_response", (
            "the hop's reason is the record's own reason field, unchanged"
        )
        assert _WEEKLY_SENTENCE in hop["detail"].lower()
        assert _RESET_SENTENCE in hop["detail"].lower()
        annotation = _hop_text(hop).lower()
        assert "harness_lane_no_model_response" in annotation
        assert _WEEKLY_SENTENCE in annotation, (
            "the takeover annotation must say WHY kimi was passed over"
        )

    def test_an_old_record_lets_kimi_read_again(self, store):
        """Past the TTL the quota may have reset: kimi is asked, as before."""
        _write_kimi_result(store, _kimi_record(age=timedelta(hours=2)))
        chain = _parse_review_gate_takeover_chain(_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN)

        reader, path, _refusal = _manager()._walk_takeover_chain(
            "kimi_gate", PR, chain, None, head=_PrHead(PR, HEAD_SHA),
        )

        assert (reader, path) == ("kimi_gate", [])
