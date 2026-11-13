"""Every receipt carries ``token_usage_normalized`` and, for a claude harness builder,
``start_context_tokens``, beside the raw ``token_usage`` (herwerk-per-model-en-reviewer, ADR-071).

Cases run the REAL ``_govern`` and ``receipt_token_fields`` against ``tmp_path`` stores and
transcripts written here. No real ``claude``, ``codex``, ``gh`` or git.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
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

DISPATCH_ID = "20261113-tokennorm-1"
PROJECT = "proj-a"
SESSION = "sess-norm-1"
START = datetime(2026, 11, 13, 12, 0, 0)
END = datetime(2026, 11, 13, 12, 1, 0)

_VALID_BODY = (
    "## Summary\n\nImplemented the feature with full test coverage. All tests pass "
    "and the implementation is complete and correct in every regard.\n\n"
    "## Changes\n\n- Implemented feature X\n\n"
    "## Verification\n\n- pytest passed: 5/5\n\n"
    "## Open Items\n\nNone\n"
)
_LEDGER_TOKENS = {
    "input_uncached": 120, "cache_hit": 9000, "cache_write_5m": 700, "cache_write_1h": 0, "output": 345,
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    data_dir = tmp_path / PROJECT
    for sub in ("state", "unified_reports"):
        (data_dir / sub).mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    projects = tmp_path / "claude-projects"
    (projects / "proj").mkdir(parents=True)
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(projects))
    return data_dir


def _spec(data_dir: Path, provider: str = "claude", model: str = "sonnet") -> EnvelopeSpec:
    return EnvelopeSpec(DISPATCH_ID, "T1", provider, model, "do the thing", "backend-developer", None,
                        data_dir / "state", data_dir)


def _entry(message_id: str, timestamp: Optional[str], usage: Dict[str, Any]) -> str:
    entry: Dict[str, Any] = {"message": {"id": message_id, "role": "assistant", "usage": usage}}
    if timestamp is not None:
        entry["timestamp"] = timestamp
    return json.dumps(entry)


def _write_transcript(monkeypatch_dir: Path, lines: list, session: str = SESSION) -> None:
    (monkeypatch_dir / "proj" / f"{session}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _projects(tmp_path: Path) -> Path:
    return tmp_path / "claude-projects"


def _first_message_usage() -> Dict[str, Any]:
    return {"input_tokens": 2, "cache_read_input_tokens": 14666, "cache_creation_input_tokens": 67457,
            "output_tokens": 9}


def _govern_receipt(data_dir: Path, *, provider: str = "claude", model: str = "sonnet",
                    token_usage: Optional[Dict[str, Any]] = None, record: Optional[Dict[str, Any]] = None,
                    session_id: Optional[str] = SESSION) -> Dict[str, Any]:
    (data_dir / "unified_reports" / f"{DISPATCH_ID}.md").write_text(_VALID_BODY, encoding="utf-8")
    with patch("envelope_govern._book_ledger_run", return_value=(record, None)), \
         patch("envelope_govern._verify_role_application", return_value=SimpleNamespace(
             role_applied=True, tier="prompt_assembler", reason=None, source_path="/roles/x.md")):
        result = _AdapterResult(returncode=0, completion_text=_VALID_BODY, status="success",
                                token_usage=token_usage if token_usage is not None else {},
                                session_id=session_id)
        _, receipt_path = _govern(_spec(data_dir, provider, model), result, start_time=START, end_time=END)
    assert receipt_path is not None
    lines = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    return lines[0]


def _ledger_record(tokens: Dict[str, Any]) -> Dict[str, Any]:
    return {"cost_usd": 0.5, "cost_source": "price_table@2026-09-14", "run_id": "run-1", "tokens": tokens}


# --- the claude adapter -------------------------------------------------------------------


class _SpawnResult:
    returncode = 0
    error = None
    timed_out = False
    stopped_early = False
    completion_text = "done"
    session_id = SESSION
    model = None

    def __init__(self, usage: Dict[str, Any]) -> None:
        self.token_usage = usage


def _adapter_usage(tmp_path: Path, usage: Dict[str, Any]) -> Dict[str, int]:
    spec = _spec(tmp_path)
    with patch("provider_spawns.claude_spawn.spawn_claude", return_value=_SpawnResult(usage)):
        return ClaudeSubprocessAdapter().run(spec).token_usage


def test_claude_adapter_puts_cache_write_beside_the_unchanged_fields(tmp_path):
    usage = _adapter_usage(tmp_path, {
        "input_tokens": 2, "output_tokens": 311, "cache_read_input_tokens": 14666,
        "cache_creation_input_tokens": 67457,
    })
    assert usage == {"input": 2, "output": 311, "cache_hit": 14666, "cache_write": 67457}


def test_claude_adapter_without_cache_creation_reports_zero_cache_write(tmp_path):
    usage = _adapter_usage(tmp_path, {"input_tokens": 4, "output_tokens": 5})
    assert usage == {"input": 4, "output": 5, "cache_hit": 0, "cache_write": 0}


def test_claude_adapter_malformed_cache_creation_is_not_a_crash_value(tmp_path):
    usage = _adapter_usage(tmp_path, {"input_tokens": 4, "output_tokens": 5, "cache_creation_input_tokens": None})
    assert usage["cache_write"] == 0 and usage["input"] == 4 and usage["output"] == 5


# --- the receipt, ledger path ----------------------------------------------------------------


def test_receipt_with_a_ledger_record_carries_the_records_tokens(store):
    receipt = _govern_receipt(store, token_usage={"input": 2, "output": 5, "cache_hit": 1},
                              record=_ledger_record(_LEDGER_TOKENS))
    assert receipt["token_usage_normalized"] == _LEDGER_TOKENS
    assert receipt["token_usage"] == {"input": 2, "output": 5, "cache_hit": 1}


def test_ledger_record_wins_over_the_lanes_own_usage_and_adds_no_split_reason(store):
    receipt = _govern_receipt(store, provider="kimi", model="kimi-k3",
                              token_usage={"input_tokens": 99, "output_tokens": 1},
                              record=_ledger_record(_LEDGER_TOKENS))
    assert receipt["token_usage_normalized"] == _LEDGER_TOKENS
    assert "split_reason" not in receipt["token_usage_normalized"]


def test_ledger_record_with_unknown_fields_keeps_them_none_not_zero(store):
    unknown = {**_LEDGER_TOKENS, "input_uncached": None, "cache_write_5m": None, "cache_write_1h": None}
    receipt = _govern_receipt(store, record=_ledger_record(unknown))
    assert receipt["token_usage_normalized"] == unknown


@pytest.mark.parametrize("tokens", [None, "garbage", 7, []])
def test_ledger_record_without_a_tokens_mapping_gives_five_none(store, tokens):
    normalized = rtf.normalized_tokens({"input": 1}, "claude", {"run_id": "r", "tokens": tokens})
    assert normalized == dict.fromkeys(LEDGER_TOKEN_FIELDS)


# --- the receipt, CLI path ---------------------------------------------------------------------


def test_codex_usage_splits_cached_input_out_of_the_input_count():
    normalized = rtf.normalized_tokens(
        {"input_tokens": 40939, "cached_input_tokens": 33536, "output_tokens": 810}, "codex", None)
    assert normalized["input_uncached"] == 7403
    assert normalized["cache_hit"] == 33536
    assert normalized["output"] == 810
    assert "split_reason" not in normalized


def test_codex_receipt_end_to_end_without_a_ledger_record(store):
    receipt = _govern_receipt(store, provider="codex", model="gpt-5.2",
                              token_usage={"input": 40939, "cache_hit": 33536, "output": 810}, record=None)
    normalized = receipt["token_usage_normalized"]
    assert (normalized["input_uncached"], normalized["cache_hit"]) == (7403, 33536)
    assert "start_context_tokens" not in receipt


def test_provider_without_a_normalizer_leaves_input_uncached_none_with_a_reason():
    normalized = rtf.normalized_tokens({"input_tokens": 500, "output_tokens": 20}, "glm", None)
    assert normalized["input_uncached"] is None
    assert normalized["output"] == 20
    assert normalized["split_reason"] == "input_cache_split_unmeasured"


def test_codex_cache_read_larger_than_input_is_an_invalid_split():
    normalized = rtf.normalized_tokens({"input_tokens": 10, "cache_read_tokens": 20, "output_tokens": 1}, "codex", None)
    assert normalized["input_uncached"] is None
    assert normalized["split_reason"] == "input_cache_split_invalid"


# --- nothing reported ------------------------------------------------------------------------------


@pytest.mark.parametrize("usage", [None, {}, {"unavailable": True, "input": 0, "output": 0}, "garbage", []])
def test_a_lane_that_reported_nothing_gives_five_none_never_zeros(usage):
    normalized = rtf.normalized_tokens(usage, "codex", None)
    assert normalized == dict.fromkeys(LEDGER_TOKEN_FIELDS)
    assert all(value is None for value in normalized.values())


def test_receipt_of_a_lane_that_reported_nothing_holds_five_none(store):
    receipt = _govern_receipt(store, provider="kimi", model="kimi-k3", token_usage={"unavailable": True})
    assert receipt["token_usage_normalized"] == dict.fromkeys(LEDGER_TOKEN_FIELDS)


def test_a_normalizer_that_raises_never_withholds_the_receipt():
    with patch("receipt_token_fields.normalized_tokens", side_effect=ValueError("boom")):
        fields = rtf.receipt_token_fields(provider="codex", role="backend-developer", session_id=None,
                                          token_usage={"input": 1}, ledger_record=None)
    normalized = fields["token_usage_normalized"]
    assert all(normalized[name] is None for name in LEDGER_TOKEN_FIELDS)
    assert normalized["split_reason"] == "normalization_failed:ValueError"


# --- start_context_tokens -----------------------------------------------------------------------------


def test_start_context_is_the_first_assistant_message_input_cache_read_and_cache_write(tmp_path, store):
    # The later message is written FIRST: the earliest timestamp wins, not the file order.
    _write_transcript(_projects(tmp_path), [
        _entry("m2", "2026-11-13T12:00:09.000Z", {"input_tokens": 5, "cache_read_input_tokens": 80000,
                                                   "cache_creation_input_tokens": 10, "output_tokens": 3}),
        _entry("m1", "2026-11-13T12:00:01.000Z", _first_message_usage()),
    ])
    assert rtf.start_context_tokens(SESSION) == 82125


def test_receipt_of_a_claude_builder_carries_start_context_tokens(tmp_path, store):
    _write_transcript(_projects(tmp_path), [_entry("m1", "2026-11-13T12:00:01.000Z", _first_message_usage())])
    receipt = _govern_receipt(store, record=_ledger_record(_LEDGER_TOKENS))
    assert receipt["start_context_tokens"] == 82125


def test_a_missing_transcript_gives_none_and_the_receipt_omits_the_field(store):
    assert rtf.start_context_tokens(SESSION) is None
    receipt = _govern_receipt(store, record=_ledger_record(_LEDGER_TOKENS))
    assert "start_context_tokens" not in receipt
    assert receipt["token_usage_normalized"] == _LEDGER_TOKENS


@pytest.mark.parametrize("session", [None, "", "  "])
def test_no_session_id_gives_none(session):
    assert rtf.start_context_tokens(session) is None


def test_a_non_claude_lane_writes_no_start_context_field(tmp_path, store):
    _write_transcript(_projects(tmp_path), [_entry("m1", "2026-11-13T12:00:01.000Z", _first_message_usage())])
    fields = rtf.receipt_token_fields(provider="codex", role="backend-developer", session_id=SESSION,
                                      token_usage={"input": 1}, ledger_record=None)
    assert "start_context_tokens" not in fields
    receipt = _govern_receipt(store, provider="codex", model="gpt-5.2", token_usage={"input": 1, "output": 1})
    assert "start_context_tokens" not in receipt


def test_a_gate_seat_is_not_a_builder_and_gets_no_start_context(tmp_path, store):
    _write_transcript(_projects(tmp_path), [_entry("m1", "2026-11-13T12:00:01.000Z", _first_message_usage())])
    fields = rtf.receipt_token_fields(provider="claude", role="review-gate", session_id=SESSION,
                                      token_usage={"input": 1}, ledger_record=None)
    assert "start_context_tokens" not in fields


@pytest.mark.parametrize("missing", ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"])
def test_an_unknown_part_makes_the_whole_start_context_none(tmp_path, store, missing):
    usage = _first_message_usage()
    del usage[missing]
    _write_transcript(_projects(tmp_path), [_entry("m1", "2026-11-13T12:00:01.000Z", usage)])
    assert rtf.start_context_tokens(SESSION) is None


@pytest.mark.parametrize("timestamp", [None, "not-a-time", ""])
def test_a_transcript_with_nothing_to_rank_on_gives_none(tmp_path, store, timestamp):
    _write_transcript(_projects(tmp_path), [_entry("m1", timestamp, _first_message_usage())])
    assert rtf.start_context_tokens(SESSION) is None


def test_a_transcript_read_that_raises_gives_none(store):
    with patch("receipt_token_fields.harvest_session_messages", side_effect=OSError("disk")):
        assert rtf.start_context_tokens(SESSION) is None


# --- unchanged contracts -------------------------------------------------------------------------------


def test_phantom_guard_receives_the_same_token_sum_with_and_without_cache_writes(store):
    seen = []

    def _capture(**kwargs):
        seen.append(kwargs["token_usage"])
        return None

    for usage in ({"input": 2, "output": 311, "cache_hit": 14666},
                  {"input": 2, "output": 311, "cache_hit": 14666, "cache_write": 67457}):
        result = _AdapterResult(returncode=0, completion_text="x", status="success", token_usage=usage)
        with patch("phantom_guard.record_phantom_if_any", side_effect=_capture), \
             patch("envelope_govern._publish_phantom_verdict"), \
             patch("envelope_govern._resolve_fix_forward_diff", return_value="diff"):
            _run_phantom_guard(_spec(store), result, "diff", "origin/main")
    assert seen == [313, 313]


def test_receipt_v2_stamps_the_new_fields_only_when_set():
    base = dict(dispatch_id="d", terminal_id="T1", provider="claude", model="sonnet", event_type="task_complete",
                status="success", completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0,
                token_usage={}, cost_usd=None, pr_id=None, role="backend-developer", receipt_kind="dispatch")
    plain = ReceiptV2(**base).to_dict()
    assert "token_usage_normalized" not in plain and "start_context_tokens" not in plain
    stamped = ReceiptV2(**base, token_usage_normalized=_LEDGER_TOKENS, start_context_tokens=0).to_dict()
    assert stamped["token_usage_normalized"] == _LEDGER_TOKENS
    assert stamped["start_context_tokens"] == 0
