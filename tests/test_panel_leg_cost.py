#!/usr/bin/env python3
"""Tests for the panel's cost-ledger lines (K4a, ADR-043 §1): one line per panel leg.

No network, no model call: every seat is a stub dispatcher returning a unified-report
text in the shape a lane hands back (frontmatter + body), so the usage a leg reports —
an OpenRouter response's ``usage.cost``, plain token counts, or none at all — is test
data here, never a live number.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import cost_ledger  # noqa: E402
import deliberation_panel as dp  # noqa: E402
import provider_costs  # noqa: E402

# The models are the shipped panel seats, so every one of them is in the model registry
# (ADR-036): a leg is priced, never refused for an unknown model.
GLM = ("glm-harness", "glm-5.2")
DEEPSEEK = ("deepseek-harness", "deepseek-v4-pro")
CLAUDE = ("claude", "sonnet")


@pytest.fixture(autouse=True)
def _no_ambient_receipts(monkeypatch, tmp_path):
    """Pin the default receipt ledger to a path that never exists, as the panel tests do."""
    monkeypatch.setattr(dp, "_default_receipts_path", lambda: tmp_path / "absent" / "t0_receipts.ndjson")


def _report(provider, dispatch_id, *, tokens=None, cost_usd=None, cost_source=None,
            openrouter_cost=None, measured=None, exit_code=0, body="The analysis."):
    """A leg's unified report: the frontmatter a lane publishes plus its body.

    ``openrouter_cost`` writes the provider's OWN usage mapping (OpenRouter returns
    ``usage.cost`` beside its token counts); ``cost_usd``/``cost_source`` write ADR-040's
    pair instead. ``tokens`` is ``(input, output, cache_read)`` or None for a report with
    no measurement at all.
    """
    lines = ["---", "schema_version: 1", f"dispatch_id: {dispatch_id}",
             f"provider: {provider}", "model: sonnet", "role: deliberation-panelist",
             f"exit_code: {exit_code}"]
    if tokens is not None:
        input_tokens, output_tokens, cache_read = tokens
        lines += ["token_usage:", f"  input: {input_tokens}", f"  output: {output_tokens}",
                  f"  cache_read: {cache_read}"]
    if measured is not None:
        lines.append(f"token_usage_measured: {'true' if measured else 'false'}")
    if cost_usd is not None:
        lines.append(f"cost_usd: {cost_usd}")
    if cost_source is not None:
        lines.append(f"cost_source: {cost_source}")
    if openrouter_cost is not None:
        lines += ["usage:", f"  cost: {openrouter_cost}", "  prompt_tokens: 1200",
                  "  completion_tokens: 400"]
    return "\n".join(lines + ["---", "", body])


class _Stub:
    """A dispatcher over fixed per-provider reports; it can also leave a receipt behind."""

    def __init__(self, reports, *, receipts_path=None, booked=None, raises=None):
        self.reports = reports
        self.receipts_path = receipts_path
        self.booked = booked or {}
        self.raises = raises or set()
        self.calls = []

    def __call__(self, provider, model, prompt, dispatch_id):
        self.calls.append({"provider": provider, "model": model, "dispatch_id": dispatch_id})
        if provider in self.raises:
            raise RuntimeError(f"{provider} lane is down")
        if self.receipts_path is not None and provider in self.booked:
            # The lane books its own line and writes the receipt BEFORE the dispatcher
            # returns, exactly as provider_dispatch does (K2b's cost_ledger_run_ids).
            with self.receipts_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "dispatch_id": dispatch_id, "status": "success",
                    "cost_ledger_run_ids": self.booked[provider],
                }) + "\n")
        return self.reports.get(provider, "")


def _ledger_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(tmp_path, seats, stub, *, min_seats=None, receipts_path=None, panel_run_id="panel-sweep-k4a"):
    ledger = tmp_path / "events" / "provider_costs.ndjson"
    result = dp.run_deliberation(
        "sweep", "audit src/", dispatcher=stub, roster=seats, max_workers=len(seats),
        min_seats=min_seats, receipts_path=receipts_path or tmp_path / "absent" / "t0_receipts.ndjson",
        ledger_path=ledger, project_id="vnx-dev", panel_run_id=panel_run_id,
    )
    return result, ledger


def _one_leg(tmp_path, seat, report, **kwargs):
    provider = seat[0]
    stub = _Stub({provider: report}, **kwargs)
    result, ledger = _run(tmp_path, [seat], stub, min_seats=2)
    return result, _ledger_lines(ledger)


class TestOneLinePerLeg:
    def test_three_legs_write_three_lines_with_their_own_producer_and_attempt(self, tmp_path):
        reports = {p: _report(p, "unused", tokens=(1200, 400, 0)) for p, _ in (GLM, DEEPSEEK, CLAUDE)}
        stub = _Stub(reports)
        result, ledger = _run(tmp_path, [GLM, DEEPSEEK, CLAUDE], stub, min_seats=4)
        lines = _ledger_lines(ledger)

        assert len(stub.calls) == 3 and len(result.fan_out) == 3
        assert len(lines) == 3, "one model call is one run in the ledger"
        assert len({line["run_id"] for line in lines}) == 3
        for line in lines:
            assert line["role"] == "panel"
            assert line["project_id"] == "vnx-dev"
            assert line["kind"] == "charge"
            assert cost_ledger.split_run_id(line["run_id"])[2] == result.panel_run_id
            assert cost_ledger.split_run_id(line["run_id"])[3] == 1
        assert {line["provider"] for line in lines} == {"glm-harness", "deepseek-harness", "claude"}
        assert {cost_ledger.split_run_id(line["run_id"])[1] for line in lines} == {
            "panel-diverge-glm-harness", "panel-diverge-deepseek-harness", "panel-diverge-claude"}
        assert sorted(result.cost_ledger_run_ids) == sorted(line["run_id"] for line in lines)
        # Each line carries the tokens its own leg reported.
        assert all(line["tokens"]["input_uncached"] == 1200 for line in lines)
        assert all(line["tokens"]["output"] == 400 for line in lines)

    def test_a_bare_panel_run_books_three_lines_through_the_default_ledger(self, tmp_path, monkeypatch):
        """As an operator runs it: no cost wiring at the call site, three legs, three lines."""
        ledger = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: ledger)
        reports = {p: _report(p, "unused", tokens=(1200, 400, 0)) for p, _ in (GLM, DEEPSEEK, CLAUDE)}
        dp.run_deliberation(
            "sweep", "audit src/", dispatcher=_Stub(reports), roster=[GLM, DEEPSEEK, CLAUDE],
            max_workers=3, min_seats=4, receipts_path=tmp_path / "absent" / "t0_receipts.ndjson",
        )
        lines = _ledger_lines(ledger)
        assert len(lines) == 3
        assert {line["role"] for line in lines} == {"panel"}
        assert len({line["run_id"] for line in lines}) == 3

    def test_a_retried_leg_is_the_next_attempt_not_an_overwrite(self, tmp_path):
        import panel_leg_cost as plc

        ledger = tmp_path / "events" / "provider_costs.ndjson"
        booker = plc.LegBooker(ref="panel-sweep-retry", project_id="vnx-dev", ledger_path=ledger)
        report = _report("glm-harness", "d", tokens=(10, 5, 0))
        for _ in range(2):
            booker.book(stage="contrarian", provider="glm-harness", model=GLM[1], dispatch_id="d",
                        report_text=report, started=_started(), ended=_started())
        lines = _ledger_lines(ledger)
        assert [cost_ledger.split_run_id(line["run_id"])[3] for line in lines] == [1, 2]
        assert {cost_ledger.split_run_id(line["run_id"])[1] for line in lines} == {"panel-contrarian-glm-harness"}
        assert len(booker.run_ids) == 2


def _started():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


class TestWhereTheAmountComesFrom:
    def test_a_reported_openrouter_cost_is_provider_reported(self, tmp_path):
        report = _report(GLM[0], "d", tokens=(1200, 400, 0), openrouter_cost=0.00123)
        _, lines = _one_leg(tmp_path, GLM, report)
        assert len(lines) == 1
        assert lines[0]["cost_source"] == "provider_reported"
        assert lines[0]["cost_usd"] == pytest.approx(0.00123)

    def test_the_adr040_pair_is_provider_reported_too(self, tmp_path):
        report = _report(GLM[0], "d", tokens=(1200, 400, 0),
                         cost_usd=0.00042, cost_source="provider_reported")
        _, lines = _one_leg(tmp_path, GLM, report)
        assert lines[0]["cost_source"] == "provider_reported"
        assert lines[0]["cost_usd"] == pytest.approx(0.00042)

    def test_the_three_sources_side_by_side_in_one_run(self, tmp_path, monkeypatch):
        """Acceptance 2 in one panel run: reported, tokens-only, and no usage at all."""
        ledger = tmp_path / "events" / "provider_costs.ndjson"
        monkeypatch.setattr(provider_costs, "_resolve_costs_path", lambda: ledger)
        reports = {
            GLM[0]: _report(GLM[0], "d", tokens=(1200, 400, 0), openrouter_cost=0.00123),
            DEEPSEEK[0]: _report(DEEPSEEK[0], "d", tokens=(1200, 400, 0)),
            CLAUDE[0]: _report(CLAUDE[0], "d"),
        }
        dp.run_deliberation(
            "sweep", "audit src/", dispatcher=_Stub(reports), roster=[GLM, DEEPSEEK, CLAUDE],
            max_workers=3, min_seats=4, receipts_path=tmp_path / "absent" / "t0_receipts.ndjson",
        )
        by_provider = {line["provider"]: line for line in _ledger_lines(ledger)}
        assert set(by_provider) == {GLM[0], DEEPSEEK[0], CLAUDE[0]}
        assert by_provider[GLM[0]]["cost_source"] == "provider_reported"
        assert by_provider[GLM[0]]["cost_usd"] == pytest.approx(0.00123)
        assert by_provider[DEEPSEEK[0]]["cost_source"].startswith("price_table@")
        assert by_provider[DEEPSEEK[0]]["cost_usd"] > 0
        assert by_provider[CLAUDE[0]]["cost_source"] == "unavailable:no_usage"
        assert set(by_provider[CLAUDE[0]]["tokens"].values()) == {None}
        # claude bills on the subscription: its cost_usd is 0 by ADR-043 §1, and the run is
        # in no sum because its cost_source is unavailable. The metered lane's None is
        # asserted in test_a_leg_without_usage_is_unavailable_never_zero.
        assert by_provider[CLAUDE[0]]["cost_usd"] == 0.0
        assert by_provider[CLAUDE[0]]["api_equivalent_usd"] is None

    def test_a_leg_without_a_reported_amount_is_priced_from_the_register(self, tmp_path):
        report = _report(GLM[0], "d", tokens=(1200, 400, 0))
        _, lines = _one_leg(tmp_path, GLM, report)
        assert lines[0]["cost_source"].startswith("price_table@")
        assert lines[0]["cost_usd"] > 0
        assert lines[0]["tokens"]["input_uncached"] == 1200

    def test_a_register_stamped_cost_in_the_report_is_not_copied_onto_the_line(self, tmp_path):
        # provider_dispatch stamps its own price_table guess; the ledger prices from the
        # register itself, so the lane's number is not taken as the provider's own report.
        report = _report(GLM[0], "d", tokens=(1200, 400, 0),
                         cost_usd=99.0, cost_source="price_table@2026-09-14")
        _, lines = _one_leg(tmp_path, GLM, report)
        assert lines[0]["cost_source"].startswith("price_table@")
        assert lines[0]["cost_usd"] != pytest.approx(99.0)

    def test_a_leg_without_usage_is_unavailable_never_zero(self, tmp_path):
        _, lines = _one_leg(tmp_path, GLM, _report(GLM[0], "d"))
        assert len(lines) == 1
        assert lines[0]["cost_source"] == "unavailable:no_usage"
        assert lines[0]["cost_usd"] is None
        assert set(lines[0]["tokens"].values()) == {None}
        assert lines[0]["usage_completeness"] == "unknown:no_usage"

    def test_an_unmeasured_report_is_no_usage_not_the_zeros_beside_it(self, tmp_path):
        # kimi-cli publishes token_usage: 0 with token_usage_measured: false; those zeros
        # are the lane saying it did not measure, so they are not a measurement.
        report = _report("kimi", "d", tokens=(0, 0, 0), measured=False)
        _, lines = _one_leg(tmp_path, ("kimi", "kimi-k3"), report)
        assert lines[0]["cost_source"] == "unavailable:no_usage"
        assert set(lines[0]["tokens"].values()) == {None}

    def test_a_reported_amount_on_a_subscription_lane_is_its_list_price(self, tmp_path):
        report = _report(CLAUDE[0], "d", tokens=(1200, 400, 0), openrouter_cost=0.012)
        _, lines = _one_leg(tmp_path, CLAUDE, report)
        assert lines[0]["billing"] == "subscription"
        assert lines[0]["cost_source"] == "provider_reported"
        assert lines[0]["cost_usd"] == 0.0
        assert lines[0]["api_equivalent_usd"] == pytest.approx(0.012)

    def test_a_leg_whose_dispatch_raised_books_unavailable_dispatch_error(self, tmp_path):
        stub = _Stub({GLM[0]: ""}, raises={GLM[0]})
        result, ledger = _run(tmp_path, [GLM], stub, min_seats=2)
        assert "dispatch error" in result.fan_out[0]["text"]
        lines = _ledger_lines(ledger)
        assert len(lines) == 1
        assert lines[0]["cost_source"] == "unavailable:dispatch_error"
        assert set(lines[0]["tokens"].values()) == {None}

    def test_a_codex_input_count_is_split_against_its_cache_read(self, tmp_path):
        # OI-2407: a codex input count INCLUDES the cached part, so booking it whole as
        # uncached would pay for the cache read twice.
        report = _report("codex", "d", tokens=(1000, 100, 600))
        _, lines = _one_leg(tmp_path, ("codex", "gpt-5.5"), report)
        assert lines[0]["tokens"]["input_uncached"] == 400
        assert lines[0]["tokens"]["cache_hit"] == 600


class TestDoubleBooking:
    def test_a_leg_its_lane_already_booked_is_not_booked_again(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        receipts.write_text("", encoding="utf-8")
        already = "vnx-dev:builder:panel-sweep-diverge-0-abc123:1"
        reports = {p: _report(p, "d", tokens=(1200, 400, 0)) for p, _ in (GLM, DEEPSEEK)}
        stub = _Stub(reports, receipts_path=receipts, booked={GLM[0]: [already]})
        result, ledger = _run(tmp_path, [GLM, DEEPSEEK], stub, min_seats=3, receipts_path=receipts)
        lines = _ledger_lines(ledger)

        assert len(stub.calls) == 2 and len(result.fan_out) == 2
        assert len(lines) == 1, "the leg the lane already booked is not booked a second time"
        assert lines[0]["provider"] == DEEPSEEK[0]
        assert all(already not in line["run_id"] for line in lines)
        assert result.cost_ledger_run_ids == [lines[0]["run_id"]]

    @pytest.mark.parametrize("field", [None, [], "vnx-dev:builder:x:1", [0], [""]])
    def test_only_a_non_empty_list_of_ids_counts_as_evidence(self, field):
        import panel_leg_cost as plc

        receipt = {} if field is None else {"cost_ledger_run_ids": field}
        assert plc.already_booked_run_ids([receipt]) == []


class TestBookingNeverBreaksThePanel:
    def test_an_oserror_from_the_ledger_leaves_the_panel_intact_and_logs_one_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        def boom(*args, **kwargs):
            raise cost_ledger.CostLedgerWriteError("simulated: the ledger and its spool are gone")

        monkeypatch.setattr(cost_ledger, "record_next_attempt", boom)
        caplog.set_level(logging.DEBUG, logger="panel_leg_cost")
        report = _report(GLM[0], "d", tokens=(1200, 400, 0))
        result, lines = _one_leg(tmp_path, GLM, report)

        warnings = [r for r in caplog.records if r.name == "panel_leg_cost" and r.levelno == logging.WARNING]
        assert len(warnings) == 1, "one leg, one booking failure, one WARNING"
        message = warnings[0].getMessage()
        assert result.panel_run_id in message and GLM[0] in message and "diverge" in message
        assert "CostLedgerWriteError" in message
        # The panel carried on: the leg's text is untouched and the run has a result.
        assert result.fan_out[0]["text"] == report
        assert result.cost_ledger_run_ids == [] and lines == []

    def test_a_refused_record_logs_one_warning_and_no_line(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="panel_leg_cost")
        # kimi/k2 is not in the model registry: the writer refuses it (ADR-036).
        _, lines = _one_leg(tmp_path, ("kimi", "k2"), _report("kimi", "d", tokens=(10, 5, 0)))
        refused = [r for r in caplog.records if r.name == "panel_leg_cost" and "UnknownModel" in r.getMessage()]
        assert lines == [] and len(refused) == 1
