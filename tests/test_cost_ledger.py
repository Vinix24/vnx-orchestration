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
  ``legacy_lines``, the spool count is reported;
- spool and replay: a failing append spools the record and reports the
  ``cost-ledger`` beacon degraded, replay is idempotent per ``run_id`` +
  ``record_id``, an unreadable spool file stays and is reported;
- ``vnx costs replay`` (script, pip CLI) and the dispatcher prelude tick;
- ``emit_provider_cost`` writes the same line as before (golden).

Failures are injected for real: a ledger path that is a directory makes the
append fail with ``IsADirectoryError``, exactly as an unwritable store would.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from argparse import Namespace
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

COSTS_CLI = REPO_ROOT / "scripts" / "costs_cli.py"
TICKS_SH = REPO_ROOT / "scripts" / "lib" / "dispatcher_supervisor_ticks.sh"
DISPATCHER_SH = REPO_ROOT / "scripts" / "dispatcher_minimal.sh"

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
    return ledger_path.parent / "provider_costs.spool"


def _beacon(ledger_path: Path) -> dict:
    path = ledger_path.parent.parent / "health" / "cost-ledger.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _lines(ledger_path: Path) -> list:
    if not ledger_path.is_file():
        return []
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_nothing_written(ledger_path: Path) -> None:
    assert _lines(ledger_path) == []
    assert not _spool_dir(ledger_path).exists() or not any(_spool_dir(ledger_path).glob("*.json"))


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
        assert totals.runs == {} and totals.cost_usd == 0 and totals.spool_pending == 0
        assert totals.incomplete is None


# ---------------------------------------------------------------------------
# Spool and replay (item 4)
# ---------------------------------------------------------------------------

def _break_the_ledger(ledger_path: Path) -> None:
    """A directory where the ledger file should be: every append fails with IsADirectoryError."""
    ledger_path.mkdir(parents=True)


def _repair_the_ledger(ledger_path: Path) -> None:
    ledger_path.rmdir()


class TestSpoolAndReplay:
    def test_a_failing_append_spools_the_record_and_reports_degraded(self, ledger):
        _break_the_ledger(ledger)
        record = cost_ledger.record_run(_run(), ledger_path=ledger)

        spooled = sorted(_spool_dir(ledger).glob("*.json"))
        assert [p.name for p in spooled] == [f"{RUN_A1}.json"]
        assert json.loads(spooled[0].read_text(encoding="utf-8")) == [record]
        beacon = _beacon(ledger)
        assert beacon["component"] == "cost-ledger"
        assert beacon["status"] == "stale"
        assert beacon["details"]["state"] == "degraded"
        assert beacon["details"]["spool_pending"] == 1

        totals = cost_ledger.aggregate_runs(ledger)
        assert totals.spool_pending == 1
        assert totals.incomplete == "onvolledig: 1 runs wachten op replay"

    def test_replay_brings_the_record_in_once(self, ledger):
        _break_the_ledger(ledger)
        record = cost_ledger.record_run(_run(), ledger_path=ledger)
        _repair_the_ledger(ledger)

        first = cost_ledger.replay_spool(ledger)
        assert first.written == 1
        assert first.removed == (f"{RUN_A1}.json",)
        assert first.pending == 0
        assert _lines(ledger) == [record]
        assert not any(_spool_dir(ledger).glob("*.json"))
        assert _beacon(ledger)["status"] == "ok"

        second = cost_ledger.replay_spool(ledger)
        assert second.written == 0
        assert _lines(ledger) == [record]

    def test_a_record_that_already_landed_is_not_written_again(self, ledger):
        """A crash between the append and the unlink leaves both; the replay writes nothing twice."""
        _break_the_ledger(ledger)
        record = cost_ledger.record_run(_run(), ledger_path=ledger)
        _repair_the_ledger(ledger)
        ledger.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")

        result = cost_ledger.replay_spool(ledger)
        assert result.written == 0
        assert result.already_present == 1
        assert result.removed == (f"{RUN_A1}.json",)
        assert _lines(ledger) == [record]

    def test_an_unreadable_spool_file_stays_and_is_reported(self, ledger):
        _spool_dir(ledger).mkdir(parents=True)
        broken = _spool_dir(ledger) / f"{RUN_B1}.json"
        broken.write_text("{not json", encoding="utf-8")
        _break_the_ledger(ledger)
        cost_ledger.record_run(_run(), ledger_path=ledger)
        _repair_the_ledger(ledger)

        result = cost_ledger.replay_spool(ledger)
        assert result.unreadable == (broken.name,)
        assert result.written == 1
        assert result.pending == 1
        assert broken.read_text(encoding="utf-8") == "{not json"
        assert _beacon(ledger)["status"] == "stale"
        assert _beacon(ledger)["details"]["unreadable"] == [broken.name]

    def test_a_later_record_of_a_spooled_run_queues_behind_it(self, ledger):
        """Last line wins only if the replayed charge lands before its correction."""
        _break_the_ledger(ledger)
        charge = cost_ledger.record_run(_run(cost_usd=0.5), ledger_path=ledger)
        _repair_the_ledger(ledger)
        correction = cost_ledger.record_run(
            _run(kind="correction", supersedes=charge["record_id"], reason="corrected", cost_usd=2.0),
            ledger_path=ledger,
        )
        assert _lines(ledger) == []

        cost_ledger.replay_spool(ledger)
        assert [line["record_id"] for line in _lines(ledger)] == [charge["record_id"], correction["record_id"]]
        assert cost_ledger.aggregate_runs(ledger).cost_usd == pytest.approx(2.0)

    def test_a_failing_spool_write_is_an_error_of_the_call(self, ledger):
        _break_the_ledger(ledger)
        _spool_dir(ledger).write_text("a file where the spool directory should be", encoding="utf-8")
        with pytest.raises(cost_ledger.CostLedgerSpoolError):
            cost_ledger.record_run(_run(), ledger_path=ledger)

    def test_a_replay_on_a_broken_ledger_raises_and_keeps_the_spool(self, ledger):
        _break_the_ledger(ledger)
        cost_ledger.record_run(_run(), ledger_path=ledger)
        with pytest.raises(OSError):
            cost_ledger.replay_spool(ledger)
        assert [p.name for p in _spool_dir(ledger).glob("*.json")] == [f"{RUN_A1}.json"]

    def test_a_replay_without_a_spool_does_nothing(self, ledger):
        result = cost_ledger.replay_spool(ledger)
        assert (result.written, result.removed, result.unreadable, result.pending) == (0, (), (), 0)
        assert not ledger.exists()


# ---------------------------------------------------------------------------
# vnx costs replay: the script, the pip CLI and bin/vnx
# ---------------------------------------------------------------------------

def _spool_one_run_in_the_default_store() -> tuple:
    default = provider_costs._resolve_costs_path()
    _break_the_ledger(default)
    record = cost_ledger.record_run(_run())
    _repair_the_ledger(default)
    return default, record


class TestTheCostsCommand:
    def test_the_script_replays_the_spool(self):
        default, record = _spool_one_run_in_the_default_store()
        proc = subprocess.run([sys.executable, str(COSTS_CLI), "replay", "--json"],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["written"] == 1
        assert _lines(default) == [record]

    def test_the_script_exits_one_on_an_unreadable_spool_file(self):
        default = provider_costs._resolve_costs_path()
        _spool_dir(default).mkdir(parents=True)
        (_spool_dir(default) / f"{RUN_B1}.json").write_text("[{]", encoding="utf-8")
        proc = subprocess.run([sys.executable, str(COSTS_CLI), "replay"],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 1
        assert f"{RUN_B1}.json" in proc.stdout

    def test_the_script_exits_two_when_the_ledger_cannot_be_written(self):
        default = provider_costs._resolve_costs_path()
        _break_the_ledger(default)
        cost_ledger.record_run(_run())
        proc = subprocess.run([sys.executable, str(COSTS_CLI), "replay"],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 2
        assert "replay failed" in proc.stderr

    def test_the_pip_cli_runs_the_same_script(self):
        import vnx_cli.commands.costs as costs_cli

        default, record = _spool_one_run_in_the_default_store()
        assert costs_cli.resolve_costs_script(REPO_ROOT) == COSTS_CLI
        rc = costs_cli.vnx_costs(Namespace(costs_args=["replay"]))
        assert rc == 0
        assert _lines(default) == [record]

    def test_the_pip_cli_registers_costs(self):
        import argparse

        from vnx_cli.commands.costs import register_costs_subparser

        parser = argparse.ArgumentParser(prog="vnx")
        register_costs_subparser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["costs", "replay", "--json"])
        assert args.costs_args == ["replay", "--json"]

    def test_a_spawn_failure_is_a_defined_error(self, tmp_path, monkeypatch, capsys):
        import vnx_cli.commands.costs as costs_cli

        script = tmp_path / "costs_cli.py"
        script.write_text("", encoding="utf-8")
        monkeypatch.setattr(costs_cli, "resolve_costs_script", lambda root=None: script)

        def broken(*_args, **_kwargs):
            raise OSError("spawn failed")

        monkeypatch.setattr(costs_cli.subprocess, "run", broken)
        assert costs_cli.vnx_costs(Namespace(costs_args=["replay"])) == costs_cli._EXIT_ERROR
        assert f"could not start {script}" in capsys.readouterr().err

    def test_bin_vnx_delegates_costs_to_the_script(self):
        text = (REPO_ROOT / "bin" / "vnx").read_text(encoding="utf-8")
        assert re.search(r'^\s+costs\)\n\s+"\$VNX_PYTHON" "\$VNX_HOME/scripts/costs_cli\.py" "\$@"', text, re.M)
        import vnx_mode

        assert "costs" in vnx_mode.TIER_STARTER_OPERATOR


# ---------------------------------------------------------------------------
# The dispatcher prelude tick
# ---------------------------------------------------------------------------

def _tick_env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    data_dir = Path(env["VNX_DATA_DIR"])
    env["VNX_LOGS_DIR"] = str(tmp_path / "logs")
    env["VNX_DIR"] = str(REPO_ROOT)
    env["SCRIPT_DIR"] = str(REPO_ROOT / "scripts")
    env["STATE_DIR"] = str(data_dir / "state")
    env["VNX_COST_LEDGER_REPLAY_INTERVAL_SEC"] = "0"
    env["VNX_LOG_CAPTURE"] = str(tmp_path / "log_capture.txt")
    env.pop("VNX_SUPERVISOR_MODE", None)
    return env


def _run_tick(env: dict) -> subprocess.CompletedProcess:
    script = textwrap.dedent(f"""\
        set -euo pipefail
        log() {{ printf '[log] %s\\n' "$*" >> "$VNX_LOG_CAPTURE"; }}
        source "{TICKS_SH}"
        _maybe_cost_ledger_replay
        echo "SURVIVED:$?"
        """)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)


class TestThePreludeTick:
    def test_the_tick_replays_the_spool(self, tmp_path):
        default, record = _spool_one_run_in_the_default_store()
        proc = _run_tick(_tick_env(tmp_path))
        assert proc.returncode == 0, proc.stderr
        assert "SURVIVED:0" in proc.stdout
        assert _lines(default) == [record]
        assert not any(_spool_dir(default).glob("*.json"))

    def test_the_tick_survives_a_failing_replay_and_leaves_the_spool(self, tmp_path):
        default = provider_costs._resolve_costs_path()
        _break_the_ledger(default)
        cost_ledger.record_run(_run())
        env = _tick_env(tmp_path)
        proc = _run_tick(env)
        assert proc.returncode == 0, proc.stderr
        assert "SURVIVED:0" in proc.stdout
        assert [p.name for p in _spool_dir(default).glob("*.json")] == [f"{RUN_A1}.json"]
        assert "cost-ledger replay failed" in Path(env["VNX_LOG_CAPTURE"]).read_text(encoding="utf-8")

    def test_the_tick_is_throttled(self, tmp_path):
        default, _record = _spool_one_run_in_the_default_store()
        env = _tick_env(tmp_path)
        env["VNX_COST_LEDGER_REPLAY_INTERVAL_SEC"] = "3600"
        state_file = Path(env["VNX_DATA_DIR"]) / "state" / ".last_cost_ledger_replay_ts"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(str(int(datetime.now(timezone.utc).timestamp())), encoding="utf-8")
        proc = _run_tick(env)
        assert "SURVIVED:0" in proc.stdout
        assert _lines(default) == []

    def test_process_dispatches_calls_the_replay_tick(self):
        source = DISPATCHER_SH.read_text(encoding="utf-8")
        match = re.search(r"^process_dispatches\(\) \{\n.*?^\}\n", source, re.MULTILINE | re.DOTALL)
        assert match, "could not locate process_dispatches() in dispatcher_minimal.sh"
        assert "_maybe_cost_ledger_replay" in match.group(0)


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
