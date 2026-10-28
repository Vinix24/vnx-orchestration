"""tests/test_k2b_a_builder_cost_lines.py — codex and kimi BUILDER runs in the ledger (K2b-a).

Track ``kosten-per-model-in-het-grootboek``, deliverable K2b-a (ADR-043). K2a put
the claude harness lanes in the ledger. This deliverable adds the two lanes that
report their own usage instead of leaving a claude transcript, as BUILDER: a
codex or kimi build books exactly one cost-ledger line per attempt, from the
usage the CLI itself reported. The gate half (``ExecutionDepth``, governed-lane
reports, golden records) is K2b-b.

- ``codex`` — ``token_usage`` from ``codex_spawn``. MEASURED SOURCE: a codex
  input count INCLUDES the cached part
  (``tests/fixtures/gate_verdict/pr-1869-codex_gate-bare-verdict.ndjson`` line 7
  reports ``input_tokens: 40939`` beside ``cached_input_tokens: 33536`` for one
  call), so the ledger books ``input_uncached = input_tokens - cache_read_tokens``.
- ``kimi`` — ``token_usage`` from the post-run ``wire.jsonl`` harvest. MEASURED
  SOURCE: ``input_tokens`` is the sum of ``input_other``, the per-call NEW tokens,
  and so EXCLUDES the cache read
  (``scripts/lib/provider_spawns/kimi_spawn.py:483-489``); no subtraction. A run
  the CLI reported no usage for books ``unknown:kimi_cli_no_usage`` /
  ``unavailable:kimi_cli_no_usage``, never zeros.

A missing cache count never books the total as uncached: ``input_uncached`` is
``None`` with ``input_cache_split_unmeasured`` (or ``..._invalid`` when the
cached count exceeds the input), and a provider whose split was never measured
gets the same reason instead of a default.

Both bill flat (codex on the operator's ChatGPT login, kimi on CLI OAuth), so
every line is ``billing: subscription`` with ``cost_usd`` 0 and the registry's
list price in ``api_equivalent_usd``. Every attempt writes one line: a refire is
attempt 2, and a refused spawn is one zero line carrying
``unavailable:spawn_refused``. Booking is fail-open for these lanes — a ledger
that raises never changes the build outcome — but every failed booking is a
WARNING naming the ref, the provider, the role and the reason.

The ledger here is the real writer on a temporary store; only the CLI
subprocess boundary is replaced.
"""
from __future__ import annotations

import errno
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import cost_ledger  # noqa: E402
import harness_run_cost  # noqa: E402

PROJECT = "vnx-dev"
CODEX_MODEL = "gpt-6-astra"
KIMI_MODEL = "kimi-k3"

RUN_START = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
RUN_END = datetime(2026, 10, 27, 10, 1, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A temporary store: the real cost ledger lives here, under ``<data>/events``."""
    data = tmp_path / "data"
    state = data / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    return SimpleNamespace(
        data=data, state=state, ledger=data / "events" / "provider_costs.ndjson",
    )


def _lines(store) -> list:
    """Every cost-ledger run line in the store, in write order."""
    if not store.ledger.exists():
        return []
    return [
        record for record in (
            json.loads(line) for line in store.ledger.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        if record.get("schema") == cost_ledger.RUN_SCHEMA
    ]


def _warnings(caplog) -> str:
    return " | ".join(r.getMessage() for r in caplog.records if r.name == "harness_run_cost")


def _book_usage(store, caplog, *, provider, model, usage, ref="20261027-kp"):
    """One builder booking from *usage*, through the real ledger, under a captured log."""
    run = harness_run_cost.cli_run(
        provider=provider, model=model, started=RUN_START, ended=RUN_END, token_usage=usage,
    )
    with caplog.at_level(logging.WARNING, logger="harness_run_cost"):
        record, reason = harness_run_cost.record_cli_run(
            run, project_id=PROJECT, producer="provider_dispatch", ref=ref,
            role=harness_run_cost.BUILDER_ROLE, ledger_path=store.ledger,
        )
    assert reason is None, reason
    return record


# ---------------------------------------------------------------------------
# (a) the input/cache split is normalised per provider, from the measured source
# ---------------------------------------------------------------------------


def test_a_codex_usage_books_the_uncached_input_never_the_total(store, caplog):
    """OI-2407 acceptance 1: input 40939 with 33536 cached books 7403 uncached.

    Codex's ``input_tokens`` INCLUDES the cached input (the measured source is
    ``tests/fixtures/gate_verdict/pr-1869-codex_gate-bare-verdict.ndjson`` line
    7: one call reporting input_tokens 40939 and cached_input_tokens 33536).
    Booking the total as uncached double-counts every cached token: the run
    would be priced twice, once at the input rate and once at the cache rate.
    """
    record = _book_usage(
        store, caplog, provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        usage={"input_tokens": 40939, "cached_input_tokens": 33536, "output_tokens": 991},
    )

    assert record["tokens"]["input_uncached"] == 7403
    assert record["tokens"]["cache_hit"] == 33536
    assert record["tokens"]["output"] == 991
    assert record["usage_completeness"] == "partial:cache_write_1h,cache_write_5m"
    assert _warnings(caplog) == "", "a measured split is booked without a warning"


def test_a_codex_usage_without_a_cached_count_never_books_the_total_as_uncached(store, caplog):
    """Acceptance 2: no provider defaults 'input means uncached'.

    A codex report that carries no cached count leaves the split unmeasured:
    ``input_uncached`` AND ``cache_hit`` are ``None`` with
    ``input_cache_split_unmeasured`` written down, because booking the total as
    the uncached part is exactly the double count this round removes.
    """
    record = _book_usage(
        store, caplog, provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        usage={"input_tokens": 40939, "output_tokens": 991},
    )

    assert record["tokens"]["input_uncached"] is None
    assert record["tokens"]["cache_hit"] is None
    assert record["tokens"]["output"] == 991
    assert record["usage_completeness"] == "partial:cache_hit,cache_write_1h,cache_write_5m,input_uncached"
    assert harness_run_cost.INPUT_CACHE_SPLIT_UNMEASURED in _warnings(caplog)
    assert "20261027-kp" in _warnings(caplog), "the warning names the run it could not split"


def test_a_cached_count_larger_than_the_input_never_books_a_negative(store, caplog):
    """OI-2407 acceptance 3: 100 input with 250 cached is a contradiction, not -150.

    The two reported numbers disagree, so the DERIVED uncached part is unknown:
    ``input_uncached`` is ``None`` with ``input_cache_split_invalid`` and the
    line says ``partial:``. The reported cache count is still booked — it is
    what the lane said — and the reason is never silent.
    """
    record = _book_usage(
        store, caplog, provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        usage={"input_tokens": 100, "cached_input_tokens": 250, "output_tokens": 10},
    )

    assert record["tokens"]["input_uncached"] is None
    assert record["tokens"]["cache_hit"] == 250
    assert record["usage_completeness"] == "partial:cache_write_1h,cache_write_5m,input_uncached"
    assert harness_run_cost.INPUT_CACHE_SPLIT_INVALID in _warnings(caplog)


def test_the_two_providers_get_one_normaliser_each():
    """Acceptance 2: one function per provider, each with its measured source.

    Codex's input INCLUDES the cached part (the gate fixture above); kimi's does
    NOT — measured on the wire.jsonl ``StatusUpdate`` fixture the kimi lane
    already uses (``tests/test_kimi_token_capture.py::_status_update``): it
    records ``input_other`` next to ``input_cache_read``, and
    ``_parse_wire_token_usage`` sums ``input_other`` into ``input_tokens``, so a
    kimi input count is new tokens only.
    """
    codex, codex_reason = harness_run_cost._cli_tokens(
        {"input_tokens": 40939, "cached_input_tokens": 33536, "output_tokens": 991}, "codex",
    )
    kimi, kimi_reason = harness_run_cost._cli_tokens(
        {"input_tokens": 42110, "cache_read_tokens": 39424, "output_tokens": 4223}, "kimi",
    )

    assert (codex["input_uncached"], codex["cache_hit"], codex_reason) == (7403, 33536, None)
    # Kimi's 42110 already excludes the 39424 it read from cache: no subtraction.
    assert (kimi["input_uncached"], kimi["cache_hit"], kimi_reason) == (42110, 39424, None)


def test_a_provider_whose_split_was_never_measured_books_input_uncached_unknown():
    """Acceptance 2: an unmeasured provider gets no default 'input means uncached'."""
    tokens, reason = harness_run_cost._cli_tokens(
        {"input_tokens": 100, "cache_read_tokens": 20, "output_tokens": 5}, "gemini",
    )

    assert tokens["input_uncached"] is None
    assert tokens["cache_hit"] == 20
    assert tokens["output"] == 5
    assert reason == harness_run_cost.INPUT_CACHE_SPLIT_UNMEASURED


# ---------------------------------------------------------------------------
# (b) the kimi builder: input_other is the uncached part, cache read beside it
# ---------------------------------------------------------------------------


def test_a_kimi_builder_books_input_other_as_uncached_and_the_cache_read_beside_it(store, caplog):
    """A kimi builder's input EXCLUDES the cache read (measured, kimi_spawn:483-489).

    The wire ``StatusUpdate`` reports ``input_other: 4000`` (per-call NEW
    tokens) and ``input_cache_read: 10000`` (cumulative context-cache read).
    ``_parse_wire_token_usage`` sums ``input_other`` into ``input_tokens`` and
    keeps the cache read as its own count, so the ledger books 4000 uncached
    and 10000 cache hits: no subtraction, and no negative even though the cache
    read is the larger number.
    """
    from provider_spawns.kimi_spawn import _parse_wire_token_usage

    wire = json.dumps({"timestamp": 1785515300.0, "message": {
        "type": "StatusUpdate", "payload": {"token_usage": {
            "input_other": 4000, "output": 500,
            "input_cache_read": 10000, "input_cache_creation": 0,
        }},
    }})
    usage = _parse_wire_token_usage(wire)
    assert usage["input_tokens"] == 4000 and usage["cache_read_tokens"] == 10000

    record = _book_usage(
        store, caplog, provider=harness_run_cost.KIMI_PROVIDER, model=KIMI_MODEL, usage=usage,
    )

    assert record["tokens"]["input_uncached"] == 4000
    assert record["tokens"]["cache_hit"] == 10000
    assert record["tokens"]["output"] == 500
    assert record["billing"] == "subscription" and record["cost_usd"] == 0.0
    assert record["cost_source"].startswith("price_table@")
    assert _warnings(caplog) == "", "a measured kimi run books no split warning"


def test_a_kimi_builder_with_no_usage_books_the_documented_unavailable(store, caplog):
    """kimi-cli 1.46.0's stream carries no usage accounting (ADR-043 §8).

    A kimi builder whose post-run harvest found no ``StatusUpdate`` reports
    ``None``; the line says the run is unknown with no amount, never three
    zeros — a zero would be an invention.
    """
    record = _book_usage(
        store, caplog, provider=harness_run_cost.KIMI_PROVIDER, model=KIMI_MODEL, usage=None,
    )

    assert record["usage_completeness"] == "unknown:kimi_cli_no_usage"
    assert record["cost_source"] == "unavailable:kimi_cli_no_usage"
    assert record["cost_usd"] == 0.0 and record["api_equivalent_usd"] is None
    assert all(value is None for value in record["tokens"].values())


# ---------------------------------------------------------------------------
# (c) a subscription line at the registry price, one per attempt
# ---------------------------------------------------------------------------


def test_a_codex_builder_line_is_a_subscription_line_at_the_registry_price(store, caplog):
    """Codex bills flat: 0 at the margin, the registry list price as api_equivalent_usd."""
    record = _book_usage(
        store, caplog, provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        usage={"input_tokens": 14000, "cached_input_tokens": 10000, "output_tokens": 500},
    )

    assert record["role"] == "builder" and record["provider"] == "codex"
    assert record["model"] == CODEX_MODEL
    assert record["billing"] == "subscription" and record["cost_usd"] == 0.0
    assert record["cost_source"].startswith("price_table@")
    assert record["api_equivalent_usd"] > 0


def test_a_refire_is_a_second_line_with_attempt_two(store, caplog):
    """Two attempts of one ref are two lines, never one overwritten line."""
    first = _book_usage(
        store, caplog, provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        usage={"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20}, ref="20261027-refire",
    )
    second = _book_usage(
        store, caplog, provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        usage={"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20}, ref="20261027-refire",
    )

    assert first["run_id"] == f"{PROJECT}:provider_dispatch:20261027-refire:1"
    assert second["run_id"] == f"{PROJECT}:provider_dispatch:20261027-refire:2"
    assert first["record_id"] != second["record_id"]


def test_a_refused_spawn_is_one_zero_line_with_spawn_refused(store, caplog):
    """A builder spawn the lane refused still books its attempt — as a refusal, not as unknown usage."""
    run = harness_run_cost.cli_run(
        provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        started=RUN_START, ended=RUN_END,
        spawn_refused="codex_model_unresolved: no codex model in the registry",
    )
    record, reason = harness_run_cost.record_cli_run(
        run, project_id=PROJECT, producer="provider_dispatch", ref="20261027-refused",
        role=harness_run_cost.BUILDER_ROLE, ledger_path=store.ledger,
    )

    assert reason is None, reason
    assert record["usage_completeness"] == "complete"
    assert record["cost_source"] == "unavailable:spawn_refused"
    assert record["cost_usd"] == 0.0 and record["api_equivalent_usd"] is None
    assert all(value == 0 for value in record["tokens"].values())


def test_a_review_seat_on_a_cli_lane_is_not_a_builder_run():
    """K2b-b books a gate seat; the builder half must leave it to the old event until then."""
    assert harness_run_cost.books_cli_run("codex", "backend-developer") is True
    assert harness_run_cost.books_cli_run("kimi", None) is True
    assert harness_run_cost.books_cli_run("codex", "review-gate") is False
    assert harness_run_cost.books_cli_run("kimi", "deliberation-panelist") is False
    assert harness_run_cost.books_cli_run("deepseek-harness", "backend-developer") is False


# ---------------------------------------------------------------------------
# (d) fail-open: a ledger that raises never changes the build outcome, but is loud
# ---------------------------------------------------------------------------


def test_a_ledger_write_that_raises_returns_none_with_a_loud_warning(store, monkeypatch, caplog):
    """A builder whose booking throws still builds; the refusal is never silent."""
    def _explode(*_args, **_kwargs):
        raise cost_ledger.CostLedgerWriteError("the ledger and its spool are both gone")

    monkeypatch.setattr(cost_ledger, "record_next_attempt", _explode)
    run = harness_run_cost.cli_run(
        provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        started=RUN_START, ended=RUN_END,
        token_usage={"input_tokens": 100, "cache_read_tokens": 0, "output_tokens": 20},
    )
    with caplog.at_level(logging.WARNING, logger="harness_run_cost"):
        record, reason = harness_run_cost.record_cli_run(
            run, project_id=PROJECT, producer="provider_dispatch", ref="20261027-broken",
            role=harness_run_cost.BUILDER_ROLE, ledger_path=store.ledger,
        )

    assert record is None and "CostLedgerWriteError" in reason
    assert _lines(store) == [], "nothing was written, and the caller still goes on"
    (warning,) = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "harness_run_cost"
    ]
    message = warning.getMessage()
    assert "codex" in message and "builder" in message and "20261027-broken" in message
    assert "CostLedgerWriteError" in message, "a failed booking is never silent"


def _permission_denied(*_args, **_kwargs):
    """The registry/ledger read ``record_next_attempt`` propagates unchanged (an OSError)."""
    raise PermissionError(errno.EACCES, "Permission denied", "/store/state/provider_costs.ndjson")


def test_a_registry_read_error_returns_none_with_a_reason_and_one_warning(store, monkeypatch, caplog):
    """An OSError reading the ledger is not a CostLedger* error, and it must not escape either.

    ``cost_ledger.record_next_attempt`` propagates an OSError from reading the
    registry unchanged; a booker that only catches the ledger's own errors lets
    it through and the build call has no cost at all.
    """
    run = harness_run_cost.cli_run(
        provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        started=RUN_START, ended=RUN_END,
        # A complete codex usage (the cache count is explicit) so the only
        # warning this test sees is the booking refusal itself.
        token_usage={"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0},
    )
    monkeypatch.setattr(cost_ledger, "record_next_attempt", _permission_denied)
    with caplog.at_level(logging.WARNING, logger="harness_run_cost"):
        record, reason = harness_run_cost.record_codex_run(
            run, project_id=PROJECT, producer="provider_dispatch", ref="20261027-perm",
            role=harness_run_cost.BUILDER_ROLE, ledger_path=store.ledger,
        )

    assert record is None and "PermissionError" in reason
    (warning,) = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "harness_run_cost"
    ]
    message = warning.getMessage()
    assert "codex" in message and "builder" in message and "20261027-perm" in message
    assert "PermissionError" in message, "a failed booking is never silent"
