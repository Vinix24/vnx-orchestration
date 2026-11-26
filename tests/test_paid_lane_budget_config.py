"""N8: real-store cost ceilings; the USD override is never the boolean bypass."""
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

import config_registry as cr
import config_runtime as crt
import config_store_db as cs
import cost_ledger
import paid_lane_budget as plb
import receipt_classifier as classifier

PID = "n8-project"
READERS = (plb, classifier)
USD_OVERRIDE = "VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET_USD"


@pytest.fixture(autouse=True)
def config_store(tmp_path, monkeypatch):
    for reader in READERS:
        monkeypatch.delenv(reader.ENV_DAILY_BUDGET, raising=False)
        monkeypatch.delenv(reader.ENV_DAILY_BUDGET.replace("VNX_", "VNX_OVERRIDE_", 1), raising=False)
    monkeypatch.delenv(plb.ENV_OVERRIDE, raising=False)
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    monkeypatch.setattr(crt, "_wired_for", {})
    monkeypatch.setattr(crt, "_wire_status", None)
    monkeypatch.setattr(cr, "_db_resolver", None)
    monkeypatch.setattr(cr, "_default_project_id", None)
    with closing(sqlite3.connect(tmp_path / "runtime_coordination.db")) as conn:
        cs.ensure_config_tables(conn)
        yield conn


@pytest.mark.parametrize("reader", READERS)
def test_env_raise_without_project_row(reader, config_store, monkeypatch):
    monkeypatch.setenv(reader.ENV_DAILY_BUDGET, "8")
    assert reader.get_daily_budget_usd() == 8
    assert config_store.execute("SELECT count(*) FROM project_config").fetchone()[0] == 0


@pytest.mark.parametrize("reader", READERS)
def test_project_row_beats_env_and_numeric_override_beats_row(reader, config_store, monkeypatch):
    key = reader.ENV_DAILY_BUDGET
    cs.set_config(config_store, PID, key, "3", actor="op", approval_id="n8-test")
    cs.set_config(config_store, PID + "-other", key, "9", actor="op", approval_id="n8-test")
    monkeypatch.setenv(key, "8")
    assert reader.get_daily_budget_usd() == 3
    monkeypatch.setenv(key.replace("VNX_", "VNX_OVERRIDE_", 1), "4")
    assert reader.get_daily_budget_usd() == 4


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("raw,expected", [(None, None), ("", None), ("bad", None), ("-5", 0), ("nan", 0)])
def test_existing_parser_behaviour(reader, monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv(reader.ENV_DAILY_BUDGET, raw)
    assert reader.get_daily_budget_usd() == (reader.DEFAULT_DAILY_BUDGET_USD if expected is None else expected)


@pytest.fixture
def spent_ledger(tmp_path):
    ledger = tmp_path / "provider_costs.ndjson"
    today = datetime.now(timezone.utc).date()
    roles = (*plb.meetpoort.REQUIRED_ROLES, "builder")
    for index, role in enumerate(roles):
        day = today if index == len(roles) - 1 else today - timedelta(days=30)
        cost_ledger.record_run(cost_ledger.RunCost(
            run_id=f"{PID}:dispatch:n8-{index}:1", project_id=PID, role=role,
            provider="deepseek-harness", model="deepseek-v4-pro",
            occurred_from=f"{day}T07:00:00Z", occurred_to=f"{day}T07:30:00Z",
            tokens={"input_uncached": 1000, "cache_hit": 0, "cache_write_5m": 0,
                    "cache_write_1h": 0, "output": 200},
            usage_completeness="complete", billing="metered", cost_usd=2,
            cost_source="provider_reported",
        ), ledger_path=ledger)
    assert plb.meetpoort.evaluate(ledger, today).passed
    return ledger


def test_bypass_one_logs_exactly_one_warning_with_both_amounts(tmp_path, spent_ledger, monkeypatch, caplog):
    monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1")
    monkeypatch.setenv(plb.ENV_OVERRIDE, "1")
    with caplog.at_level(logging.WARNING):
        plb.enforce_daily_budget("deepseek-harness", tmp_path, ledger_path=spent_ledger)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "spent_today_usd=2.000000 daily_budget_usd=1.000000" in warnings[0]
    assert f"{plb.ENV_OVERRIDE}=1" in warnings[0]


@pytest.mark.parametrize("bypass", [None, "", "true", "0", "unknown", " 1", "1 "])
def test_only_exact_one_bypasses(tmp_path, spent_ledger, monkeypatch, bypass):
    monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "1")
    if bypass is not None:
        monkeypatch.setenv(plb.ENV_OVERRIDE, bypass)
    with pytest.raises(plb.PaidLaneBudgetExceededError):
        plb.enforce_daily_budget("deepseek-harness", tmp_path, ledger_path=spent_ledger)


def test_usd_one_is_a_ceiling_not_a_bypass(tmp_path, spent_ledger, monkeypatch):
    monkeypatch.setenv(plb.ENV_DAILY_BUDGET, "8")
    monkeypatch.setenv(USD_OVERRIDE, "1")
    assert plb.get_daily_budget_usd() == 1
    with pytest.raises(plb.PaidLaneBudgetExceededError) as exc:
        plb.enforce_daily_budget("deepseek-harness", tmp_path, ledger_path=spent_ledger)
    assert (exc.value.spent_usd, exc.value.budget_usd) == (2, 1)
