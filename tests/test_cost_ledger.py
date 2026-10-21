"""tests/test_cost_ledger.py — the cost ledger per run (K2a-1, ADR-043).

Track ``kosten-per-model-in-het-grootboek``, deliverable K2a, first PR. Covers
``scripts/lib/cost_ledger.py`` against the real model registry and real files:

- the writer refuses, with a typed error and nothing written, every record the
  plan forbids: an amount without a source, ``unavailable`` with a non-zero
  amount, a correction without ``supersedes``/``reason``, ``complete`` usage with
  an unknown token count, a ``run_id`` of another project, and a model the
  registry does not know;
- the writer prices a run through ``price_run`` when no provider cost is given;
- one aggregation function: per ``run_id`` only the last line counts,
  ``unavailable`` is listed and never summed as 0, old-shape lines are counted as
  ``legacy_lines``, and an unreadable ledger is an error, never an empty total;
- a failed append lands the line in the spool (ADR-043 §6, K2a-3a), whether the
  open, the lock or the append failed; a failed close after a whole append lands
  it in the ledger and the spool;
- a torn append: every append starts on a valid NDJSON boundary, a failed
  append ends its own line when the disk has room, and the ledger is closed
  while its lock is held;
- ``emit_provider_cost`` writes the same line as before (golden).

The acceptance of the one lock file, the spool and the failure contract (K2a-3a)
is in ``tests/test_cost_ledger_one_lock.py``; the replay (K2a-3b) is not built yet.

Failures are injected for real: a ledger path that is a directory makes the
append fail with ``IsADirectoryError``, exactly as an unwritable store would. A
torn append (half a line on disk, then ``ENOSPC``), a refused lock and a failing
close are injected at the ledger's file handle or lock call, so the code under
test runs unchanged.
"""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
for _path in (str(REPO_ROOT), str(_LIB)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger  # noqa: E402
import provider_costs  # noqa: E402

PROJECT = "vnx-dev"
RUN_A1 = f"{PROJECT}:dispatch:20261020-kp-x:1"
RUN_A2 = f"{PROJECT}:dispatch:20261020-kp-x:2"
RUN_B1 = f"{PROJECT}:gate:codex-gate-pr2140:1"

_ZERO = {"input_uncached": 0, "cache_hit": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 0}


def _tokens(**values):
    tokens = dict(_ZERO)
    tokens.update(values)
    return tokens


def _run(**overrides) -> "cost_ledger.RunCost":
    fields = dict(
        run_id=RUN_A1,
        project_id=PROJECT,
        role="builder",
        provider="deepseek-harness",
        model="deepseek-v4-pro",
        occurred_from="2026-10-20T07:00:00Z",
        occurred_to="2026-10-20T07:30:00Z",
        tokens=_tokens(input_uncached=1000, output=200),
        usage_completeness="complete",
        billing="metered",
        cost_usd=0.5,
        cost_source="provider_reported",
    )
    fields.update(overrides)
    return cost_ledger.RunCost(**fields)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "store" / "events" / "provider_costs.ndjson"


def _spool_dir(ledger_path: Path) -> Path:
    """The spool of ADR-043 §6. A refused record never reaches it."""
    return ledger_path.parent / "provider_costs.spool"


def _spooled(ledger_path: Path, run_id: str = RUN_A1) -> list:
    """The records waiting in the spool for ``run_id``, in order; none when the run has no spool file."""
    path = cost_ledger.spool_file_for(_spool_dir(ledger_path), run_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def _lines(ledger_path: Path) -> list:
    if not ledger_path.is_file():
        return []
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_nothing_written(ledger_path: Path) -> None:
    assert _lines(ledger_path) == []
    assert not _spool_dir(ledger_path).exists()


# ---------------------------------------------------------------------------
# The writer refuses (item 2), one test per refusal, nothing written
# ---------------------------------------------------------------------------

class TestTheWriterRefuses:
    def test_an_amount_without_a_source(self, ledger):
        with pytest.raises(cost_ledger.CostWithoutSource):
            cost_ledger.record_run(_run(cost_usd=0.5, cost_source=None), ledger_path=ledger)
        _assert_nothing_written(ledger)

    def test_an_api_equivalent_without_a_source(self, ledger):
        run = _run(provider="claude", model="opus-5-5", billing="subscription",
                   cost_usd=None, cost_source=None, api_equivalent_usd=0.6)
        with pytest.raises(cost_ledger.CostWithoutSource):
            cost_ledger.record_run(run, ledger_path=ledger)
        _assert_nothing_written(ledger)

    def test_unavailable_with_a_non_zero_amount(self, ledger):
        with pytest.raises(cost_ledger.UnavailableWithAmount):
            cost_ledger.record_run(
                _run(cost_usd=0.25, cost_source="unavailable:kimi_cli_no_usage"), ledger_path=ledger,
            )
        _assert_nothing_written(ledger)

    @pytest.mark.parametrize("missing", ["supersedes", "reason"])
    def test_a_correction_without_supersedes_or_reason(self, ledger, missing):
        fields = {"kind": "correction", "supersedes": "a" * 32, "reason": "backfill: transcript"}
        fields[missing] = None
        with pytest.raises(cost_ledger.CorrectionWithoutSupersedes):
            cost_ledger.record_run(_run(**fields), ledger_path=ledger)
        _assert_nothing_written(ledger)

    def test_complete_usage_with_an_unknown_token_count(self, ledger):
        tokens = _tokens(input_uncached=1000, output=200)
        tokens["cache_hit"] = None
        with pytest.raises(cost_ledger.IncompleteUsageMarkedComplete):
            cost_ledger.record_run(_run(tokens=tokens, usage_completeness="complete"), ledger_path=ledger)
        _assert_nothing_written(ledger)

    def test_a_run_id_of_another_project(self, ledger):
        with pytest.raises(cost_ledger.RunIdProjectMismatch):
            cost_ledger.record_run(
                _run(run_id="seocrawler-v2:dispatch:20261020-kp-x:1", project_id=PROJECT), ledger_path=ledger,
            )
        _assert_nothing_written(ledger)

    def test_a_model_the_registry_does_not_know(self, ledger):
        with pytest.raises(cost_ledger.UnknownModel):
            cost_ledger.record_run(_run(model="deepseek-v9-ultra"), ledger_path=ledger)
        _assert_nothing_written(ledger)

    def test_a_provider_the_registry_does_not_know(self, ledger):
        with pytest.raises(cost_ledger.UnknownModel):
            cost_ledger.record_run(_run(provider="litellm:openrouter", model="openai/gpt-4o-mini"),
                                   ledger_path=ledger)
        _assert_nothing_written(ledger)

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"role": "worker"}, id="unknown-role"),
            pytest.param({"kind": "refund"}, id="unknown-kind"),
            pytest.param({"billing": "free"}, id="unknown-billing"),
            pytest.param({"kind": "charge", "supersedes": "a" * 32, "reason": "x"}, id="charge-with-supersedes"),
            pytest.param({"cost_source": "estimate"}, id="unknown-cost-source"),
            pytest.param({"cost_source": "price_table@"}, id="price-table-without-version"),
            pytest.param({"cost_source": "unavailable:"}, id="unavailable-without-reason"),
            pytest.param({"cost_usd": None, "cost_source": "provider_reported"}, id="priced-source-without-amount"),
            pytest.param({"cost_usd": -0.1}, id="negative-amount"),
            pytest.param({"cost_usd": float("nan")}, id="nan-amount"),
            pytest.param({"api_equivalent_usd": 0.1}, id="metered-with-api-equivalent"),
            pytest.param({"provider": "claude", "model": "opus-5-5", "billing": "subscription",
                          "cost_usd": 0.6, "cost_source": "price_table@2026-09-23"},
                         id="subscription-with-a-marginal-cost"),
            pytest.param({"usage_completeness": "mostly"}, id="unknown-completeness"),
            pytest.param({"usage_completeness": "partial:cache_hit"}, id="partial-naming-a-known-field"),
            pytest.param({"usage_completeness": "unknown:kimi_cli_no_usage"}, id="unknown-with-known-tokens"),
            pytest.param({"tokens": {**_ZERO, "input_uncached": -1}}, id="negative-token-count"),
            pytest.param({"tokens": {**_ZERO, "input_uncached": 1.5}}, id="fractional-token-count"),
            pytest.param({"tokens": {**_ZERO, "reasoning": 3}}, id="unknown-token-field"),
            pytest.param({"occurred_from": "2026-10-20T08:00:00Z", "occurred_to": "2026-10-20T07:00:00Z"},
                         id="ends-before-it-starts"),
            pytest.param({"occurred_from": "2026-10-20 07:00"}, id="timestamp-without-zone"),
            pytest.param({"run_id": f"{PROJECT}:dispatch:20261020-kp-x:0"}, id="attempt-zero"),
            pytest.param({"run_id": f"{PROJECT}:dispatch:1"}, id="run-id-too-short"),
            pytest.param({"segments": [{"peak": True, "tokens": _tokens(input_uncached=999, output=200)}]},
                         id="segments-do-not-add-up"),
            pytest.param({"segments": [{"window": "dal", "tokens": _tokens(input_uncached=1000, output=200)}]},
                         id="segment-with-an-unknown-key"),
        ],
    )
    def test_a_malformed_record(self, ledger, overrides):
        with pytest.raises(cost_ledger.InvalidRecord):
            cost_ledger.record_run(_run(**overrides), ledger_path=ledger)
        _assert_nothing_written(ledger)

    def test_partial_naming_exactly_the_unknown_fields_is_accepted(self, ledger):
        tokens = _tokens(input_uncached=1000, output=200)
        tokens["cache_write_5m"] = None
        tokens["cache_write_1h"] = None
        record = cost_ledger.record_run(
            _run(tokens=tokens, usage_completeness="partial:cache_write_1h,cache_write_5m"), ledger_path=ledger,
        )
        assert _lines(ledger) == [record]


# ---------------------------------------------------------------------------
# The record and its price
# ---------------------------------------------------------------------------

class TestTheRecord:
    def test_the_record_carries_exactly_the_plan_fields(self, ledger):
        record = cost_ledger.record_run(_run(), ledger_path=ledger)
        assert set(record) == {
            "schema", "record_id", "run_id", "project_id", "kind", "occurred_from", "occurred_to",
            "recorded_at", "role", "provider", "model", "tokens", "usage_completeness",
            "cost_usd", "cost_source", "billing",
        }
        assert record["schema"] == "provider_cost_run/1"
        assert set(record["tokens"]) == {"input_uncached", "cache_hit", "cache_write_5m", "cache_write_1h", "output"}
        assert re.fullmatch(r"[0-9a-f]{32}", record["record_id"])
        assert _lines(ledger) == [record]

    def test_the_line_is_written_when_record_run_returns(self, ledger):
        """ADR-005: the caller mutates its state after record_run returns; the line is there by then."""
        record = cost_ledger.record_run(_run(), ledger_path=ledger)
        assert _lines(ledger)[-1]["record_id"] == record["record_id"]

    def test_the_default_ledger_is_the_one_provider_costs_resolves(self):
        record = cost_ledger.record_run(_run())
        assert _lines(provider_costs._resolve_costs_path()) == [record]

    def test_the_model_is_the_registry_key_not_the_callers_string(self, ledger):
        record = cost_ledger.record_run(
            _run(provider="claude", model="claude-opus-5-5", billing="subscription",
                 cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        assert record["model"] == "opus-5-5"

    def test_a_cli_model_argument_resolves_to_its_registry_key(self, ledger):
        record = cost_ledger.record_run(
            _run(provider="kimi", model="kimi-code/k3", billing="subscription", cost_usd=None,
                 cost_source="unavailable:kimi_cli_no_usage", tokens={k: None for k in _ZERO},
                 usage_completeness="unknown:kimi_cli_no_usage"),
            ledger_path=ledger,
        )
        assert record["model"] == "kimi-k3"

    def test_a_metered_run_is_priced_through_price_run_per_segment(self, ledger):
        """One peak and one off-peak segment: two tariffs, as ADR-040's price_run applies them."""
        segments = [
            {"peak": True, "tokens": _tokens(input_uncached=1_000_000, output=100_000)},
            {"peak": False, "tokens": _tokens(input_uncached=2_000_000, output=500_000)},
        ]
        record = cost_ledger.record_run(
            _run(tokens=_tokens(input_uncached=3_000_000, output=600_000), segments=segments,
                 cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        expected, source = provider_costs.price_run("deepseek-harness", "deepseek-v4-pro", [
            {"peak": True, "input": 1_000_000, "output": 100_000},
            {"peak": False, "input": 2_000_000, "output": 500_000},
        ])
        assert record["cost_usd"] == pytest.approx(expected)
        assert record["cost_usd"] == pytest.approx(1.716 + 2.31)
        assert record["cost_source"] == "price_table@" + source.rsplit("@", 1)[1]
        assert record["segments"] == segments

    def test_a_run_without_segments_is_priced_at_the_upper_bound(self, ledger):
        record = cost_ledger.record_run(
            _run(tokens=_tokens(input_uncached=1_000_000, output=100_000), cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        assert record["cost_usd"] == pytest.approx(1.716)

    def test_a_subscription_run_books_zero_and_the_api_equivalent(self, ledger):
        record = cost_ledger.record_run(
            _run(provider="claude", model="opus-5-5", billing="subscription",
                 tokens=_tokens(input_uncached=100_000, output=10_000), cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        assert record["billing"] == "subscription"
        assert record["cost_usd"] == 0.0
        assert record["api_equivalent_usd"] == pytest.approx(0.6)
        assert record["cost_source"] == "price_table@2026-09-23"

    def test_unknown_usage_gets_an_unavailable_source_not_a_zero(self, ledger):
        record = cost_ledger.record_run(
            _run(tokens={k: None for k in _ZERO}, usage_completeness="unknown:no_transcript",
                 cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        assert record["cost_usd"] is None
        assert record["cost_source"] == "unavailable:usage_unknown"

    @pytest.mark.parametrize("unknown", [("input_uncached", "output"), ("input_uncached",), ("output",)])
    def test_an_unknown_billable_count_is_unavailable_not_a_priced_zero(self, ledger, unknown):
        """codex_gate on 313f4bdc: input/output null and the cache counters 0 priced as cost_usd 0.0."""
        tokens = _tokens(input_uncached=1000, output=200)
        tokens.update(dict.fromkeys(unknown))
        completeness = "partial:" + ",".join(unknown)
        record = cost_ledger.record_run(
            _run(tokens=tokens, usage_completeness=completeness, cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        assert record["cost_usd"] is None
        assert record["cost_source"] == "unavailable:usage_unknown"
        assert record["usage_completeness"] == completeness
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.unavailable == (RUN_A1,)
        assert totals.cost_usd == 0.0

    def test_an_unknown_count_on_a_subscription_has_no_api_equivalent(self, ledger):
        record = cost_ledger.record_run(
            _run(provider="claude", model="opus-5-5", billing="subscription",
                 tokens=_tokens(input_uncached=100_000, output=None), usage_completeness="partial:output",
                 cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        assert (record["cost_usd"], record["api_equivalent_usd"]) == (0.0, None)
        assert record["cost_source"] == "unavailable:usage_unknown"
        assert cost_ledger.aggregate_runs(ledger).unavailable == (RUN_A1,)

    def test_an_unknown_cache_count_the_price_charges_is_unavailable(self, ledger):
        """deepseek-flash charges cache hits: an unknown cache_hit leaves the price unknown."""
        record = cost_ledger.record_run(
            _run(model="deepseek-flash", tokens=_tokens(input_uncached=1000, output=200, cache_hit=None),
                 usage_completeness="partial:cache_hit", cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        assert record["cost_usd"] is None
        assert record["cost_source"] == "unavailable:usage_unknown"

    def test_an_unknown_cache_count_the_price_does_not_charge_is_priced(self, ledger):
        """deepseek-v4-pro has no cache rate: an unknown cache_hit does not change its price."""
        record = cost_ledger.record_run(
            _run(tokens=_tokens(input_uncached=1_000_000, output=100_000, cache_hit=None),
                 usage_completeness="partial:cache_hit", cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        assert record["cost_usd"] == pytest.approx(1.716)
        assert record["cost_source"] == "price_table@2026-09-14"

    def test_a_real_zero_is_still_a_priced_zero(self, ledger):
        record = cost_ledger.record_run(
            _run(model="deepseek-flash", tokens=dict(_ZERO), cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        assert record["cost_usd"] == 0.0
        assert record["cost_source"] == "price_table@2026-09-14"
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.unavailable == ()
        assert totals.runs == {RUN_A1: record}

    def test_a_refused_spawn_is_a_line_with_zero_and_its_reason(self, ledger):
        record = cost_ledger.record_run(
            _run(run_id=RUN_A1, tokens=dict(_ZERO), cost_usd=0, cost_source="unavailable:spawn_refused"),
            ledger_path=ledger,
        )
        assert record["cost_usd"] == 0
        assert record["cost_source"] == "unavailable:spawn_refused"

    def test_a_correction_carries_supersedes_and_reason(self, ledger):
        first = cost_ledger.record_run(_run(), ledger_path=ledger)
        correction = cost_ledger.record_run(
            _run(kind="correction", supersedes=first["record_id"], reason="backfill: transcript cache hits",
                 cost_usd=0.9, backfill=True),
            ledger_path=ledger,
        )
        assert correction["supersedes"] == first["record_id"]
        assert correction["reason"] == "backfill: transcript cache hits"
        assert correction["backfill"] is True
        assert correction["record_id"] != first["record_id"]

    def test_run_id_for_builds_the_plan_shape(self):
        assert cost_ledger.run_id_for(PROJECT, "dispatch", "20261020-kp-x", 2) == RUN_A2
        with pytest.raises(cost_ledger.InvalidRecord):
            cost_ledger.run_id_for(PROJECT, "dispatch", "20261020-kp-x", 0)
        with pytest.raises(cost_ledger.InvalidRecord):
            cost_ledger.run_id_for("a:b", "dispatch", "20261020-kp-x", 1)


class TestTheModelIdentity:
    """provider_registry.resolve_model_identity: the ledger's identity is never a prefix guess."""

    def test_a_dated_snapshot_resolves_to_its_model(self):
        from providers import provider_registry

        assert provider_registry.resolve_model_identity("anthropic", "claude-opus-4-8-20260801") == "opus-4-8"
        assert provider_registry.resolve_model_identity("anthropic", "claude-opus-4-8-2026-08-01") == "opus-4-8"

    def test_a_longer_name_is_another_model_not_a_variant(self):
        from providers import provider_registry

        with pytest.raises(provider_registry.RegistryLookupError, match="sonnet-6"):
            provider_registry.resolve_model_identity("anthropic", "sonnet-6")
        # The price lookup's prefix match would have booked it as sonnet.
        assert provider_registry._resolve_model_key(
            provider_registry.load()["anthropic"], "anthropic", "sonnet-6") == "sonnet"

    def test_an_unknown_section_names_the_registry(self):
        from providers import provider_registry

        with pytest.raises(provider_registry.RegistryLookupError, match="openrouter.*wave7_models.yaml"):
            provider_registry.resolve_model_identity("openrouter", "openai/gpt-4o-mini")


# ---------------------------------------------------------------------------
# One aggregation function (item 3)
# ---------------------------------------------------------------------------

class TestTheAggregation:
    def test_a_correction_replaces_the_charge_of_its_run(self, ledger):
        first = cost_ledger.record_run(_run(cost_usd=0.5), ledger_path=ledger)
        cost_ledger.record_run(
            _run(kind="correction", supersedes=first["record_id"], reason="cache hits were missing",
                 cost_usd=1.25, tokens=_tokens(input_uncached=1000, cache_hit=7_218_944, output=200)),
            ledger_path=ledger,
        )
        totals = cost_ledger.aggregate_runs(ledger)
        assert list(totals.runs) == [RUN_A1]
        assert totals.cost_usd == pytest.approx(1.25)
        assert totals.tokens["cache_hit"] == 7_218_944
        assert totals.runs[RUN_A1]["kind"] == "correction"

    def test_two_attempts_of_one_dispatch_count_twice(self, ledger):
        cost_ledger.record_run(_run(run_id=RUN_A1, cost_usd=0.5), ledger_path=ledger)
        cost_ledger.record_run(_run(run_id=RUN_A2, cost_usd=0.75), ledger_path=ledger)
        totals = cost_ledger.aggregate_runs(ledger)
        assert sorted(totals.runs) == [RUN_A1, RUN_A2]
        assert totals.cost_usd == pytest.approx(1.25)
        assert totals.tokens["input_uncached"] == 2000

    def test_unavailable_is_listed_and_never_summed_as_zero(self, ledger):
        cost_ledger.record_run(_run(run_id=RUN_A1, cost_usd=0.5), ledger_path=ledger)
        cost_ledger.record_run(
            _run(run_id=RUN_B1, provider="kimi", model="kimi-k3", role="review", billing="subscription",
                 tokens={k: None for k in _ZERO}, usage_completeness="unknown:kimi_cli_no_usage",
                 cost_usd=None, cost_source="unavailable:kimi_cli_no_usage"),
            ledger_path=ledger,
        )
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.unavailable == (RUN_B1,)
        assert totals.cost_usd == pytest.approx(0.5)
        assert totals.tokens_unknown["input_uncached"] == 1
        assert totals.tokens["input_uncached"] == 1000

    def test_a_refused_spawn_is_listed_as_unavailable_too(self, ledger):
        cost_ledger.record_run(_run(tokens=dict(_ZERO), cost_usd=0, cost_source="unavailable:spawn_refused"),
                               ledger_path=ledger)
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.unavailable == (RUN_A1,)
        assert totals.cost_usd == 0

    def test_old_shape_lines_are_counted_not_summed(self, ledger, monkeypatch):
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: ledger)
        provider_costs.emit_provider_cost("deepseek-harness", "deepseek-v4-pro", 102_385, 64_922, 0.39,
                                          dispatch_id="20261019-rs-r1b-hygiene", project_id=PROJECT)
        cost_ledger.record_run(_run(cost_usd=0.5), ledger_path=ledger)
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.legacy_lines == 1
        assert totals.cost_usd == pytest.approx(0.5)
        assert list(totals.runs) == [RUN_A1]

    def test_unreadable_lines_are_counted(self, ledger):
        cost_ledger.record_run(_run(cost_usd=0.5), ledger_path=ledger)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps({"schema": "provider_cost_run/9", "run_id": RUN_A2, "cost_usd": 4.0}) + "\n")
            fh.write(json.dumps({"schema": "provider_cost_run/1", "cost_usd": 4.0}) + "\n")
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.unreadable_lines == 3
        assert totals.cost_usd == pytest.approx(0.5)

    def test_the_selection_applies_to_the_last_line_of_a_run(self, ledger):
        """A correction that moves a run out of the selection takes the run out, charge and all."""
        first = cost_ledger.record_run(_run(run_id=RUN_A1, role="builder", cost_usd=0.5), ledger_path=ledger)
        cost_ledger.record_run(
            _run(run_id=RUN_A1, role="review", kind="correction", supersedes=first["record_id"],
                 reason="role was the review seat", cost_usd=0.5),
            ledger_path=ledger,
        )
        cost_ledger.record_run(_run(run_id=RUN_A2, role="builder", cost_usd=0.75), ledger_path=ledger)
        totals = cost_ledger.aggregate_runs(ledger, select=lambda record: record["role"] == "builder")
        assert list(totals.runs) == [RUN_A2]
        assert totals.cost_usd == pytest.approx(0.75)

    def test_subscription_runs_sum_their_api_equivalent_apart(self, ledger):
        cost_ledger.record_run(_run(cost_usd=0.5), ledger_path=ledger)
        cost_ledger.record_run(
            _run(run_id=RUN_B1, provider="claude", model="opus-5-5", billing="subscription",
                 tokens=_tokens(input_uncached=100_000, output=10_000), cost_usd=None, cost_source=None),
            ledger_path=ledger,
        )
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.cost_usd == pytest.approx(0.5)
        assert totals.api_equivalent_usd == pytest.approx(0.6)

    def test_a_missing_ledger_is_an_empty_total(self, ledger):
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.runs == {} and totals.cost_usd == 0
        assert (totals.unavailable, totals.legacy_lines, totals.unreadable_lines) == ((), 0, 0)
        assert not ledger.exists(), "reading the ledger never creates it"

    def test_an_unreadable_ledger_is_an_error_not_an_empty_total(self, ledger):
        """Only a missing ledger is empty: one that cannot be read never sums to 0 unnoticed."""
        _break_the_ledger(ledger)
        with pytest.raises(IsADirectoryError):
            cost_ledger.aggregate_runs(ledger)

    def test_the_totals_carry_the_ledger_and_the_spool_fields(self, ledger):
        """The ledger and the spool backlog, read in one scope (K2a-3a)."""
        cost_ledger.record_run(_run(), ledger_path=ledger)
        totals = cost_ledger.aggregate_runs(ledger)
        assert [field.name for field in dataclasses.fields(totals)] == [
            "runs", "cost_usd", "api_equivalent_usd", "tokens", "tokens_unknown", "unavailable",
            "legacy_lines", "unreadable_lines", "spool_pending", "spool_unreadable", "spool_conflicts",
            "spool_quarantined", "torn_tail", "unlocked_read",
        ]
        assert (
            totals.spool_pending, totals.spool_unreadable, totals.spool_conflicts, totals.spool_quarantined,
            totals.torn_tail, totals.unlocked_read, totals.incomplete,
        ) == (0, False, 0, 0, False, False, None)


# ---------------------------------------------------------------------------
# A failed append lands in the spool (K2a-3a, ADR-043 §6)
# ---------------------------------------------------------------------------

def _break_the_ledger(ledger_path: Path) -> None:
    """A directory where the ledger file should be: every open fails with IsADirectoryError."""
    ledger_path.mkdir(parents=True)


class _RefusedFlock:
    """``cost_ledger.fcntl`` with every exclusive ``flock`` refused, as a filesystem without locks does."""

    def __getattr__(self, name):
        return getattr(fcntl, name)

    def flock(self, fd, operation):
        if operation & fcntl.LOCK_EX:
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
        return fcntl.flock(fd, operation)


_WHERE_THE_APPEND_FAILS = {"open": "spool", "lock": "spool", "append": "spool", "close": "ledger+spool"}


class TestAFailedAppendLandsInTheSpool:
    @pytest.mark.parametrize("where", sorted(_WHERE_THE_APPEND_FAILS))
    def test_record_run_spools_the_line_and_says_where_it_landed(
        self, ledger, inject, close_probe, monkeypatch, where,
    ):
        """The caller's call succeeds: the line is in the spool, and in the ledger too after a failed close."""
        if where == "open":
            _break_the_ledger(ledger)
        elif where == "lock":
            monkeypatch.setattr(cost_ledger, "fcntl", _RefusedFlock())
        elif where == "append":
            inject(ledger, "torn")
        else:
            close_probe(ledger, failing_close=[OSError(errno.EIO, os.strerror(errno.EIO))])

        record = cost_ledger.record_run(_run(), ledger_path=ledger)

        assert _spooled(ledger) == [record]
        assert sorted(path.name for path in _spool_dir(ledger).iterdir()) == [
            f"{RUN_A1}~{hashlib.sha256(RUN_A1.encode('utf-8')).hexdigest()[:16]}.json",
        ]
        assert record.landed == _WHERE_THE_APPEND_FAILS[where]
        assert not (ledger.parent.parent / "health").exists()

    def test_a_refused_lock_writes_nothing_to_the_ledger(self, ledger, monkeypatch):
        monkeypatch.setattr(cost_ledger, "fcntl", _RefusedFlock())
        record = cost_ledger.record_run(_run(), ledger_path=ledger)
        assert ledger.read_bytes() == b""
        assert _spooled(ledger) == [record]


# ---------------------------------------------------------------------------
# A torn append: the NDJSON boundary (round 2)
# ---------------------------------------------------------------------------

class _LedgerFault:
    """One fault on the next ledger line written through an append handle.

    ``torn`` puts the first half of the line on disk and raises ``ENOSPC``, as
    a full disk does halfway through a write. ``unterminated`` puts the whole
    JSON on disk without its final newline and raises ``ENOSPC``. Only a JSON
    line triggers it. After the fault the disk stays full for the rest of that
    call: every later write through the same handle, a bare newline included,
    raises ``ENOSPC``. With ``disk_stays_full=False`` the disk has room again
    right after the fault. The next call always finds room.
    """

    def __init__(self, ledger_path: Path, kind: str, disk_stays_full: bool):
        self.ledger_path = ledger_path
        self.kind = kind
        self.disk_stays_full = disk_stays_full
        self.armed = True


def _enospc() -> OSError:
    return OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


class _FaultyHandle:
    def __init__(self, fh, fault: _LedgerFault):
        self._fh = fh
        self._fault = fault
        self._full = False

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._fh.__exit__(*exc_info)

    def write(self, text):
        if self._full:
            raise _enospc()
        if not (self._fault.armed and text.startswith("{")):
            return self._fh.write(text)
        self._fault.armed = False
        self._fh.write(text[:-1] if self._fault.kind == "unterminated" else text[: len(text) // 2])
        self._fh.flush()
        self._full = self._fault.disk_stays_full
        raise _enospc()


@pytest.fixture
def inject(monkeypatch):
    """``inject(ledger_path, kind)`` arms one fault on every append handle of that ledger."""
    real_open = Path.open

    def arm(ledger_path: Path, kind: str, *, disk_stays_full: bool = True) -> _LedgerFault:
        fault = _LedgerFault(ledger_path, kind, disk_stays_full)

        def faulty_open(self, mode="r", *args, **kwargs):
            fh = real_open(self, mode, *args, **kwargs)
            if "a" in mode and self == fault.ledger_path:
                return _FaultyHandle(fh, fault)
            return fh

        monkeypatch.setattr(Path, "open", faulty_open)
        return fault

    return arm


def _line_bytes(record: dict) -> bytes:
    return (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")


class _FrozenEmitDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 19, 8, 15, 30, 123456, tzinfo=timezone.utc)


def _emit_an_old_line(ledger_path: Path, monkeypatch) -> None:
    """One line through the untouched old writer, ``emit_provider_cost``: ``_GOLDEN_EMIT_LINE``."""
    monkeypatch.setattr(provider_costs, "datetime", _FrozenEmitDatetime)
    monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: ledger_path)
    provider_costs.emit_provider_cost(
        "deepseek-harness", "deepseek-v4-pro", 102_385, 64_922, 0.39,
        dispatch_id="20261019-rs-r1b-hygiene", project_id="vnx-dev",
        metadata={"lane": "deepseek-harness"}, cost_source="deepseek-v4-pro@2026-09-14",
    )


class TestATornAppend:
    @pytest.mark.parametrize("kind", ["torn", "unterminated"])
    def test_a_record_after_a_torn_append_starts_on_its_own_line(self, ledger, inject, kind):
        """The disk stays full for the failed call, so its line stays open: the next append ends it first."""
        inject(ledger, kind)
        spooled = cost_ledger.record_run(_run(), ledger_path=ledger)
        assert spooled.landed == "spool"
        torn = ledger.read_bytes()
        assert torn and not torn.endswith(b"\n")

        other = cost_ledger.record_run(_run(run_id=RUN_B1), ledger_path=ledger)
        assert other.landed == "ledger"
        assert ledger.read_bytes() == torn + b"\n" + _line_bytes(other)
        totals = cost_ledger.aggregate_runs(ledger)
        # The unterminated append put the whole record in the ledger: its spooled copy waits for nothing (OI-2244).
        assert (totals.spool_pending, totals.torn_tail) == (1 if kind == "torn" else 0, False)
        if kind == "torn":
            assert totals.runs == {RUN_B1: other}
            assert totals.unreadable_lines == 1
        else:
            assert totals.runs == {RUN_A1: json.loads(torn), RUN_B1: other}
            assert totals.unreadable_lines == 0

    def test_a_whole_ledger_gets_no_extra_newline(self, ledger):
        first = cost_ledger.record_run(_run(), ledger_path=ledger)
        second = cost_ledger.record_run(_run(run_id=RUN_B1), ledger_path=ledger)
        assert ledger.read_bytes() == _line_bytes(first) + _line_bytes(second)

    @pytest.mark.parametrize("kind", ["torn", "unterminated"])
    def test_a_failed_append_ends_its_own_line_when_the_disk_has_room(self, ledger, inject, monkeypatch, kind):
        """The old writer cannot glue onto a line this module left open: the failed append ends it."""
        inject(ledger, kind, disk_stays_full=False)
        assert cost_ledger.record_run(_run(), ledger_path=ledger).landed == "spool"
        assert ledger.read_bytes().endswith(b"\n")
        left = ledger.read_bytes()

        _emit_an_old_line(ledger, monkeypatch)
        assert ledger.read_bytes() == left + _GOLDEN_EMIT_LINE.encode("utf-8")
        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.legacy_lines == 1
        if kind == "torn":
            assert (totals.runs, totals.unreadable_lines) == ({}, 1)
        else:
            assert (list(totals.runs), totals.unreadable_lines) == ([RUN_A1], 0)


# ---------------------------------------------------------------------------
# The ledger lock goes with the close (round 5)
# ---------------------------------------------------------------------------

def _lock_is_held(path: Path) -> bool:
    """True when another open file description holds ``path``'s ``flock``. A missing file holds no lock."""
    if not path.is_file():
        return False
    with path.open("r", encoding="utf-8") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe, fcntl.LOCK_UN)
        return False


class _CloseProbe:
    """The ledger's append handle; ``close`` notes whether the ledger lock is still held, and may fail once."""

    def __init__(self, fh, ledger_path: Path, held_at_close: list, failing_close: list):
        self._fh = fh
        self._ledger_path = ledger_path
        self._held_at_close = held_at_close
        self._failing_close = failing_close

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        if self._fh.closed:
            return
        self._held_at_close.append(_lock_is_held(self._ledger_path))
        self._fh.close()
        if self._failing_close:
            raise self._failing_close.pop()


@pytest.fixture
def close_probe(monkeypatch):
    """``close_probe(ledger_path, failing_close=[])``: wrap every append handle of that ledger in a :class:`_CloseProbe`.

    It wraps the ``Path.open`` in place when it is armed, so armed after
    ``inject`` it probes the close of the faulty handle.
    """

    def arm(ledger_path: Path, failing_close: list = ()) -> list:
        held_at_close: list = []
        failing = list(failing_close)
        real_open = Path.open

        def probing_open(self, mode="r", *args, **kwargs):
            fh = real_open(self, mode, *args, **kwargs)
            if "a" in mode and self == ledger_path:
                return _CloseProbe(fh, ledger_path, held_at_close, failing)
            return fh

        monkeypatch.setattr(Path, "open", probing_open)
        return held_at_close

    return arm


class TestTheLedgerLockGoesWithTheClose:
    def test_the_ledger_is_closed_while_its_lock_is_held(self, ledger, inject, close_probe):
        """A close flushes what a failed write left buffered: the writer closes under the lock, failed or not."""
        inject(ledger, "torn", disk_stays_full=False)
        held_at_close = close_probe(ledger)
        assert cost_ledger.record_run(_run(), ledger_path=ledger).landed == "spool"
        cost_ledger.record_run(_run(run_id=RUN_B1), ledger_path=ledger)
        assert held_at_close == [True, True]


# ---------------------------------------------------------------------------
# emit_provider_cost keeps its line byte for byte
# ---------------------------------------------------------------------------

_GOLDEN_EMIT_LINE = (
    '{"record_id":"37b593cb1ed7dbeaaeb6d24838bcf407","project_id":"vnx-dev","provider":"deepseek-harness",'
    '"model":"deepseek-v4-pro","input_tokens":102385,"output_tokens":64922,"cost_usd_estimate":0.39,'
    '"cost_source":"deepseek-v4-pro@2026-09-14","billing_mode":"metered",'
    '"dispatch_id":"20261019-rs-r1b-hygiene","timestamp":"2026-10-19T08:15:30.123456Z",'
    '"metadata":{"lane":"deepseek-harness"}}\n'
)


def test_emit_provider_cost_writes_the_golden_line(ledger, monkeypatch):
    """The old writer is untouched: same fields, same order, same record_id for the same instant."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 19, 8, 15, 30, 123456, tzinfo=timezone.utc)

    monkeypatch.setattr(provider_costs, "datetime", _FrozenDatetime)
    monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: ledger)
    provider_costs.emit_provider_cost(
        "deepseek-harness", "deepseek-v4-pro", 102_385, 64_922, 0.39,
        dispatch_id="20261019-rs-r1b-hygiene", project_id="vnx-dev",
        metadata={"lane": "deepseek-harness"}, cost_source="deepseek-v4-pro@2026-09-14",
    )
    assert ledger.read_text(encoding="utf-8") == _GOLDEN_EMIT_LINE
