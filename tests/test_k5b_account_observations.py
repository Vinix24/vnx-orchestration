"""tests/test_k5b_account_observations.py — a calibration is its own record, never a spend (K5b, ADR-043 §7).

``vnx costs observe`` writes one ``account_observation`` per measurable provider into
``events/provider_account_observations.ndjson``; a probe that fails is an ``unavailable:<reason>``
record, never a silent skip. ``calibration_delta`` reads the drop in the total balance as the spend
of a window and refuses to subtract a top-up. ``openrouter_coverage`` reports a generation without
a ledger line when the API can list generations, and the ``total_usage`` delta when it cannot.
No test reaches the real DeepSeek or OpenRouter API: the probe's ``http_get`` is injected.

**Red run** on d0139cd3 (main before this PR): the observe test and the top-up test fail — the
module ``account_observations`` does not exist there, so ``observe`` and ``calibration_delta`` are
not defined.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "lib"), str(REPO_ROOT / "scripts")):
    sys.path.insert(0, _path)
import account_observations as ao  # noqa: E402
import cost_ledger  # noqa: E402
import cost_ledger_report  # noqa: E402

PROJECT = "vnx-dev"
DEEPSEEK_KEY = "sk-fake-deepseek-0123456789abcdef"
OPENROUTER_KEY = "sk-or-fake-openrouter-0123456789abcdef"
ZERO = dict.fromkeys(cost_ledger.TOKEN_FIELDS, 0)
ENV = {"DEEPSEEK_API_KEY": DEEPSEEK_KEY, "OPENROUTER_API_KEY": OPENROUTER_KEY}


def _deepseek_body(total: str, topped_up: str, granted: str = "0.00") -> dict:
    return {"is_available": True, "balance_infos": [
        {"currency": "USD", "total_balance": total, "granted_balance": granted, "topped_up_balance": topped_up}]}


def _openrouter_body(credits: float, usage: float) -> dict:
    return {"data": {"total_credits": credits, "total_usage": usage}}


def _http(deepseek=None, openrouter=None):
    """A probe seam: one answer per balance endpoint, as ``(body, reason)``."""
    def http_get(url: str, key: str):
        if "deepseek" in url:
            return deepseek
        return openrouter
    return http_get


def _observation(provider: str, at: str, **values) -> dict:
    if provider == "deepseek":
        values.setdefault("currency", "USD")  # a balance is only spend in dollars (K5b round 2)
    record = {"schema": ao.SCHEMA, "kind": ao.KIND, "provider": provider, "observed_at": at,
              "values": values, "record_id": f"{provider}-{at}"}
    return record


def _write_observations(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A store whose ledger path and observations path are both controlled by the test."""
    return tmp_path / "store" / "events" / "provider_costs.ndjson"


def _run(run_id: str, **overrides) -> "cost_ledger.RunCost":
    fields = dict(run_id=run_id, project_id=PROJECT, role="builder", provider="deepseek-harness",
                  model="deepseek-v4-pro", occurred_from="2026-10-20T09:00:00Z",
                  occurred_to="2026-10-20T09:30:00Z", tokens={**ZERO, "input_uncached": 1000, "output": 100},
                  usage_completeness="complete", billing="metered", cost_usd=2.0,
                  cost_source="provider_reported")
    fields.update(overrides)
    return cost_ledger.RunCost(**fields)


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------

def test_observe_writes_one_record_per_provider_with_the_raw_numbers(store: Path):
    http_get = _http((_deepseek_body("12.50", "10.00", "2.50"), ""),
                     (_openrouter_body(25.5, 10.25), ""))
    records = ao.observe(env=ENV, now=1_792_600_000.0, http_get=http_get, ledger_path=store)
    assert [record["provider"] for record in records] == ["deepseek", "openrouter"]
    assert all(record["kind"] == ao.KIND and record["schema"] == ao.SCHEMA for record in records)
    assert records[0]["observed_at"] == "2026-10-21T16:26:40Z"
    assert records[0]["values"] == {"currency": "USD", "total": 12.5, "granted": 2.5, "topped_up": 10.0}
    assert records[1]["values"] == {"total_credits": 25.5, "total_usage": 10.25}
    path = ao.observations_path_for(store)
    assert path == store.parent / "provider_account_observations.ndjson"
    assert len(ao.read_observations(path)[0]) == 2


def test_a_failing_probe_writes_unavailable_with_its_reason(store: Path):
    http_get = _http((None, "http 401"), (_openrouter_body(1.0, 0.5), ""))
    records = ao.observe(providers=("deepseek",), env=ENV, now=1_792_600_000.0, http_get=http_get,
                         ledger_path=store)
    assert "values" not in records[0]
    assert records[0]["unavailable"].startswith("unavailable:")
    assert "http 401" in records[0]["unavailable"]
    assert DEEPSEEK_KEY not in ao.observations_path_for(store).read_text(encoding="utf-8")


def test_two_currencies_are_not_added_up(store: Path):
    body = {"is_available": True, "balance_infos": [
        {"currency": "CNY", "total_balance": "12.50", "granted_balance": "2.50", "topped_up_balance": "10.00"},
        {"currency": "USD", "total_balance": "1.00", "granted_balance": "0.00", "topped_up_balance": "1.00"}]}
    records = ao.observe(providers=("deepseek",), env=ENV, now=1_792_600_000.0,
                         http_get=_http((body, ""), None), ledger_path=store)
    assert records[0]["unavailable"] == "unavailable:multiple_currencies"
    assert len(records[0]["values"]["balances"]) == 2


# ---------------------------------------------------------------------------
# calibration_delta
# ---------------------------------------------------------------------------

def test_a_topup_does_not_change_the_delta_of_the_windows_around_it(store: Path):
    path = _write_observations(ao.observations_path_for(store), [
        _observation("deepseek", "2026-10-20T00:00:00Z", total=100.0, topped_up=50.0),
        _observation("deepseek", "2026-10-21T00:00:00Z", total=80.0, topped_up=60.0),
        _observation("deepseek", "2026-10-22T00:00:00Z", total=70.0, topped_up=60.0),
    ])
    with_topup = ao.calibration_delta("deepseek", "2026-10-20", "2026-10-21", observations_path=path)
    assert with_topup["spend_usd"] is None and with_topup["unavailable"] == "unavailable:topup_in_window"
    after = ao.calibration_delta("deepseek", "2026-10-21", "2026-10-22", observations_path=path)
    assert after["spend_usd"] == 10.0 and after["unavailable"] is None
    spanning = ao.calibration_delta("deepseek", "2026-10-20", "2026-10-22", observations_path=path)
    assert spanning["unavailable"] == "unavailable:topup_in_window"


def test_the_drop_of_the_total_balance_is_the_spend(store: Path):
    path = _write_observations(ao.observations_path_for(store), [
        _observation("deepseek", "2026-10-20T00:00:00Z", total=100.0, topped_up=50.0),
        _observation("deepseek", "2026-10-21T00:00:00Z", total=92.5, topped_up=50.0),
    ])
    assert ao.calibration_delta("deepseek", "2026-10-20", "2026-10-21", observations_path=path)["spend_usd"] == 7.5
    assert ao.deviation_pct(2.0, 10.0) == -80.0
    assert ao.deviation_pct(2.0, None) is None and ao.deviation_pct(2.0, 0.0) is None


def test_a_balance_in_another_currency_has_no_dollar_spend(store: Path):
    """Codex finding 2: two CNY observations never become ``spend_usd``, and USD is the only currency."""
    path = _write_observations(ao.observations_path_for(store), [
        _observation("deepseek", "2026-10-20T00:00:00Z", currency="CNY", total=100.0, topped_up=50.0),
        _observation("deepseek", "2026-10-21T00:00:00Z", currency="CNY", total=90.0, topped_up=50.0)])
    delta = ao.calibration_delta("deepseek", "2026-10-20", "2026-10-21", observations_path=path)
    assert delta["spend_usd"] is None
    assert delta["unavailable"] == "unavailable:currency_CNY"
    mixed = _write_observations(path.parent / "mixed.ndjson", [
        _observation("deepseek", "2026-10-20T00:00:00Z", currency="USD", total=100.0, topped_up=50.0),
        _observation("deepseek", "2026-10-21T00:00:00Z", currency="CNY", total=90.0, topped_up=50.0)])
    assert ao.calibration_delta("deepseek", "2026-10-20", "2026-10-21",
                                observations_path=mixed)["unavailable"] == "unavailable:currency_mismatch"


def test_a_window_without_two_observations_or_with_a_broken_line_has_no_number(store: Path):
    path = _write_observations(ao.observations_path_for(store), [
        _observation("deepseek", "2026-10-20T00:00:00Z", total=100.0, topped_up=50.0)])
    assert ao.calibration_delta("deepseek", "2026-10-20", "2026-10-21",
                                observations_path=path)["unavailable"] == "unavailable:no_observations"
    broken = path.parent / "broken.ndjson"
    broken.write_text(json.dumps({"schema": ao.SCHEMA, "kind": ao.KIND}) + "\n", encoding="utf-8")
    delta = ao.calibration_delta("deepseek", observations_path=broken)
    assert delta["spend_usd"] is None and "observations_unreadable" in delta["unavailable"]


def test_a_valid_json_line_that_is_not_an_object_is_unreadable_and_the_rest_is_read(store: Path):
    """Codex finding 3: a ``[1]`` line is no observation and must not crash the reader."""
    path = ao.observations_path_for(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1]) + "\n"
                    + json.dumps(_observation("deepseek", "2026-10-20T00:00:00Z", total=100.0, topped_up=50.0))
                    + "\n", encoding="utf-8")
    records, unreadable = ao.read_observations(path)
    assert unreadable == 1
    assert [record["provider"] for record in records] == ["deepseek"]


def test_an_openrouter_topup_does_not_move_the_usage_delta(store: Path):
    path = _write_observations(ao.observations_path_for(store), [
        _observation("openrouter", "2026-10-20T00:00:00Z", total_credits=10.0, total_usage=5.0),
        _observation("openrouter", "2026-10-21T00:00:00Z", total_credits=30.0, total_usage=7.5)])
    assert ao.calibration_delta("openrouter", "2026-10-20", "2026-10-21", observations_path=path)["spend_usd"] == 2.5


# ---------------------------------------------------------------------------
# the ledger never reads the observations
# ---------------------------------------------------------------------------

def test_aggregate_runs_totals_do_not_move_when_observations_land(store: Path):
    cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5b-d1:1"), ledger_path=store)
    before = cost_ledger.aggregate_runs(store)
    _write_observations(ao.observations_path_for(store), [
        _observation("deepseek", "2026-10-20T00:00:00Z", total=999.0, topped_up=1.0)])
    after = cost_ledger.aggregate_runs(store)
    assert (after.cost_usd, after.api_equivalent_usd) == (before.cost_usd, before.api_equivalent_usd) == (2.0, 0.0)
    assert after.tokens == before.tokens and after.runs.keys() == before.runs.keys()


# ---------------------------------------------------------------------------
# OpenRouter coverage
# ---------------------------------------------------------------------------

def test_a_generation_without_a_ledger_line_is_a_missed_catch(store: Path):
    cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5b-d2:1", provider="litellm:zai", model="glm-5.2"),
                           ledger_path=store)
    generations = {"data": [{"id": "gen-in", "created_at": "2026-10-20T09:10:00Z"},
                            {"id": "gen-out", "created_at": "2026-10-20T11:00:00Z"}]}
    coverage = ao.openrouter_coverage("2026-10-20", "2026-10-20", env=ENV,
                                      http_get=_http(None, (generations, "")), ledger_path=store)
    assert coverage["source"] == "generations" and coverage["unavailable"] is None
    assert coverage["missed_catches"] == ("gen-out",) and coverage["ledger_runs"] == 1
    assert coverage["ledger_usd"] == 2.0


def test_without_a_generation_source_the_usage_delta_is_used_and_labelled(store: Path):
    cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5b-d3:1", provider="litellm:zai", model="glm-5.2"),
                           ledger_path=store)
    _write_observations(ao.observations_path_for(store), [
        _observation("openrouter", "2026-10-20T00:00:00Z", total_credits=10.0, total_usage=5.0),
        _observation("openrouter", "2026-10-21T00:00:00Z", total_credits=10.0, total_usage=9.0)])
    coverage = ao.openrouter_coverage("2026-10-20", "2026-10-21", env=ENV,
                                      http_get=_http(None, (None, "http 404")), ledger_path=store)
    assert coverage["source"] == "usage_delta" and coverage["missed_catches"] == ()
    assert coverage["unavailable"].startswith("unavailable:no_generation_source")
    assert coverage["usage_delta_usd"] == 4.0 and coverage["ledger_usd"] == 2.0


@pytest.mark.parametrize("generation", [{"id": "gen-no-moment"},
                                        {"id": "gen-garbage", "created_at": "not-a-moment"}])
def test_a_generation_without_a_readable_timestamp_has_no_coverage_number(store: Path, generation: dict):
    """Codex finding 4: a missing or invalid ``created_at`` is ``unavailable``, never a crash."""
    cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5b-d6:1", provider="litellm:zai", model="glm-5.2"),
                           ledger_path=store)
    coverage = ao.openrouter_coverage(
        "2026-10-20", "2026-10-20", env=ENV,
        http_get=_http(None, ({"data": [generation]}, "")), ledger_path=store)
    assert coverage["source"] == "usage_delta"
    assert coverage["unavailable"] == "unavailable:no_generation_source: generation_without_created_at"
    assert coverage["missed_catches"] == ()


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def test_the_report_carries_the_calibration_next_to_the_metered_sum(store: Path, capsys):
    cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5b-d4:1"), ledger_path=store)
    _write_observations(ao.observations_path_for(store), [
        _observation("deepseek", "2026-10-20T00:00:00Z", total=100.0, topped_up=50.0),
        _observation("deepseek", "2026-10-21T00:00:00Z", total=90.0, topped_up=50.0)])
    assert cost_ledger_report.main(["--ledger", str(store), "--by", "provider",
                                    "--since", "2026-10-20", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(group for group in payload["groups"] if group["group"] == "deepseek-harness")
    assert row["calibration"] == {"account": "deepseek", "spend_usd": 10.0, "observations": 2,
                                  "unavailable": None, "ledger_cost_usd": 2.0, "deviation_pct": -80.0}
    assert cost_ledger_report.main(["--ledger", str(store), "--by", "day"]) == 0
    assert "calibration" not in capsys.readouterr().out  # only --by provider carries it


def test_ledger_runs_outside_the_measured_interval_do_not_move_the_deviation(store: Path, capsys):
    """Codex finding 1: both sums cover the same interval, the one between the two observations."""
    cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5b-d7:1", occurred_from="2026-10-19T09:00:00Z",
                                occurred_to="2026-10-19T09:30:00Z", cost_usd=100.0), ledger_path=store)
    cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5b-d7:2", cost_usd=2.0), ledger_path=store)
    cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5b-d7:3", occurred_from="2026-10-22T09:00:00Z",
                                occurred_to="2026-10-22T09:30:00Z", cost_usd=100.0), ledger_path=store)
    _write_observations(ao.observations_path_for(store), [
        _observation("deepseek", "2026-10-20T00:00:00Z", total=100.0, topped_up=50.0),
        _observation("deepseek", "2026-10-21T00:00:00Z", total=90.0, topped_up=50.0)])
    assert cost_ledger_report.main(["--ledger", str(store), "--by", "provider",
                                    "--since", "2026-10-19", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(group for group in payload["groups"] if group["group"] == "deepseek-harness")
    assert row["calibration"]["ledger_cost_usd"] == 2.0
    assert row["calibration"]["spend_usd"] == 10.0
    assert row["calibration"]["deviation_pct"] == -80.0


def test_a_provider_without_an_account_says_so_instead_of_zero(store: Path, capsys):
    cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5b-d5:1", provider="claude", model="sonnet"),
                           ledger_path=store)
    assert cost_ledger_report.main(["--ledger", str(store), "--by", "provider", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["groups"][0]["calibration"]["unavailable"] == "unavailable:no_account_endpoint"
    assert payload["groups"][0]["calibration"]["spend_usd"] is None


def test_the_pip_cli_observes_into_the_stores_own_file(store: Path, capfd, monkeypatch, tmp_path: Path):
    import argparse

    from vnx_cli.commands.costs import vnx_costs

    # No key anywhere: the subprocess must measure nothing and call no provider.
    for name in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("VNX_PROVIDER_ENV_FILE", str(tmp_path / "absent.env"))
    assert vnx_costs(argparse.Namespace(costs_command="observe", json=True, ledger=str(store))) == 0
    records = json.loads(capfd.readouterr().out.strip().splitlines()[-1])
    assert [record["kind"] for record in records] == [ao.KIND, ao.KIND]
    assert all(record["unavailable"].startswith("unavailable:") for record in records)
    assert len(ao.read_observations(ao.observations_path_for(store))[0]) == 2
