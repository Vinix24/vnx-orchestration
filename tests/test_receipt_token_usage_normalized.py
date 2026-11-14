"""Every receipt carries ``token_usage_normalized`` and, for a claude harness builder,
``start_context_tokens``, beside the raw ``token_usage`` (herwerk-per-model-en-reviewer, ADR-071).

Cases run the real ``_govern`` and ``receipt_token_fields`` against ``tmp_path`` stores and
transcripts written here. No real ``claude``, ``codex``, ``gh`` or git.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

import receipt_token_fields as rtf  # noqa: E402
from envelope_adapters_claude import ClaudeSubprocessAdapter  # noqa: E402
from envelope_govern import _govern, _run_phantom_guard  # noqa: E402
from envelope_types import EnvelopeSpec, _AdapterResult  # noqa: E402
from receipt_schema import ReceiptV2  # noqa: E402
from token_harvest import LEDGER_TOKEN_FIELDS  # noqa: E402

DISPATCH_ID, SESSION = "20261113-tokennorm-1", "sess-norm-1"
BODY = ("## Summary\n\nImplemented the feature with full test coverage. All tests pass and the "
        "implementation is complete and correct in every regard.\n\n## Changes\n\n- X\n\n"
        "## Verification\n\n- pytest passed: 5/5\n\n## Open Items\n\nNone\n")
LEDGER_TOKENS = {"input_uncached": 120, "cache_hit": 9000, "cache_write_5m": 700, "cache_write_1h": 0, "output": 345}
NONE5 = dict.fromkeys(LEDGER_TOKEN_FIELDS)
FIRST = {"input_tokens": 2, "cache_read_input_tokens": 14666, "cache_creation_input_tokens": 67457, "output_tokens": 9}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    data_dir = tmp_path / "proj-a"
    for sub in ("state", "unified_reports"):
        (data_dir / sub).mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    monkeypatch.setenv("VNX_PROJECT_ID", "proj-a")
    (tmp_path / "projects" / "p").mkdir(parents=True)
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    return data_dir


def _spec(data_dir, provider="claude", model="sonnet"):
    return EnvelopeSpec(DISPATCH_ID, "T1", provider, model, "do it", "backend-developer", None, data_dir / "state", data_dir)


def _transcript(tmp_path, *entries):
    """Each entry is (message_id, timestamp or None, usage)."""
    lines = []
    for message_id, stamp, usage in entries:
        line = {"message": {"id": message_id, "role": "assistant", "usage": usage}}
        if stamp is not None:
            line["timestamp"] = stamp
        lines.append(json.dumps(line))
    (tmp_path / "projects" / "p" / f"{SESSION}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _receipt(data_dir, *, provider="claude", model="sonnet", usage=None, record=None):
    (data_dir / "unified_reports" / f"{DISPATCH_ID}.md").write_text(BODY, encoding="utf-8")
    role_app = SimpleNamespace(role_applied=True, tier="t", reason=None, source_path="/r.md")
    with patch("envelope_govern._book_ledger_run", return_value=(record, None)), \
         patch("envelope_govern._verify_role_application", return_value=role_app):
        result = _AdapterResult(returncode=0, completion_text=BODY, status="success",
                                token_usage=usage or {}, session_id=SESSION)
        _, path = _govern(_spec(data_dir, provider, model), result,
                          start_time=datetime(2026, 11, 13, 12, 0), end_time=datetime(2026, 11, 13, 12, 1))
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def _record(tokens):
    return {"cost_usd": 0.5, "cost_source": "price_table@2026-09-14", "run_id": "run-1", "tokens": tokens}


class _Spawn:
    returncode, error, timed_out, stopped_early, completion_text, session_id, model = 0, None, False, False, "done", SESSION, None

    def __init__(self, usage):
        self.token_usage = usage


@pytest.mark.parametrize("raw, expected", [
    ({"input_tokens": 2, "output_tokens": 311, "cache_read_input_tokens": 14666, "cache_creation_input_tokens": 67457},
     {"input": 2, "output": 311, "cache_hit": 14666, "cache_write": 67457}),
    ({"input_tokens": 4, "output_tokens": 5}, {"input": 4, "output": 5, "cache_hit": 0, "cache_write": 0}),
    ({"input_tokens": 4, "output_tokens": 5, "cache_creation_input_tokens": None},
     {"input": 4, "output": 5, "cache_hit": 0, "cache_write": 0}),
])
def test_claude_adapter_adds_cache_write_and_keeps_the_other_fields(tmp_path, raw, expected):
    with patch("provider_spawns.claude_spawn.spawn_claude", return_value=_Spawn(raw)):
        assert ClaudeSubprocessAdapter().run(_spec(tmp_path)).token_usage == expected


def test_receipt_with_a_ledger_record_carries_the_records_tokens_and_keeps_raw_usage(store):
    receipt = _receipt(store, usage={"input": 2, "output": 5, "cache_hit": 1}, record=_record(LEDGER_TOKENS))
    assert receipt["token_usage_normalized"] == LEDGER_TOKENS
    assert receipt["token_usage"] == {"input": 2, "output": 5, "cache_hit": 1}


def test_ledger_record_wins_over_the_lanes_own_usage(store):
    receipt = _receipt(store, provider="kimi", model="kimi-k3", usage={"input_tokens": 99}, record=_record(LEDGER_TOKENS))
    assert receipt["token_usage_normalized"] == LEDGER_TOKENS


def test_ledger_record_with_unknown_fields_keeps_them_none_not_zero(store):
    unknown = {**LEDGER_TOKENS, "input_uncached": None, "cache_write_5m": None, "cache_write_1h": None}
    assert _receipt(store, record=_record(unknown))["token_usage_normalized"] == unknown


@pytest.mark.parametrize("tokens", [None, "garbage", 7, []])
def test_ledger_record_without_a_tokens_mapping_gives_five_none(tokens):
    assert rtf.normalized_tokens({"input": 1}, "claude", {"run_id": "r", "tokens": tokens}) == NONE5


def test_codex_usage_splits_the_cached_part_out_of_input():
    normalized = rtf.normalized_tokens({"input_tokens": 40939, "cached_input_tokens": 33536, "output_tokens": 810}, "codex", None)
    assert (normalized["input_uncached"], normalized["cache_hit"], normalized["output"]) == (7403, 33536, 810)
    assert "split_reason" not in normalized


def test_codex_receipt_without_a_ledger_record_carries_the_split_and_no_start_context(store):
    receipt = _receipt(store, provider="codex", model="gpt-5.2", usage={"input": 40939, "cache_hit": 33536, "output": 810})
    assert (receipt["token_usage_normalized"]["input_uncached"], receipt["token_usage_normalized"]["cache_hit"]) == (7403, 33536)
    assert "start_context_tokens" not in receipt


@pytest.mark.parametrize("provider, usage, reason", [
    ("glm", {"input_tokens": 500, "output_tokens": 20}, "input_cache_split_unmeasured"),
    ("codex", {"input_tokens": 10, "cache_read_tokens": 20, "output_tokens": 1}, "input_cache_split_invalid"),
])
def test_an_unknown_split_leaves_input_uncached_none_with_a_reason(provider, usage, reason):
    normalized = rtf.normalized_tokens(usage, provider, None)
    assert normalized["input_uncached"] is None and normalized["split_reason"] == reason


@pytest.mark.parametrize("usage", [None, {}, {"unavailable": True, "input": 0, "output": 0}, "garbage", []])
def test_a_lane_that_reported_nothing_gives_five_none_never_zeros(usage):
    assert rtf.normalized_tokens(usage, "codex", None) == NONE5


def test_receipt_of_a_lane_that_reported_nothing_holds_five_none(store):
    assert _receipt(store, provider="kimi", model="kimi-k3", usage={"unavailable": True})["token_usage_normalized"] == NONE5


def test_a_normalizer_that_raises_never_withholds_the_receipt():
    with patch("receipt_token_fields.normalized_tokens", side_effect=ValueError("boom")):
        normalized = rtf.receipt_token_fields(provider="codex", role="backend-developer", session_id=None,
                                              token_usage={"input": 1}, ledger_record=None)["token_usage_normalized"]
    assert {name: normalized[name] for name in LEDGER_TOKEN_FIELDS} == NONE5
    assert normalized["split_reason"] == "normalization_failed:ValueError"


def test_start_context_is_the_earliest_assistant_message_not_the_first_in_the_file(tmp_path, store):
    later = {"input_tokens": 5, "cache_read_input_tokens": 80000, "cache_creation_input_tokens": 10, "output_tokens": 3}
    _transcript(tmp_path, ("m2", "2026-11-13T12:00:09.000Z", later), ("m1", "2026-11-13T12:00:01.000Z", FIRST))
    assert rtf.start_context_tokens(SESSION) == 82125


def test_receipt_of_a_claude_builder_carries_start_context_tokens(tmp_path, store):
    _transcript(tmp_path, ("m1", "2026-11-13T12:00:01.000Z", FIRST))
    assert _receipt(store, record=_record(LEDGER_TOKENS))["start_context_tokens"] == 82125


def test_a_missing_transcript_gives_none_and_the_receipt_omits_the_field(store):
    assert rtf.start_context_tokens(SESSION) is None
    receipt = _receipt(store, record=_record(LEDGER_TOKENS))
    assert "start_context_tokens" not in receipt and receipt["token_usage_normalized"] == LEDGER_TOKENS


@pytest.mark.parametrize("session", [None, "", "  "])
def test_no_session_id_gives_none(session):
    assert rtf.start_context_tokens(session) is None


@pytest.mark.parametrize("provider, role", [("codex", "backend-developer"), ("claude", "review-gate")])
def test_a_non_claude_lane_or_a_reviewer_seat_writes_no_start_context_field(tmp_path, store, provider, role):
    _transcript(tmp_path, ("m1", "2026-11-13T12:00:01.000Z", FIRST))
    fields = rtf.receipt_token_fields(provider=provider, role=role, session_id=SESSION, token_usage={"input": 1}, ledger_record=None)
    assert "start_context_tokens" not in fields


@pytest.mark.parametrize("missing", ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"])
def test_an_unknown_part_makes_the_whole_start_context_none(tmp_path, store, missing):
    usage = {k: v for k, v in FIRST.items() if k != missing}
    _transcript(tmp_path, ("m1", "2026-11-13T12:00:01.000Z", usage))
    assert rtf.start_context_tokens(SESSION) is None


@pytest.mark.parametrize("stamp", [None, "not-a-time", ""])
def test_a_transcript_with_nothing_to_rank_on_gives_none(tmp_path, store, stamp):
    _transcript(tmp_path, ("m1", stamp, FIRST))
    assert rtf.start_context_tokens(SESSION) is None


def test_a_transcript_read_that_raises_gives_none(store):
    with patch("receipt_token_fields.harvest_session_messages", side_effect=OSError("disk")):
        assert rtf.start_context_tokens(SESSION) is None


def test_phantom_guard_receives_the_same_token_sum_with_and_without_cache_writes(store):
    seen = []
    for usage in ({"input": 2, "output": 311, "cache_hit": 14666}, {"input": 2, "output": 311, "cache_hit": 14666, "cache_write": 67457}):
        result = _AdapterResult(returncode=0, completion_text="x", status="success", token_usage=usage)
        with patch("phantom_guard.record_phantom_if_any", side_effect=lambda **kw: seen.append(kw["token_usage"])), \
             patch("envelope_govern._publish_phantom_verdict"), \
             patch("envelope_govern._resolve_fix_forward_diff", return_value="diff"):
            _run_phantom_guard(_spec(store), result, "diff", "origin/main")
    assert seen == [313, 313]


def test_receipt_v2_stamps_the_new_fields_only_when_set():
    base = dict(dispatch_id="d", terminal_id="T1", provider="claude", model="sonnet", event_type="task_complete", status="success",
                completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0, token_usage={}, cost_usd=None, pr_id=None,
                role="backend-developer", receipt_kind="dispatch")
    plain = ReceiptV2(**base).to_dict()
    assert "token_usage_normalized" not in plain and "start_context_tokens" not in plain
    stamped = ReceiptV2(**base, token_usage_normalized=LEDGER_TOKENS, start_context_tokens=0).to_dict()
    assert stamped["token_usage_normalized"] == LEDGER_TOKENS and stamped["start_context_tokens"] == 0
