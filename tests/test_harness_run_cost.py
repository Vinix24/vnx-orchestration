"""tests/test_harness_run_cost.py — builder runs on the claude harness lanes write the cost ledger (K2a-2).

Track ``kosten-per-model-in-het-grootboek``, deliverable K2a, second PR
(ADR-043). Every builder run on ``claude``, ``deepseek-harness`` or
``glm-harness`` writes exactly one ``cost_ledger`` line per attempt, and those
lanes stop writing the old ``emit_provider_cost`` event. Since K2b the codex and
kimi lanes book their own line the same way (their gate half is covered in
``test_k2b_codex_kimi_cost_lines.py``). Covered here, against the real model
registry, real files and the real call sites:

- ``token_harvest`` gives the five ledger fields per assistant message,
  deduplicated on ``message.id``; a field a message lacks is ``None``, never 0;
- the R1b acceptance from ADR-043 line 12: the transcript of the DeepSeek build
  of R1b books 7,218,944 cache hits next to 102,385 input and 64,922 output;
- a run across DeepSeek's peak boundary is two segments priced per window;
- a refire is a new attempt and a new ``run_id``; a refused spawn is a zero line;
  a missing transcript is ``unknown``/``unavailable`` and never a priced zero;
- every model string the three providers put in receipts over 30 days resolves
  to a registry key (fixture), the dated haiku id through a registry alias;
- per call site (``envelope_govern._govern``, the subprocess recovery loop,
  ``provider_dispatch._emit_governance``): one ledger line and no old line for a
  harness builder run, the old line and no ledger line for codex/kimi, the
  receipt's cost and ``cost_ledger_run_ids`` from the record, a failed append
  that lands in the spool and still gets its receipt (ADR-043 §6, K2a-3a), and
  a spool that fails too: no receipt and no second attempt booked.

Transcripts are real JSONL files under a temporary projects dir
(``VNX_CLAUDE_PROJECTS_DIR``); the ledger is the real file under a temporary
``VNX_DATA_DIR``. Only the spawn itself and side effects unrelated to cost
(git, worktrees, report bodies) are patched.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
for _path in (str(REPO_ROOT), str(_LIB)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger  # noqa: E402
import harness_run_cost  # noqa: E402
import provider_costs  # noqa: E402
import token_harvest  # noqa: E402
from providers import provider_registry  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "cost_ledger"
R1B_TRANSCRIPT = FIXTURES / "r1b_deepseek_harness_transcript.jsonl"
MODEL_STRINGS = FIXTURES / "harness_model_strings_30d.json"
PROJECT = "vnx-dev"
START = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)  # a Wednesday, inside the 06-10 UTC peak
END = START + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Fixtures and transcript builders
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A temporary central store and projects dir: the ledger and every transcript live here."""
    data = tmp_path / "store"
    state = data / "state"
    state.mkdir(parents=True)
    (data / "unified_reports").mkdir()
    projects = tmp_path / "claude-projects"
    projects.mkdir()
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setenv("VNX_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.delenv("VNX_TMUX_SIGNAL_DIR", raising=False)
    return SimpleNamespace(
        data=data, state=state, projects=projects,
        ledger=data / "events" / "provider_costs.ndjson",
        receipts=state / "t0_receipts.ndjson",
    )


def _usage(input_tokens=0, output_tokens=0, cache_read=0, write_5m=0, write_1h=0, *, flat=None, drop=()):
    usage = {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": write_5m + write_1h,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output_tokens,
        "cache_creation": {"ephemeral_5m_input_tokens": write_5m, "ephemeral_1h_input_tokens": write_1h},
    }
    if flat is not None:
        usage.pop("cache_creation")
        usage["cache_creation_input_tokens"] = flat
    for name in drop:
        usage.pop(name)
    return usage


def _assistant(message_id, timestamp, usage, model="deepseek-v4-pro"):
    return {
        "type": "assistant", "timestamp": timestamp,
        "message": {"id": message_id, "role": "assistant", "model": model, "usage": usage},
    }


def _write_transcript(projects: Path, session_id: str, entries) -> Path:
    path = projects / "-tmp-dispatch-worktree" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "timestamp": "2026-10-21T09:00:00Z", "message": {"role": "user", "content": "go"}}]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in lines + list(entries)), encoding="utf-8")
    return path


def _two_message_transcript(projects: Path, session_id: str, model="deepseek-v4-pro") -> Path:
    return _write_transcript(projects, session_id, [
        _assistant("msg-1", "2026-10-21T09:01:00Z", _usage(1000, 200, cache_read=4000), model),
        _assistant("msg-2", "2026-10-21T09:05:00Z", _usage(3000, 300, cache_read=6000), model),
    ])


def _run_records(ledger: Path) -> list:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ledger_lines(ledger: Path) -> list:
    return [record for record in _run_records(ledger) if record.get("schema") == cost_ledger.RUN_SCHEMA]


def _old_lines(ledger: Path) -> list:
    return [record for record in _run_records(ledger) if "schema" not in record]


def _receipts(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _builder(provider="deepseek-harness", model="deepseek-v4-pro", session_id=None, spawn_refused=None,
             started=START, ended=END):
    return harness_run_cost.BuilderRun(
        provider=provider, model=model, started=started, ended=ended,
        session_id=session_id, spawn_refused=spawn_refused,
    )


def _record(store, run, dispatch_id="20261021-kp-test", producer="envelope_govern"):
    return harness_run_cost.record_builder_run(
        run, project_id=PROJECT, producer=producer, dispatch_id=dispatch_id, ledger_path=store.ledger,
    )


def _failing_append(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


@pytest.fixture()
def broken_spool(store):
    """A spool directory that can be listed but takes no file: every spool write fails with ``EACCES``."""
    if os.geteuid() == 0:
        pytest.skip("root writes into a read-only directory")
    spool = store.ledger.parent / "provider_costs.spool"
    spool.mkdir(parents=True)
    spool.chmod(0o555)
    yield spool
    spool.chmod(0o755)


def _spooled(store, run_id) -> list:
    """The records waiting in the spool for ``run_id``; none when the run has no spool file."""
    path = cost_ledger.spool_file_for(store.ledger.parent / "provider_costs.spool", run_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def _attempts_booked():
    """A spy on ``cost_ledger.record_next_attempt``: how often a caller tried to book an attempt."""
    return patch.object(cost_ledger, "record_next_attempt", wraps=cost_ledger.record_next_attempt)


# ---------------------------------------------------------------------------
# token_harvest: the five ledger fields per message
# ---------------------------------------------------------------------------


class TestPerMessageUsage:
    def test_cache_reads_and_both_cache_writes_deduplicated_on_message_id(self, store):
        path = _write_transcript(store.projects, "sess-fields", [
            # a message rewritten while it streamed: only its last rewrite counts
            _assistant("m-a", "2026-10-21T09:01:00Z", _usage(10, 1, cache_read=100)),
            _assistant("m-a", "2026-10-21T09:01:03Z", _usage(1000, 50, cache_read=20000, write_5m=300)),
            _assistant("m-b", "2026-10-21T09:02:00Z", _usage(500, 70, cache_read=9000, write_5m=40, write_1h=1200)),
            # a pre-split entry: only the flat cache-creation total, booked under 5m
            _assistant("m-c", "2026-10-21T09:03:00Z", _usage(200, 30, cache_read=10, flat=77)),
        ])

        messages = token_harvest.read_transcript_messages(path)

        assert [message.message_id for message in messages] == ["m-a", "m-b", "m-c"]
        assert messages[0].tokens == {
            "input_uncached": 1000, "cache_hit": 20000, "cache_write_5m": 300, "cache_write_1h": 0, "output": 50,
        }
        assert messages[0].timestamp == "2026-10-21T09:01:03Z"
        assert messages[1].tokens["cache_write_1h"] == 1200
        assert messages[2].tokens == {
            "input_uncached": 200, "cache_hit": 10, "cache_write_5m": 77, "cache_write_1h": 0, "output": 30,
        }
        assert harness_run_cost.run_tokens(messages) == {
            "input_uncached": 1700, "cache_hit": 29010, "cache_write_5m": 417, "cache_write_1h": 1200,
            "output": 150,
        }

    def test_a_field_one_message_lacks_is_unknown_for_the_run(self, store):
        path = _write_transcript(store.projects, "sess-partial", [
            _assistant("m-1", "2026-10-21T09:01:00Z", _usage(100, 10, cache_read=5)),
            _assistant("m-2", "2026-10-21T09:02:00Z", _usage(100, 10, drop=("cache_read_input_tokens",))),
        ])

        messages = token_harvest.read_transcript_messages(path)
        tokens = harness_run_cost.run_tokens(messages)

        assert messages[1].tokens["cache_hit"] is None
        assert tokens["cache_hit"] is None, "a missing count must never become a silent 0"
        assert tokens["input_uncached"] == 200
        assert harness_run_cost.usage_completeness(tokens) == "partial:cache_hit"

    def test_no_assistant_usage_is_none_not_an_empty_run(self, store):
        path = _write_transcript(store.projects, "sess-empty", [])

        assert token_harvest.read_transcript_messages(path) is None
        assert token_harvest.harvest_session_messages("sess-empty") is None
        assert token_harvest.harvest_session_messages("sess-does-not-exist") is None
        assert token_harvest.harvest_session_messages(None) is None

    def test_the_receipt_totals_keep_their_shape(self, store):
        _write_transcript(store.projects, "sess-receipt", [
            _assistant("m-1", "2026-10-21T09:01:00Z", _usage(100, 10, cache_read=5, write_5m=2, write_1h=3)),
        ])

        assert token_harvest.harvest_session_tokens("sess-receipt") == {
            "input": 100, "output": 10, "cache_creation_5m": 2, "cache_creation_1h": 3, "cache_read": 5,
        }
        assert token_harvest.harvest_session_tokens("sess-missing")["unavailable"] is True


# ---------------------------------------------------------------------------
# The R1b acceptance (ADR-043 line 12)
# ---------------------------------------------------------------------------


class TestTheR1bAcceptance:
    """The fixture is the R1b transcript with only the per-message usage kept (no content)."""

    def test_the_transcript_carries_the_cache_hits_the_old_line_missed(self):
        messages = token_harvest.read_transcript_messages(R1B_TRANSCRIPT)

        assert len(messages) == 73, "73 messages after deduplicating 156 assistant entries on message.id"
        assert harness_run_cost.run_tokens(messages) == {
            "input_uncached": 102385, "cache_hit": 7218944, "cache_write_5m": 0, "cache_write_1h": 0,
            "output": 64922,
        }

    def test_the_ledger_line_of_r1b_books_7_2_million_cache_hits(self, store):
        session_id = "dc1baa89-b585-40d7-b4a6-964a05cf9ab8"
        target = store.projects / "-r1b-worktree" / f"{session_id}.jsonl"
        target.parent.mkdir(parents=True)
        target.write_text(R1B_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

        record = _record(store, _builder(session_id=session_id), dispatch_id="20261019-rs-r1b-hygiene")

        assert record["tokens"]["cache_hit"] == 7218944
        assert record["tokens"]["input_uncached"] == 102385
        assert record["tokens"]["output"] == 64922
        assert record["usage_completeness"] == "complete"
        # each message carries the timestamp of its last rewrite, the entry its usage came from
        assert record["occurred_from"] == "2026-10-19T09:03:11.898Z"
        assert record["occurred_to"] == "2026-10-19T09:16:46.136Z"
        # a Monday, 09:03-09:16 UTC: one peak segment, priced at the peak rate
        assert record["segments"] == [{"tokens": record["tokens"], "peak": True, "ts": "2026-10-19T09:03:11.898Z"}]
        expected, _source = provider_costs.price_run(
            "deepseek-harness", "deepseek-v4-pro", [{"input": 102385, "output": 64922, "peak": True}],
        )
        assert record["cost_usd"] == expected
        assert record["cost_source"] == "price_table@2026-09-14"
        assert record["billing"] == "metered"


# ---------------------------------------------------------------------------
# Price windows
# ---------------------------------------------------------------------------


class TestPriceWindows:
    def test_a_run_across_the_peak_boundary_is_two_segments_priced_per_window(self, store):
        _write_transcript(store.projects, "sess-window", [
            _assistant("m-peak", "2026-10-21T09:58:00Z", _usage(1000, 200, cache_read=5000), "deepseek-flash"),
            _assistant("m-off", "2026-10-21T10:02:00Z", _usage(3000, 400, cache_read=7000), "deepseek-flash"),
        ])

        record = _record(store, _builder(model="deepseek-flash", session_id="sess-window"))

        peak, off_peak = record["segments"]
        assert peak == {"tokens": {"input_uncached": 1000, "cache_hit": 5000, "cache_write_5m": 0,
                                   "cache_write_1h": 0, "output": 200}, "peak": True, "ts": "2026-10-21T09:58:00Z"}
        assert off_peak["peak"] is False and off_peak["ts"] == "2026-10-21T10:02:00Z"
        for name in cost_ledger.TOKEN_FIELDS:
            assert peak["tokens"][name] + off_peak["tokens"][name] == record["tokens"][name]
        per_window, _ = provider_costs.price_run("deepseek-harness", "deepseek-flash", [
            {"input": 1000, "output": 200, "cache_read": 5000, "cache_write": 0, "peak": True},
            {"input": 3000, "output": 400, "cache_read": 7000, "cache_write": 0, "peak": False},
        ])
        all_peak, _ = provider_costs.price_run("deepseek-harness", "deepseek-flash", [
            {"input": 4000, "output": 600, "cache_read": 12000, "cache_write": 0, "peak": True},
        ])
        assert record["cost_usd"] == per_window
        assert per_window < all_peak, "the off-peak segment must be priced at the off-peak rate"

    def test_a_message_without_a_timestamp_counts_at_the_peak_rate(self, store):
        _write_transcript(store.projects, "sess-nots", [
            _assistant("m-off", "2026-10-21T11:00:00Z", _usage(100, 10)),
            _assistant("m-nots", None, _usage(50, 5)),
        ])

        record = _record(store, _builder(session_id="sess-nots"))

        by_class = {segment["peak"]: segment["tokens"]["input_uncached"] for segment in record["segments"]}
        assert by_class == {True: 50, False: 100}

    def test_a_provider_without_windows_gets_no_segments(self, store):
        _two_message_transcript(store.projects, "sess-glm", model="z-ai/glm-5.2")

        record = _record(store, _builder(provider="glm-harness", model="glm-5.2", session_id="sess-glm"))

        assert "segments" not in record
        assert record["model"] == "glm-5.2"
        assert record["billing"] == "metered"


# ---------------------------------------------------------------------------
# One run per attempt
# ---------------------------------------------------------------------------


class TestOneRunPerAttempt:
    def test_a_refire_is_a_new_attempt_and_both_are_counted(self, store):
        _two_message_transcript(store.projects, "sess-fire-1")
        _two_message_transcript(store.projects, "sess-fire-2")

        first = _record(store, _builder(session_id="sess-fire-1"))
        second = _record(store, _builder(session_id="sess-fire-2"))

        assert first["run_id"] == f"{PROJECT}:envelope_govern:20261021-kp-test:1"
        assert second["run_id"] == f"{PROJECT}:envelope_govern:20261021-kp-test:2"
        totals = cost_ledger.aggregate_runs(store.ledger)
        assert set(totals.runs) == {first["run_id"], second["run_id"]}
        assert totals.cost_usd == round(first["cost_usd"] + second["cost_usd"], 8)
        assert totals.tokens["cache_hit"] == 20000

    def test_the_attempt_counts_per_producer_and_dispatch(self, store):
        _record(store, _builder(spawn_refused="worktree_creation_failed"))
        other_dispatch = _record(store, _builder(spawn_refused="worktree_creation_failed"), dispatch_id="other")
        other_producer = _record(store, _builder(spawn_refused="worktree_creation_failed"),
                                 producer="provider_dispatch")

        assert other_dispatch["run_id"].endswith(":other:1")
        assert other_producer["run_id"] == f"{PROJECT}:provider_dispatch:20261021-kp-test:1"

    def test_concurrent_attempts_never_share_a_run_id(self, store):
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(_LIB)!r})
            from datetime import datetime, timezone
            from pathlib import Path
            import harness_run_cost
            moment = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
            run = harness_run_cost.BuilderRun(provider="claude", model="sonnet", started=moment, ended=moment,
                                              spawn_refused="worktree_creation_failed")
            harness_run_cost.record_builder_run(run, project_id="vnx-dev", producer="envelope_govern",
                                                dispatch_id="20261021-kp-race", ledger_path=Path({str(store.ledger)!r}))
        """)
        writers = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(6)]
        assert [writer.wait(timeout=60) for writer in writers] == [0] * 6

        attempts = sorted(int(record["run_id"].rsplit(":", 1)[1]) for record in _ledger_lines(store.ledger))
        assert attempts == [1, 2, 3, 4, 5, 6]

    def test_a_refused_spawn_is_a_zero_line(self, store):
        record = _record(store, _builder(spawn_refused="local_key_missing"))

        assert record["tokens"] == dict.fromkeys(cost_ledger.TOKEN_FIELDS, 0)
        assert record["usage_completeness"] == "complete"
        assert record["cost_usd"] == 0.0
        assert record["cost_source"] == "unavailable:spawn_refused"
        totals = cost_ledger.aggregate_runs(store.ledger)
        assert totals.unavailable == (record["run_id"],)
        assert totals.cost_usd == 0.0

    def test_a_missing_transcript_is_unknown_never_a_priced_zero(self, store):
        metered = _record(store, _builder(session_id="sess-gone"))
        subscription = _record(store, _builder(provider="claude", model="sonnet", session_id=None))

        assert metered["tokens"] == dict.fromkeys(cost_ledger.TOKEN_FIELDS)
        assert metered["usage_completeness"] == "unknown:no_transcript"
        assert metered["cost_source"] == "unavailable:no_transcript"
        assert metered["cost_usd"] is None
        assert subscription["cost_source"] == "unavailable:no_transcript"
        assert subscription["api_equivalent_usd"] is None
        assert metered["occurred_from"] == "2026-10-21T09:00:00Z"
        assert metered["occurred_to"] == "2026-10-21T09:30:00Z"
        totals = cost_ledger.aggregate_runs(store.ledger)
        assert set(totals.unavailable) == {metered["run_id"], subscription["run_id"]}
        assert totals.cost_usd == 0.0 and totals.api_equivalent_usd == 0.0
        assert totals.tokens_unknown["cache_hit"] == 2

    def test_the_claude_lane_is_a_subscription_with_its_list_price(self, store):
        _two_message_transcript(store.projects, "sess-claude", model="claude-sonnet-5-5")

        record = _record(store, _builder(provider="claude", model="sonnet", session_id="sess-claude"))

        assert record["billing"] == "subscription"
        assert record["cost_usd"] == 0.0
        expected, _ = provider_costs.price_run("claude", "sonnet", [{"input": 4000, "output": 500, "cache_read": 10000}])
        assert record["api_equivalent_usd"] == expected
        assert record["role"] == "builder"

    def test_a_failed_append_lands_the_attempt_in_the_spool(self, store):
        with patch.object(cost_ledger, "_append_record", _failing_append):
            record = _record(store, _builder(spawn_refused="local_key_missing"))

        assert _ledger_lines(store.ledger) == []
        assert _spooled(store, record["run_id"]) == [record]
        assert record.landed == "spool"

    def test_a_spool_that_fails_too_raises_and_writes_nothing(self, store, broken_spool):
        with patch.object(cost_ledger, "_append_record", _failing_append):
            with pytest.raises(cost_ledger.CostLedgerWriteError) as raised:
                _record(store, _builder(spawn_refused="local_key_missing"))

        assert raised.value.errno == errno.EACCES
        assert "the outcome is unknown" in str(raised.value)
        assert _ledger_lines(store.ledger) == []

    def test_a_non_harness_provider_is_refused(self, store):
        with pytest.raises(cost_ledger.InvalidRecord):
            _record(store, _builder(provider="codex", model="gpt-5.5"))


class TestReceiptCost:
    def test_one_record_gives_its_own_cost_and_run_id(self, store):
        record = _record(store, _builder(spawn_refused="local_key_missing"))

        assert harness_run_cost.receipt_cost([record], ledger_path=store.ledger) == (
            0.0, "unavailable:spawn_refused", [record["run_id"]],
        )

    def test_several_records_sum_through_the_aggregation(self, store):
        _two_message_transcript(store.projects, "sess-sum-1")
        _two_message_transcript(store.projects, "sess-sum-2")
        records = [_record(store, _builder(session_id="sess-sum-1")), _record(store, _builder(session_id="sess-sum-2"))]

        cost, source, run_ids = harness_run_cost.receipt_cost(records, ledger_path=store.ledger)

        assert cost == round(records[0]["cost_usd"] + records[1]["cost_usd"], 8)
        assert source == "price_table@2026-09-14"
        assert run_ids == [record["run_id"] for record in records]

    def test_a_run_without_a_price_makes_the_sum_unknown(self, store):
        _two_message_transcript(store.projects, "sess-priced")
        records = [_record(store, _builder(session_id="sess-priced")), _record(store, _builder(session_id="gone"))]

        cost, source, _ = harness_run_cost.receipt_cost(records, ledger_path=store.ledger)

        assert cost is None
        assert source == "unavailable:runs_without_price:1/2"


# ---------------------------------------------------------------------------
# Model identity (OI-2224 advice 1)
# ---------------------------------------------------------------------------


def _model_strings():
    return json.loads(MODEL_STRINGS.read_text(encoding="utf-8"))


def _identity(provider: str, model: str) -> str:
    return provider_registry.resolve_model_identity(provider_costs.registry_key_for_provider(provider), model)


class TestTheMeasuredModelStrings:
    @pytest.mark.parametrize(
        "entry", _model_strings()["receipt_strings"] + _model_strings()["raw_provider_ids"],
        ids=lambda entry: f"{entry['provider']}-{entry['model']}",
    )
    def test_every_measured_string_resolves_to_its_registry_key(self, entry):
        assert _identity(entry["provider"], entry["model"]) == entry["registry_key"]

    @pytest.mark.parametrize(
        "entry",
        _model_strings()["receipt_strings_not_from_a_runner"] + _model_strings()["raw_provider_ids_not_in_the_registry"],
        ids=lambda entry: f"{entry['provider']}-{entry['model']}",
    )
    def test_a_placeholder_or_unregistered_model_is_refused_not_guessed(self, entry, store):
        with pytest.raises(provider_registry.RegistryLookupError):
            _identity(entry["provider"], entry["model"])
        with pytest.raises(cost_ledger.UnknownModel):
            _record(store, _builder(provider=entry["provider"], model=entry["model"], spawn_refused="x"))

    def test_the_dated_haiku_id_books_as_haiku(self, store):
        record = _record(store, _builder(provider="claude", model="claude-haiku-4-5-20251001", spawn_refused="x"))

        assert record["model"] == "haiku"

    def test_an_alias_that_names_two_models_fails_the_load(self, tmp_path):
        registry = tmp_path / "models.yaml"
        model = {"litellm_name": "a/b", "cost_input_per_mtok": 1.0, "cost_output_per_mtok": 2.0,
                 "max_tokens": 1, "supports_streaming": True, "supports_tool_calls": True}
        registry.write_text(json.dumps({"providers": {"anthropic": {"enabled": True, "models": {
            "haiku": dict(model, aliases=["fast"]), "sonnet": dict(model, aliases=["fast"]),
        }}}}), encoding="utf-8")

        with pytest.raises(ValueError, match="alias"):
            provider_registry.load(registry)


# ---------------------------------------------------------------------------
# Call site 1: envelope_govern._govern (door headless + provider lanes)
# ---------------------------------------------------------------------------


def _envelope_spec(store, provider, model, *, dispatch_id="20261021-kp-env", role="backend-developer"):
    from envelope_types import EnvelopeSpec

    return EnvelopeSpec(
        dispatch_id=dispatch_id, terminal_id="T1", provider=provider, model=model,
        instruction="implement the feature", role=role, pr_id=None,
        state_dir=store.state, data_dir=store.data,
    )


def _adapter(model, *, session_id=None, spawn_refused=None, token_usage=None, status="success"):
    from envelope_types import _AdapterResult

    return _AdapterResult(
        returncode=0 if status == "success" else 1, completion_text="done", status=status,
        token_usage=token_usage or {"input": 4000, "output": 500, "cache_hit": 10000},
        model=model, session_id=session_id, spawn_refused=spawn_refused,
    )


def _govern(spec, adapter_result):
    import envelope_govern

    with patch("governance_emit.emit_unified_report", MagicMock(return_value=None)), \
         patch("phantom_guard.record_phantom_if_any", MagicMock(return_value=None)):
        return envelope_govern._govern(spec, adapter_result, START, END)


class TestTheEnvelopeCallSite:
    def test_a_deepseek_builder_run_writes_one_ledger_line_and_no_old_line(self, store):
        _two_message_transcript(store.projects, "sess-env-ds")

        _report, receipt_path = _govern(
            _envelope_spec(store, "deepseek-harness", "default"),
            _adapter("deepseek-v4-pro", session_id="sess-env-ds"),
        )

        (line,) = _ledger_lines(store.ledger)
        assert _old_lines(store.ledger) == [], "the harness lane must stop writing the old event"
        assert line["run_id"] == f"{PROJECT}:envelope_govern:20261021-kp-env:1"
        assert line["role"] == "builder" and line["provider"] == "deepseek-harness"
        assert line["model"] == "deepseek-v4-pro"
        assert line["tokens"]["cache_hit"] == 10000
        (receipt,) = _receipts(receipt_path)
        assert receipt["cost_ledger_run_ids"] == [line["run_id"]]
        assert receipt["cost_usd"] == line["cost_usd"]
        assert receipt["cost_source"] == line["cost_source"]

    def test_paid_lane_budget_reads_the_ledger_cost_from_the_receipt(self, store):
        import paid_lane_budget

        _two_message_transcript(store.projects, "sess-env-budget")
        _govern(_envelope_spec(store, "deepseek-harness", "deepseek-v4-pro"),
                _adapter("deepseek-v4-pro", session_id="sess-env-budget"))

        (line,) = _ledger_lines(store.ledger)
        spend = paid_lane_budget.spent_today(store.state)
        assert line["cost_usd"] > 0
        assert spend.total_usd == line["cost_usd"]
        assert spend.unknown_cost_count == 0

    def test_a_claude_headless_builder_run_is_a_subscription_line(self, store):
        _two_message_transcript(store.projects, "sess-env-claude", model="claude-sonnet-5-5")

        _govern(_envelope_spec(store, "claude", "sonnet"), _adapter("sonnet", session_id="sess-env-claude"))

        (line,) = _ledger_lines(store.ledger)
        assert line["billing"] == "subscription" and line["api_equivalent_usd"] > 0
        (receipt,) = _receipts(store.receipts)
        assert receipt["cost_usd"] == 0.0
        assert receipt["cost_ledger_run_ids"] == [line["run_id"]]

    @pytest.mark.parametrize("provider,model", [("codex", "gpt-5.5"), ("kimi", "kimi-k3")])
    def test_codex_and_kimi_write_their_own_line_and_no_old_event(self, store, provider, model):
        # K2b inverted this: the two CLI lanes book their own line now, so the
        # old event must be gone — a lane with both would double-count the run.
        _govern(_envelope_spec(store, provider, model), _adapter(model, session_id="sess-not-read"))

        (line,) = _ledger_lines(store.ledger)
        assert _old_lines(store.ledger) == [], "K2b books the codex/kimi lane itself"
        assert line["run_id"] == f"{PROJECT}:envelope_govern:20261021-kp-env:1"
        assert line["provider"] == provider and line["role"] == "builder"
        assert line["billing"] == "subscription" and line["cost_usd"] == 0.0
        assert line["tokens"]["input_uncached"] == 4000 and line["tokens"]["cache_hit"] == 10000
        (receipt,) = _receipts(store.receipts)
        assert receipt["cost_ledger_run_ids"] == [line["run_id"]]

    def test_a_review_seat_on_a_harness_lane_keeps_the_old_line(self, store):
        _govern(_envelope_spec(store, "deepseek-harness", "deepseek-v4-pro", role="review-gate"),
                _adapter("deepseek-v4-pro"))

        assert _ledger_lines(store.ledger) == []
        assert len(_old_lines(store.ledger)) == 1

    def test_a_refire_is_a_second_run_even_when_the_receipt_is_deduplicated(self, store):
        spec = _envelope_spec(store, "deepseek-harness", "deepseek-v4-pro")
        _govern(spec, _adapter("deepseek-v4-pro", spawn_refused="paid_lane_budget_exceeded", status="failure"))
        _two_message_transcript(store.projects, "sess-refire")
        _govern(spec, _adapter("deepseek-v4-pro", session_id="sess-refire"))

        first, second = _ledger_lines(store.ledger)
        assert first["run_id"].endswith(":1") and second["run_id"].endswith(":2")
        assert first["cost_source"] == "unavailable:spawn_refused"
        assert second["tokens"]["input_uncached"] == 4000

    def test_a_refused_spawn_writes_the_zero_line_and_its_receipt(self, store):
        _govern(_envelope_spec(store, "deepseek-harness", "deepseek-v4-pro"),
                _adapter("deepseek-v4-pro", spawn_refused="worktree_creation_failed", status="failure"))

        (line,) = _ledger_lines(store.ledger)
        assert line["cost_source"] == "unavailable:spawn_refused" and line["cost_usd"] == 0.0
        (receipt,) = _receipts(store.receipts)
        assert receipt["cost_usd"] == 0.0 and receipt["cost_source"] == "unavailable:spawn_refused"

    @pytest.mark.parametrize("provider,expected", [("deepseek-harness", "deepseek-v4-pro"), ("glm-harness", "glm-5.2")])
    def test_a_refusal_before_the_adapter_resolved_a_model_books_the_lane_default(
        self, store, monkeypatch, provider, expected,
    ):
        from envelope_types import _AdapterResult

        monkeypatch.delenv("VNX_DEEPSEEK_HARNESS_MODEL", raising=False)
        monkeypatch.delenv("VNX_GLM_HARNESS_MODEL", raising=False)
        refused = _AdapterResult(returncode=1, completion_text="", status="failure", error="budget",
                                 spawn_refused="paid_lane_budget_exceeded")

        _govern(_envelope_spec(store, provider, "default"), refused)

        (line,) = _ledger_lines(store.ledger)
        assert line["model"] == expected
        assert line["cost_source"] == "unavailable:spawn_refused"
        assert len(_receipts(store.receipts)) == 1, "a budget refusal keeps its receipt"

    def test_lane_model_resolves_placeholders_like_the_adapter(self, monkeypatch):
        monkeypatch.setenv("VNX_DEEPSEEK_HARNESS_MODEL", "deepseek-flash")

        assert harness_run_cost.lane_model("deepseek-harness", "sonnet") == "deepseek-flash"
        assert harness_run_cost.lane_model("deepseek-harness", "deepseek-v4-pro") == "deepseek-v4-pro"
        assert harness_run_cost.lane_model("claude", "opus") == "opus"

    def test_a_failed_append_spools_the_line_and_writes_the_receipt(self, store):
        """ADR-005 holds with the spool: the line is in the ledger or the spool before the receipt."""
        _two_message_transcript(store.projects, "sess-env-spool")
        with patch.object(cost_ledger, "_append_record", _failing_append):
            _report, receipt_path = _govern(
                _envelope_spec(store, "deepseek-harness", "deepseek-v4-pro"),
                _adapter("deepseek-v4-pro", session_id="sess-env-spool"),
            )

        run_id = f"{PROJECT}:envelope_govern:20261021-kp-env:1"
        assert _ledger_lines(store.ledger) == []
        (spooled,) = _spooled(store, run_id)
        (receipt,) = _receipts(receipt_path)
        assert receipt["cost_ledger_run_ids"] == [run_id]
        assert (receipt["cost_usd"], receipt["cost_source"]) == (spooled["cost_usd"], spooled["cost_source"])
        assert cost_ledger.aggregate_runs(store.ledger).spool_pending == 1

    def test_a_spool_that_fails_too_writes_no_receipt_and_books_no_second_attempt(self, store, broken_spool):
        _two_message_transcript(store.projects, "sess-env-enospc")
        with patch.object(cost_ledger, "_append_record", _failing_append), _attempts_booked() as booked:
            _report, receipt_path = _govern(
                _envelope_spec(store, "deepseek-harness", "deepseek-v4-pro"),
                _adapter("deepseek-v4-pro", session_id="sess-env-enospc"),
            )

        assert booked.call_count == 1, "an unknown outcome is never booked again as a new attempt"
        assert receipt_path is None
        assert _receipts(store.receipts) == [], "no receipt may carry a cost the ledger does not have"
        assert _run_records(store.ledger) == []
        (gap,) = _receipts(store.state / "receipt_emit_failures.ndjson")
        assert gap["marker"] == "VNX_RECEIPT_EMIT_FAILURE"
        assert "CostLedgerWriteError" in gap["reason"] and "the outcome is unknown" in gap["reason"]

    def test_an_unknown_model_writes_no_receipt_and_records_the_refusal(self, store):
        _report, receipt_path = _govern(
            _envelope_spec(store, "claude", "fable"), _adapter("claude-fable-5-1", spawn_refused="x"),
        )

        assert receipt_path is None and _receipts(store.receipts) == []
        (gap,) = _receipts(store.state / "receipt_emit_failures.ndjson")
        assert "UnknownModel" in gap["reason"]


class TestTheAdaptersCarryTheSignals:
    def test_the_harness_mapper_forwards_the_session_and_the_refusal(self):
        from envelope_adapters_provider import _map_harness_spawn_result
        from provider_spawns.deepseek_harness_spawn import DeepSeekHarnessSpawnResult

        ran = DeepSeekHarnessSpawnResult(returncode=0, completion={"text": "ok"}, events_written=3,
                                         session_id="sess-ran", timed_out=False)
        refused = DeepSeekHarnessSpawnResult(returncode=1, completion={}, events_written=0, session_id=None,
                                             timed_out=False, error="local key missing",
                                             spawn_refused="local_key_missing")

        assert _map_harness_spawn_result(ran, "deepseek-harness", "deepseek-v4-pro").session_id == "sess-ran"
        mapped = _map_harness_spawn_result(refused, "deepseek-harness", "deepseek-v4-pro")
        assert mapped.status == "failure" and mapped.spawn_refused == "local_key_missing"

    def test_the_claude_adapter_forwards_a_spawn_that_never_started(self, store):
        from envelope_adapters_claude import ClaudeSubprocessAdapter
        from provider_spawns.claude_spawn import ClaudeSpawnResult

        never_started = ClaudeSpawnResult(returncode=1, completion={}, events_written=0, session_id=None,
                                          timed_out=False, spawn_refused="deliver_failed: unknown_role: x")
        with patch("provider_spawns.claude_spawn.spawn_claude", return_value=never_started):
            result = ClaudeSubprocessAdapter().run(_envelope_spec(store, "claude", "sonnet"))

        assert result.status == "failure"
        assert result.spawn_refused == "deliver_failed: unknown_role: x"

    def test_the_harness_spawns_name_their_refusals(self, tmp_path, monkeypatch):
        from provider_spawns import deepseek_harness_spawn, glm_harness_spawn

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("VNX_PROVIDER_ENV_FILE", str(tmp_path / "absent.env"))
        no_key = deepseek_harness_spawn.spawn_deepseek_harness("p", None, "d", "T1")
        with patch.object(glm_harness_spawn, "_proxy_reachable", return_value=False):
            no_proxy = glm_harness_spawn.spawn_glm_harness("p", None, "d", "T1")

        assert no_key.spawn_refused == "local_key_missing"
        assert no_proxy.spawn_refused == "proxy_unreachable"


# ---------------------------------------------------------------------------
# Call site 2: the subprocess lane (recovery), one line per delivery attempt
# ---------------------------------------------------------------------------


def _deliver(store, results, *, role="backend-developer", max_retries=1):
    import subprocess_dispatch
    from subprocess_dispatch_internals import recovery

    monitor = MagicMock()
    monitor.stuck_count = 0
    with patch.object(subprocess_dispatch, "deliver_via_subprocess", side_effect=list(results)), \
         patch.object(subprocess_dispatch, "_get_commit_hash", return_value="abc123"), \
         patch.object(subprocess_dispatch, "_get_dirty_files", return_value=frozenset()), \
         patch.object(subprocess_dispatch, "_capture_dispatch_parameters"), \
         patch.object(subprocess_dispatch, "WorkerHealthMonitor", return_value=monitor), \
         patch.object(subprocess_dispatch, "_check_commit_since", return_value=False), \
         patch.object(subprocess_dispatch, "_commit_belongs_to_dispatch", return_value=False), \
         patch.object(subprocess_dispatch, "_ensure_unified_report"), \
         patch.object(subprocess_dispatch, "_write_receipt") as write_receipt, \
         patch.object(subprocess_dispatch, "_update_pattern_confidence", return_value=0), \
         patch.object(subprocess_dispatch, "_capture_dispatch_outcome"), \
         patch.object(subprocess_dispatch, "_promote_manifest"), \
         patch.object(subprocess_dispatch, "cleanup_worker_exit"), \
         patch.object(subprocess_dispatch, "_resolve_active_dispatch_file", return_value=None), \
         patch.object(subprocess_dispatch, "_default_state_dir", return_value=store.state), \
         patch.object(recovery.time, "sleep"):
        ok = recovery.deliver_with_recovery(
            "T1", "do it", "sonnet", "20261021-kp-sub", role=role, max_retries=max_retries,
            auto_commit=False,
        )
    return ok, write_receipt


def _sub_result(success, session_id):
    from subprocess_dispatch_internals.delivery_runtime import _SubprocessResult

    return _SubprocessResult(success=success, session_id=session_id, event_count=3, manifest_path=None)


class TestTheSubprocessCallSite:
    @pytest.fixture(autouse=True)
    def _shared_govern_off(self, monkeypatch):
        monkeypatch.delenv("VNX_SHARED_GOVERN", raising=False)

    def test_every_attempt_writes_a_line_and_the_receipt_names_them(self, store):
        _two_message_transcript(store.projects, "sess-sub-1", model="claude-sonnet-5-5")
        _two_message_transcript(store.projects, "sess-sub-2", model="claude-sonnet-5-5")

        ok, write_receipt = _deliver(store, [_sub_result(False, "sess-sub-1"), _sub_result(True, "sess-sub-2")])

        assert ok is True
        lines = _ledger_lines(store.ledger)
        assert [line["run_id"] for line in lines] == [
            f"{PROJECT}:subprocess_recovery:20261021-kp-sub:1", f"{PROJECT}:subprocess_recovery:20261021-kp-sub:2",
        ]
        assert all(line["billing"] == "subscription" and line["provider"] == "claude" for line in lines)
        assert _old_lines(store.ledger) == []
        kwargs = write_receipt.call_args.kwargs
        assert kwargs["cost_ledger_run_ids"] == [line["run_id"] for line in lines]
        assert kwargs["cost_usd"] == 0.0
        assert kwargs["cost_source"] == "price_table@2026-09-29"

    def test_the_final_failure_receipt_names_every_attempt(self, store):
        ok, write_receipt = _deliver(store, [_sub_result(False, None), _sub_result(False, None)])

        assert ok is False
        lines = _ledger_lines(store.ledger)
        assert len(lines) == 2 and all(line["cost_source"] == "unavailable:no_transcript" for line in lines)
        kwargs = write_receipt.call_args.kwargs
        assert kwargs["cost_ledger_run_ids"] == [line["run_id"] for line in lines]
        assert kwargs["cost_usd"] is None
        assert kwargs["cost_source"] == "unavailable:runs_without_price:2/2"

    def test_a_spawn_that_never_started_is_a_zero_line(self, store):
        from subprocess_dispatch_internals.delivery_runtime import _SubprocessResult

        never = _SubprocessResult(success=False, session_id=None, event_count=0, manifest_path=None,
                                  spawn_refused="deliver_failed: [Errno 2] No such file or directory: 'claude'")
        _deliver(store, [never], max_retries=0)

        (line,) = _ledger_lines(store.ledger)
        assert line["cost_source"] == "unavailable:spawn_refused"
        assert line["tokens"] == dict.fromkeys(cost_ledger.TOKEN_FIELDS, 0)

    def test_a_review_role_keeps_the_old_line(self, store):
        _deliver(store, [_sub_result(True, None)], role="review-gate", max_retries=0)

        assert _ledger_lines(store.ledger) == []
        assert len(_old_lines(store.ledger)) == 1

    def test_a_failed_append_spools_the_attempt_and_the_receipt_names_it(self, store):
        with patch.object(cost_ledger, "_append_record", _failing_append):
            ok, write_receipt = _deliver(store, [_sub_result(True, None)], max_retries=0)

        run_id = f"{PROJECT}:subprocess_recovery:20261021-kp-sub:1"
        assert ok is True
        assert _ledger_lines(store.ledger) == []
        (spooled,) = _spooled(store, run_id)
        kwargs = write_receipt.call_args.kwargs
        assert kwargs["cost_ledger_run_ids"] == [run_id]
        assert (kwargs["cost_usd"], kwargs["cost_source"]) == (spooled["cost_usd"], spooled["cost_source"])

    def test_a_spool_that_fails_too_propagates_and_books_no_second_attempt(self, store, broken_spool):
        """The retry loop does not go on: a retry would book a new attempt for an outcome that is unknown."""
        with patch.object(cost_ledger, "_append_record", _failing_append), _attempts_booked() as booked:
            with pytest.raises(cost_ledger.CostLedgerWriteError):
                _deliver(store, [_sub_result(False, None), _sub_result(True, None)], max_retries=1)

        assert booked.call_count == 1
        assert _run_records(store.ledger) == []

    def test_the_receipt_writer_stamps_the_run_ids(self, store):
        from subprocess_dispatch_internals import receipt_writer

        with patch.object(receipt_writer, "_persist_receipt", side_effect=lambda receipt, *_a: receipt):
            receipt = receipt_writer._write_receipt(
                "20261021-kp-sub", "T1", "done", cost_usd=0.0, cost_source="price_table@2026-09-29",
                cost_ledger_run_ids=["vnx-dev:subprocess_recovery:20261021-kp-sub:1"], provider="claude",
                model="sonnet",
            )

        assert receipt["cost_ledger_run_ids"] == ["vnx-dev:subprocess_recovery:20261021-kp-sub:1"]


# ---------------------------------------------------------------------------
# Call site 3: provider_dispatch._emit_governance (standalone provider CLI)
# ---------------------------------------------------------------------------


def _pd_args(provider, dispatch_id="20261021-kp-pd", role="backend-developer"):
    return argparse.Namespace(
        provider=provider, terminal_id="T1", dispatch_id=dispatch_id, instruction="Reply OK.",
        model="sonnet", role=role, dispatch_paths="", auto_route=False, pr_id=None, deadline_seconds=900,
    )


class TestTheProviderDispatchCallSite:
    def test_a_deepseek_builder_run_writes_one_ledger_line_and_no_old_line(self, store):
        import provider_dispatch as pd
        from provider_spawns.deepseek_harness_spawn import DeepSeekHarnessSpawnResult

        _two_message_transcript(store.projects, "sess-pd-ds")
        result = DeepSeekHarnessSpawnResult(returncode=0, completion={"text": "OK"}, events_written=3,
                                            session_id="sess-pd-ds", timed_out=False, model="deepseek-v4-pro")

        pd._emit_governance(_pd_args("deepseek-harness"), "deepseek-harness", "deepseek-v4-pro", result,
                            START, END, "success")

        (line,) = _ledger_lines(store.ledger)
        assert line["run_id"] == f"{PROJECT}:provider_dispatch:20261021-kp-pd:1"
        assert _old_lines(store.ledger) == []
        (receipt,) = [r for r in _receipts(store.receipts) if r.get("dispatch_id") == "20261021-kp-pd"]
        assert receipt["cost_ledger_run_ids"] == [line["run_id"]]
        assert receipt["cost_usd"] == line["cost_usd"] and receipt["cost_source"] == line["cost_source"]

    def test_a_missing_key_is_a_refused_spawn_line(self, store, tmp_path, monkeypatch):
        import provider_dispatch as pd

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("VNX_PROVIDER_ENV_FILE", str(tmp_path / "absent.env"))

        assert pd._dispatch_deepseek_harness(_pd_args("deepseek-harness")) == pd._EX_USAGE

        (line,) = _ledger_lines(store.ledger)
        assert line["cost_source"] == "unavailable:spawn_refused"
        assert line["model"] == "deepseek-v4-pro"
        assert _old_lines(store.ledger) == []

    def test_codex_writes_its_own_line_and_no_old_event(self, store):
        # K2b inverted this: the codex builder's measured end books its line.
        import provider_dispatch as pd

        result = SimpleNamespace(token_usage={"input_tokens": 100, "output_tokens": 20}, completion_text="OK",
                                 returncode=0, error=None, timed_out=False)
        pd._emit_governance(_pd_args("codex"), "codex", "gpt-5.5", result, START, END, "success")

        (line,) = _ledger_lines(store.ledger)
        assert _old_lines(store.ledger) == []
        assert line["run_id"] == f"{PROJECT}:provider_dispatch:20261021-kp-pd:1"
        assert line["provider"] == "codex" and line["billing"] == "subscription"
        assert line["tokens"]["input_uncached"] == 100 and line["tokens"]["output"] == 20

    def test_a_plan_gate_seat_keeps_the_old_line(self, store):
        import provider_dispatch as pd

        result = SimpleNamespace(token_usage=None, completion_text="OK", returncode=0, error=None,
                                 timed_out=False, session_id=None)
        pd._emit_governance(_pd_args("glm-harness", role="plan-reviewer"), "glm-harness", "glm-5.2", result,
                            START, END, "success")

        assert _ledger_lines(store.ledger) == []
        assert len(_old_lines(store.ledger)) == 1

    def test_runs_the_subprocess_lane_booked_are_not_written_twice(self, store):
        import provider_dispatch as pd

        booked = _record(store, _builder(provider="claude", model="sonnet", spawn_refused="x"),
                         dispatch_id="20261021-kp-pd", producer="subprocess_recovery")
        result = SimpleNamespace(token_usage=None, completion_text="", cost_ledger_records=[booked])

        cost, source, run_ids = pd._book_dispatch_cost(
            _pd_args("claude"), "claude", "sonnet", result, {}, (START, END), store.state,
        )

        assert (cost, source, run_ids) == (0.0, "unavailable:spawn_refused", [booked["run_id"]])
        assert len(_ledger_lines(store.ledger)) == 1

    def test_a_failed_append_spools_the_line_and_the_receipt_takes_its_cost(self, store):
        import provider_dispatch as pd

        result = SimpleNamespace(token_usage=None, completion_text="", session_id=None, spawn_refused="x")
        with patch.object(cost_ledger, "_append_record", _failing_append):
            cost, source, run_ids = pd._book_dispatch_cost(
                _pd_args("deepseek-harness"), "deepseek-harness", "deepseek-v4-pro", result, {}, (START, END),
                store.state,
            )

        run_id = f"{PROJECT}:provider_dispatch:20261021-kp-pd:1"
        assert _ledger_lines(store.ledger) == []
        (spooled,) = _spooled(store, run_id)
        assert (cost, source, run_ids) == (spooled["cost_usd"], spooled["cost_source"], [run_id])

    def test_a_spool_that_fails_too_propagates_and_writes_no_receipt(self, store, broken_spool):
        import provider_dispatch as pd

        result = SimpleNamespace(token_usage=None, completion_text="", session_id=None, spawn_refused="x")
        with patch.object(cost_ledger, "_append_record", _failing_append), _attempts_booked() as booked:
            with pytest.raises(cost_ledger.CostLedgerWriteError):
                pd._emit_governance(_pd_args("deepseek-harness"), "deepseek-harness", "deepseek-v4-pro",
                                    result, START, END, "blocked")

        assert booked.call_count == 1
        assert [r for r in _receipts(store.receipts) if r.get("dispatch_id") == "20261021-kp-pd"] == []
