"""tests/test_k5c_meetpoort.py — K5c: the daily ceiling reads the cost ledger once the gate passes (ADR-043 §8).

Covers ``scripts/lib/cost_ledger_meetpoort.py`` and the switch it drives in
``scripts/lib/paid_lane_budget.py``, against ledgers written in ``tmp_path``
through the ledger's own writer (``cost_ledger.record_run`` / ``build_record``)
— never into the real store:

- the gate: five roles rolled out at least seven full UTC days ago and a clean
  window passes; a missing role, a role that arrived too late, and a metered
  ``unavailable:*`` line in the window shut it, with the evidence named;
- the two documented exceptions (kimi without a usage source, a refused spawn)
  and the line that is explicitly not one (``no_transcript``);
- the switch: the ledger sum once the gate passes (a correction counts once, an
  unavailable run is unknown, never 0), the receipts sum otherwise;
- fail-closed: an unreadable ledger and a run waiting in the spool both keep
  the receipts and say why;
- the door: one warning per call while the gate has not passed, a refusal on
  the ledger sum once it has.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

import cost_ledger  # noqa: E402
import cost_ledger_meetpoort as meetpoort  # noqa: E402
import paid_lane_budget as plb  # noqa: E402

PROJECT = "vnx-dev"
TODAY = date(2026, 10, 28)
LONG_AGO = TODAY - timedelta(days=30)
_ZERO = {"input_uncached": 0, "cache_hit": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 0}


def _tokens(**values):
    tokens = dict(_ZERO)
    tokens.update(values)
    return tokens


def _run(run_id: str, day: date = LONG_AGO, *, role: str = "builder", provider: str = "deepseek-harness",
         model: str = "deepseek-v4-pro", billing: str = "metered", cost_usd: float | None = 0.5,
         cost_source: str | None = "provider_reported", **overrides) -> "cost_ledger.RunCost":
    fields = dict(
        run_id=run_id,
        project_id=PROJECT,
        role=role,
        provider=provider,
        model=model,
        occurred_from=f"{day.isoformat()}T07:00:00Z",
        occurred_to=f"{day.isoformat()}T07:30:00Z",
        tokens=_tokens(input_uncached=1000, output=200),
        usage_completeness="complete",
        billing=billing,
        cost_usd=cost_usd,
        cost_source=cost_source,
    )
    fields.update(overrides)
    return cost_ledger.RunCost(**fields)


@pytest.fixture
def store(tmp_path):
    """The store of a state dir: ledger in ``events/``, receipts in ``state/`` (the layout of every store)."""
    data_dir = tmp_path / "store"
    (data_dir / "state").mkdir(parents=True)
    return data_dir


@pytest.fixture
def ledger(store):
    return store / "events" / "provider_costs.ndjson"


def _book(ledger: Path, run: "cost_ledger.RunCost"):
    return cost_ledger.record_run(run, ledger_path=ledger)


def _rolled_out(ledger: Path, *, first_day: date = LONG_AGO, roles=meetpoort.REQUIRED_ROLES) -> None:
    """One dated line per role: the rollout the gate measures, all out of the window."""
    for index, role in enumerate(roles):
        _book(ledger, _run(f"{PROJECT}:dispatch:rollout-{index}:1", first_day, role=role))


def _write_receipts(store: Path, receipts: list) -> None:
    with (store / "state" / plb.RECEIPTS_FILE_NAME).open("w", encoding="utf-8") as fh:
        for receipt in receipts:
            fh.write(json.dumps(receipt) + "\n")


def _today_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00Z")


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# The gate: the rollout
# ---------------------------------------------------------------------------

class TestTheRollout:
    def test_all_five_roles_older_than_seven_days_and_a_clean_window_pass(self, ledger):
        _rolled_out(ledger)
        gate = meetpoort.evaluate(ledger, TODAY)
        assert gate.passed is True
        assert gate.missing_roles == ()
        assert gate.offenders == ()
        assert gate.window == ((TODAY - timedelta(days=7)).isoformat(), (TODAY - timedelta(days=1)).isoformat())

    def test_a_first_line_exactly_seven_days_old_still_passes(self, ledger):
        """Seven full UTC days means the first day of the window; it still covers the whole window."""
        _rolled_out(ledger, first_day=TODAY - timedelta(days=7))
        assert meetpoort.evaluate(ledger, TODAY).passed is True

    def test_a_role_with_no_line_is_missing_and_named(self, ledger):
        _rolled_out(ledger, roles=tuple(r for r in meetpoort.REQUIRED_ROLES if r != "panel"))
        gate = meetpoort.evaluate(ledger, TODAY)
        assert gate.passed is False
        assert gate.missing_roles == ("panel",)
        assert "panel" in gate.reason

    def test_a_role_whose_first_line_is_six_days_old_fails(self, ledger):
        _rolled_out(ledger, roles=tuple(r for r in meetpoort.REQUIRED_ROLES if r != "panel"))
        _book(ledger, _run(f"{PROJECT}:dispatch:late-panel:1", TODAY - timedelta(days=6), role="panel"))
        gate = meetpoort.evaluate(ledger, TODAY)
        assert gate.passed is False
        assert "panel" not in gate.missing_roles, "the role is present, only its rollout is too recent"
        assert "panel" in gate.reason

    def test_a_role_with_only_an_undated_line_is_not_measured(self, ledger):
        """A line that carries no usable date proves no rollout: fail-closed, the role stays missing.

        The writer refuses an unparseable ``occurred_from``, so the malformed line
        is written by hand — exactly the hand-edited-line shape the gate must survive.
        """
        _rolled_out(ledger, roles=tuple(r for r in meetpoort.REQUIRED_ROLES if r != "review"))
        record = dict(cost_ledger.build_record(_run(f"{PROJECT}:dispatch:undated:1", role="review")),
                      occurred_from="not a timestamp")
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        gate = meetpoort.evaluate(ledger, TODAY)
        assert gate.passed is False
        assert "review" in gate.missing_roles

    def test_no_ledger_at_all_is_a_shut_gate_without_creating_one(self, store):
        path = store / "events" / "provider_costs.ndjson"
        gate = meetpoort.evaluate(path, TODAY)
        assert gate.passed is False
        assert gate.missing_roles == meetpoort.REQUIRED_ROLES
        assert not (store / "events").exists(), "a store without a ledger is not opened for writing"


# ---------------------------------------------------------------------------
# The gate: the window
# ---------------------------------------------------------------------------

class TestTheWindow:
    def test_an_unavailable_metred_line_in_the_window_fails_and_is_listed(self, ledger):
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:gap:1", TODAY - timedelta(days=3),
                           cost_usd=None, cost_source="unavailable:no_transcript"))
        gate = meetpoort.evaluate(ledger, TODAY)
        assert gate.passed is False
        assert gate.offenders == (meetpoort.Offender("deepseek-harness", "unavailable:no_transcript", 1),)
        assert "no_transcript" in gate.reason

    def test_the_same_line_eight_days_back_does_not_count(self, ledger):
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:gap:1", TODAY - timedelta(days=8),
                           cost_usd=None, cost_source="unavailable:no_transcript"))
        gate = meetpoort.evaluate(ledger, TODAY)
        assert gate.passed is True
        assert gate.offenders == ()

    def test_a_kimi_line_without_a_usage_source_is_the_documented_exception(self, ledger):
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:kimi:1", TODAY - timedelta(days=2), provider="kimi",
                           model="kimi-k3", cost_usd=None,
                           cost_source=meetpoort.KIMI_NO_USAGE_SOURCE))
        assert meetpoort.evaluate(ledger, TODAY).passed is True

    def test_a_kimi_capture_gap_is_not_the_exception(self, ledger):
        """The exception is the kimi CLI having no usage source, never a gap on a kimi line."""
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:kimi:1", TODAY - timedelta(days=2), provider="kimi",
                           model="kimi-k3", cost_usd=None, cost_source="unavailable:no_transcript"))
        gate = meetpoort.evaluate(ledger, TODAY)
        assert gate.passed is False
        assert gate.offenders == (meetpoort.Offender("kimi", "unavailable:no_transcript", 1),)

    def test_a_refused_spawn_is_a_measured_zero_and_not_an_offender(self, ledger):
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:refused:1", TODAY - timedelta(days=4),
                           cost_usd=None, cost_source="unavailable:spawn_refused"))
        assert meetpoort.evaluate(ledger, TODAY).passed is True

    def test_a_subscription_line_without_a_price_is_no_offender(self, ledger):
        """The window is about metered money; a subscription run books 0 at the margin."""
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:claude:1", TODAY - timedelta(days=2), provider="claude",
                           model="claude-opus-5-5", billing="subscription", cost_usd=0.0,
                           cost_source="unavailable:no_transcript", api_equivalent_usd=None))
        assert meetpoort.evaluate(ledger, TODAY).passed is True

    def test_offenders_are_grouped_per_provider_and_source(self, ledger):
        _rolled_out(ledger)
        for index in range(2):
            _book(ledger, _run(f"{PROJECT}:dispatch:gap-{index}:1", TODAY - timedelta(days=2),
                               cost_usd=None, cost_source="unavailable:no_transcript"))
        gate = meetpoort.evaluate(ledger, TODAY)
        assert gate.offenders == (meetpoort.Offender("deepseek-harness", "unavailable:no_transcript", 2),)

    def test_a_run_waiting_in_the_spool_shuts_the_gate_as_incomplete(self, ledger):
        _rolled_out(ledger)
        spool = cost_ledger.spool_file_for(cost_ledger.spool_dir_for(ledger), f"{PROJECT}:dispatch:waiting:1")
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text(json.dumps([cost_ledger.build_record(
            _run(f"{PROJECT}:dispatch:waiting:1", TODAY - timedelta(days=2)),
        )]), encoding="utf-8")
        gate = meetpoort.evaluate(ledger, TODAY)
        assert gate.passed is False
        assert "incomplete" in gate.reason


# ---------------------------------------------------------------------------
# The switch in spent_today
# ---------------------------------------------------------------------------

class TestTheSwitch:
    def _store_with_todays_runs(self, ledger, today: date) -> None:
        """A passed gate plus today's runs: known spend 0.50, one unavailable run, one non-paid lane."""
        _rolled_out(ledger)
        charge = _book(ledger, _run(f"{PROJECT}:dispatch:a:1", today, cost_usd=0.25))
        _book(ledger, _run(f"{PROJECT}:dispatch:a:1", today, kind="correction",
                           supersedes=charge["record_id"], reason="the transcript had the cache hits",
                           cost_usd=0.40))
        _book(ledger, _run(f"{PROJECT}:dispatch:b:1", today, provider="glm-harness", model="glm-5.2",
                           cost_usd=0.10))
        _book(ledger, _run(f"{PROJECT}:dispatch:unknown:1", today, cost_usd=None,
                           cost_source="unavailable:no_transcript"))
        _book(ledger, _run(f"{PROJECT}:dispatch:kimi:1", today, provider="kimi", model="kimi-k3",
                           cost_usd=1.0))
        _book(ledger, _run(f"{PROJECT}:dispatch:yesterday:1", today - timedelta(days=1), cost_usd=9.0))

    def test_the_ledger_sum_counts_a_correction_once_and_an_unavailable_run_as_unknown(
        self, store, ledger, caplog,
    ):
        today = _today()
        self._store_with_todays_runs(ledger, today)
        with caplog.at_level(logging.WARNING, logger="paid_lane_budget"):
            spend = plb.spent_today(store / "state", ledger_path=ledger)
        assert spend == plb.SpendToday(total_usd=pytest.approx(0.50), unknown_cost_count=1)
        assert any("carry no price in the cost ledger" in record.message for record in caplog.records)

    def test_the_ledger_sum_ignores_a_subscription_and_a_non_paid_lane(self, store, ledger):
        today = _today()
        self._store_with_todays_runs(ledger, today)
        _book(ledger, _run(f"{PROJECT}:dispatch:sub:1", today, provider="claude", model="claude-opus-5-5",
                           billing="subscription", cost_usd=None, cost_source="unavailable:usage_unknown",
                           api_equivalent_usd=None))
        assert plb.spent_today_usd(store / "state", ledger_path=ledger) == pytest.approx(0.50)

    def test_before_the_gate_passes_the_receipts_sum_is_the_answer(self, store, ledger):
        """Same receipts file, same sum as the pre-K5c scan: the ledger is not read at all."""
        _book(ledger, _run(f"{PROJECT}:dispatch:a:1", _today(), cost_usd=99.0))  # too few roles: gate shut
        _write_receipts(store, [
            {"provider": "glm-harness", "cost_usd": 1.50, "timestamp": _today_ts()},
            {"provider": "deepseek-harness", "cost_usd": 0.25, "timestamp": _today_ts()},
            {"provider": "kimi", "cost_usd": 5.00, "timestamp": _today_ts()},
        ])
        assert plb.spent_today(store / "state", ledger_path=ledger) == plb.SpendToday(1.75, 0)

    def test_the_store_of_the_state_dir_is_where_the_ledger_is_read(self, store, ledger):
        """The explicit state_dir decides the store: no ambient resolver, no other project's ledger."""
        assert plb.cost_ledger_path(store / "state") == ledger


# ---------------------------------------------------------------------------
# Fail-closed: the receipts, and a warning that names why
# ---------------------------------------------------------------------------

class TestFailClosed:
    def _receipts(self, store) -> None:
        _write_receipts(store, [{"provider": "glm-harness", "cost_usd": 1.25, "timestamp": _today_ts()}])

    def test_an_unreadable_ledger_keeps_the_receipts_and_says_why(self, store, ledger, caplog):
        self._receipts(store)
        ledger.mkdir(parents=True)  # a directory where the ledger should be: not readable as a ledger
        with caplog.at_level(logging.WARNING, logger="paid_lane_budget"):
            spend = plb.spent_today(store / "state")
        assert spend == plb.SpendToday(1.25, 0)
        assert any("could not be read" in record.message for record in caplog.records)

    def test_a_run_waiting_in_the_spool_keeps_the_receipts_and_says_why(self, store, ledger, caplog):
        _rolled_out(ledger)
        self._receipts(store)
        spool = cost_ledger.spool_file_for(cost_ledger.spool_dir_for(ledger), f"{PROJECT}:dispatch:waiting:1")
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text(json.dumps([cost_ledger.build_record(
            _run(f"{PROJECT}:dispatch:waiting:1", _today(), cost_usd=99.0),
        )]), encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="paid_lane_budget"):
            spend = plb.spent_today(store / "state")
        assert spend == plb.SpendToday(1.25, 0)
        assert any("incomplete" in record.message for record in caplog.records)

    def test_an_empty_ledger_is_never_read_as_spent_nothing(self, store, ledger, caplog):
        """No line is no rollout: the empty ledger keeps the receipts, it does not return 0."""
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("", encoding="utf-8")
        self._receipts(store)
        with caplog.at_level(logging.WARNING, logger="paid_lane_budget"):
            spend = plb.spent_today(store / "state")
        assert spend == plb.SpendToday(1.25, 0)
        assert not any("carry no price in the cost ledger" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# The door: a warning while the gate is shut, a refusal on the ledger sum after
# ---------------------------------------------------------------------------

class TestTheDoor:
    def test_it_warns_once_per_call_while_the_gate_has_not_passed(self, store, ledger, caplog):
        _book(ledger, _run(f"{PROJECT}:dispatch:a:1", _today(), cost_usd=0.25))
        _write_receipts(store, [{"provider": "glm-harness", "cost_usd": 0.10, "timestamp": _today_ts()}])
        with caplog.at_level(logging.WARNING, logger="paid_lane_budget"):
            plb.enforce_daily_budget("glm-harness", store / "state", ledger_path=ledger)
            plb.enforce_daily_budget("glm-harness", store / "state", ledger_path=ledger)
        gate_warnings = [r for r in caplog.records if "reads the receipts, not the cost ledger" in r.message]
        assert len(gate_warnings) == 2, "one warning per call"

    def test_after_the_switch_it_refuses_on_the_ledger_sum(self, store, ledger, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
        monkeypatch.delenv(plb.ENV_OVERRIDE, raising=False)
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:a:1", _today(), cost_usd=1.50))
        _write_receipts(store, [{"provider": "glm-harness", "cost_usd": 0.10, "timestamp": _today_ts()}])
        with pytest.raises(plb.PaidLaneBudgetExceededError) as excinfo:
            plb.enforce_daily_budget("glm-harness", store / "state", ledger_path=ledger)
        assert excinfo.value.spent_usd == pytest.approx(1.50)

    def test_after_the_switch_it_refuses_at_the_budget_exactly(self, store, ledger, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
        monkeypatch.delenv(plb.ENV_OVERRIDE, raising=False)
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:a:1", _today(), cost_usd=1.00))
        assert plb.is_budget_exhausted("glm-harness", store / "state", ledger_path=ledger) is True
        with pytest.raises(plb.PaidLaneBudgetExceededError):
            plb.enforce_daily_budget("glm-harness", store / "state", ledger_path=ledger)

    def test_under_the_budget_on_the_ledger_is_not_exhausted(self, store, ledger, monkeypatch):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:a:1", _today(), cost_usd=0.99))
        assert plb.is_budget_exhausted("glm-harness", store / "state", ledger_path=ledger) is False

    def test_an_unavailable_run_warns_but_never_refuses(self, store, ledger, monkeypatch, caplog):
        monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1.00")
        monkeypatch.delenv(plb.ENV_OVERRIDE, raising=False)
        _rolled_out(ledger)
        _book(ledger, _run(f"{PROJECT}:dispatch:a:1", _today(), cost_usd=0.20))
        _book(ledger, _run(f"{PROJECT}:dispatch:gap:1", _today(), cost_usd=None,
                           cost_source="unavailable:no_transcript"))
        with caplog.at_level(logging.WARNING, logger="paid_lane_budget"):
            plb.enforce_daily_budget("glm-harness", store / "state", ledger_path=ledger)  # must not raise
        assert any("carry no price in the cost ledger" in record.message for record in caplog.records)
