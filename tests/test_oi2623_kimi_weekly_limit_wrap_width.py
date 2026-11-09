"""kimi's weekly-limit refusal counts as an exhausted lane at every wrap width (OI-2623).

Measured 2026-11-07 20:31Z: the second review seat of PR #2477 stayed
``status=unavailable`` / ``reason=harness_lane_no_model_response`` with
``takeover_from=None`` because kimi's 429 body broke at a third width: the OI-2114
reset sentence was cut before " ends", the error type was cut in three and the
first sentence read "weekly (7-day) usage limit".

The fixture is the record's ``status``, ``reason`` and ``reason_detail``,
verbatim (``pr-2477-kimi_gate.json``). Gate, PR, head and ``recorded_at`` are this
test's own; the store is a ``tmp_path`` from ``review_stack_support``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import HEAD_SHA, PR, build_project_store, isolate_config_registry

from gate_request_handler import (
    _PrHead,
    _parse_review_gate_takeover_chain,
    _scan_seat_failure_text,
)
from governance_emit import _classify_lane_log_text

_PR2477_KIMI_WEEKLY_LIMIT = {
    "status": "unavailable",
    "reason": "harness_lane_no_model_response",
    "reason_detail": (
        "kimi_gate report carries failure_reason in its frontmatter (spawn layer failed before "
        "the model ran): [quota_or_auth] provider=kimi reason=quota_or_auth msg='Expecting value: "
        "line 1 column 1 (char 0)' raw='Error code: 429 - {\\'error\\': {\\'message\\': \"You\\'ve "
        "reached your weekly (7-day) usage limit. Your quota will reset when the current 7-day "
        "window'\n"
        "[quota_or_auth] provider=kimi reason=quota_or_auth msg='Expecting value: line 1 column 1 "
        "(char 0)' raw='ends. To continue now, purchase extra usage or upgrade your plan: "
        "https://www.kimi.com/settings/subscription?tab=quota\", \\'type\\': \\'exceeded_cu'\n"
        "[quota_or_auth] provider=kimi reason=quota_or_auth msg='Expecting value: line 1 column 1 "
        "(char 0)' raw=\"rrent_quota_error'}}\"\n"
        "subprocess exited with code 75 before complete event"
    ),
}

_CHAIN = "codex_gate,kimi_gate,deepseek_gate"


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def store(tmp_path, monkeypatch):
    return build_project_store(tmp_path, monkeypatch)


def _record() -> Dict[str, Any]:
    record = {"gate": "kimi_gate", "pr_id": str(PR), "pr_number": PR, "commit_sha": HEAD_SHA}
    record.update(_PR2477_KIMI_WEEKLY_LIMIT)
    stamp = datetime.now(timezone.utc) - timedelta(minutes=2)
    record["recorded_at"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return record


def test_the_measured_text_is_lane_exhausted():
    state, snippet = _classify_lane_log_text(_PR2477_KIMI_WEEKLY_LIMIT["reason_detail"])

    assert state == "lane_exhausted"
    assert snippet is not None


def test_the_scan_of_the_record_fields_finds_the_exhaustion():
    state, detail = _scan_seat_failure_text(_record(), None)

    assert state == "lane_exhausted"
    assert "weekly (7-day) usage limit" in detail.lower()


def test_the_walk_hands_the_kimi_seat_to_deepseek(store):
    import review_gate_manager

    (store["results_dir"] / f"pr-{PR}-kimi_gate.json").write_text(
        json.dumps(_record()), encoding="utf-8",
    )
    chain = _parse_review_gate_takeover_chain(_CHAIN)
    assert chain["kimi_gate"] == "deepseek_gate"

    reader, path, refusal = review_gate_manager.ReviewGateManager()._walk_takeover_chain(
        "kimi_gate", PR, chain, None, head=_PrHead(PR, HEAD_SHA),
    )

    assert reader == "deepseek_gate", f"kimi held its own seat again: {path!r}"
    assert refusal is None
    assert [hop["gate"] for hop in path] == ["kimi_gate"]


@pytest.mark.parametrize("prose", [
    "the report covers the current 7-day window",
    "we track weekly usage",
    "the quota will reset at some point in the current window",
])
def test_generic_prose_about_windows_and_weekly_usage_stays_unreadable(prose):
    assert _classify_lane_log_text(prose) == ("unreadable_verdict", None)
