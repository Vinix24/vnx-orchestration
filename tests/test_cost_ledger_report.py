"""tests/test_cost_ledger_report.py — ``vnx costs report``: the ledger per model, role, day, provider (K5a).

The fixture ledger is written through the real writer (``record_run``) where it
accepts the record, raw for the legacy line and the torn tail it refuses: two
providers, three models, two roles, two UTC days, a subscription run, a
correction that moves its run a day back and changes its amount, an
``unavailable:*`` run, a ``partial:cache_hit`` run, a legacy line and a torn tail.
Every expected number is hand-summed and compared to the cent, for all four
``--by`` keys; the totals row is compared with ``aggregate_runs``.

**Red run** on main before this PR: every test here fails (no ``aggregate_groups``, no report script).
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "lib"), str(REPO_ROOT / "scripts")):
    sys.path.insert(0, _path)
import cost_ledger  # noqa: E402
import cost_ledger_report  # noqa: E402

PROJECT = "vnx-dev"
RUNS = [f"{PROJECT}:dispatch:k5a-d{number}:1" for number in range(1, 6)]
ZERO = dict.fromkeys(cost_ledger.TOKEN_FIELDS, 0)
LEGACY_LINE = '{"provider":"claude","model":"sonnet","cost_usd":9.99}\n'
TORN_TAIL = '{"schema":"provider_cost_run/1","run_id":"vnx-dev:dispatch:tor'


def _tokens(**values) -> dict:
    return {**ZERO, **values}


def _run(run_id: str, **overrides) -> "cost_ledger.RunCost":
    fields = dict(run_id=run_id, project_id=PROJECT, role="builder", provider="claude", model="sonnet",
                  occurred_from="2026-10-20T07:00:00Z", occurred_to="2026-10-20T07:30:00Z",
                  tokens=_tokens(input_uncached=1000, cache_hit=500, output=200), usage_completeness="complete",
                  billing="metered", cost_usd=0.25, cost_source="provider_reported")
    fields.update(overrides)
    return cost_ledger.RunCost(**fields)


def _raw(path: Path, line: dict) -> Path:
    """``line`` as the ledger's only line: raw, so a record the writer refuses can be read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "store" / "events" / "provider_costs.ndjson"

    def write(run: "cost_ledger.RunCost") -> dict:
        return cost_ledger.record_run(run, ledger_path=path)

    write(_run(RUNS[0]))
    charge = write(_run(RUNS[1], role="review", occurred_from="2026-10-21T00:30:00Z",
                        occurred_to="2026-10-21T01:00:00Z", tokens=_tokens(input_uncached=2000, output=300),
                        cost_usd=0.50))
    write(_run(RUNS[1], kind="correction", supersedes=charge["record_id"], reason="began before midnight UTC",
               role="review", occurred_from="2026-10-20T23:30:00Z", occurred_to="2026-10-21T00:10:00Z",
               tokens=_tokens(input_uncached=2000, output=300), cost_usd=0.75))
    write(_run(RUNS[2], model="haiku", billing="subscription", cost_usd=None, api_equivalent_usd=1.50,
               occurred_from="2026-10-21T01:00:00Z", occurred_to="2026-10-21T01:30:00Z",
               tokens=_tokens(input_uncached=3000, output=400)))
    write(_run(RUNS[3], provider="deepseek-harness", model="deepseek-v4-pro", role="review",
               occurred_from="2026-10-21T08:00:00Z", occurred_to="2026-10-21T08:30:00Z",
               tokens=_tokens(input_uncached=4000, cache_hit=None, output=500),
               usage_completeness="partial:cache_hit", cost_usd=0.75))
    write(_run(RUNS[4], provider="deepseek-harness", model="deepseek-v4-flash", occurred_from="2026-10-21T09:00:00Z",
               occurred_to="2026-10-21T09:30:00Z", tokens=_tokens(), cost_usd=None,
               cost_source="unavailable:spawn_refused"))
    path.write_text(path.read_text(encoding="utf-8") + LEGACY_LINE + TORN_TAIL, encoding="utf-8")
    return path


def _summary(totals) -> tuple:
    """A group's numbers: money to the cent, tokens in ``TOKEN_FIELDS`` order, the counts as they are."""
    return (len(totals.runs), round(totals.cost_usd, 2), round(totals.api_equivalent_usd, 2),
            tuple(totals.tokens[field] for field in cost_ledger.TOKEN_FIELDS),
            totals.unknown_token_runs, len(totals.unavailable), totals.complete_runs)


# Hand-summed per group: runs, cost_usd, api_equivalent_usd, tokens (in, cache_hit, 5m, 1h, out),
# unknown-token runs, unavailable runs, complete runs.
EXPECTED = {
    "provider": {
        "claude": (3, 1.00, 1.50, (6000, 500, 0, 0, 900), 0, 0, 3),
        "deepseek-harness": (2, 0.75, 0.00, (4000, 0, 0, 0, 500), 1, 1, 1),
    },
    "model": {
        "sonnet": (2, 1.00, 0.00, (3000, 500, 0, 0, 500), 0, 0, 2),
        "haiku": (1, 0.00, 1.50, (3000, 0, 0, 0, 400), 0, 0, 1),
        "deepseek-v4-pro": (1, 0.75, 0.00, (4000, 0, 0, 0, 500), 1, 0, 0),
        "deepseek-v4-flash": (1, 0.00, 0.00, (0, 0, 0, 0, 0), 0, 1, 1),
    },
    "role": {
        "builder": (3, 0.25, 1.50, (4000, 500, 0, 0, 600), 0, 1, 3),
        "review": (2, 1.50, 0.00, (6000, 0, 0, 0, 800), 1, 0, 1),
    },
    "day": {
        "2026-10-20": (2, 1.00, 0.00, (3000, 500, 0, 0, 500), 0, 0, 2),
        "2026-10-21": (3, 0.75, 1.50, (7000, 0, 0, 0, 900), 1, 1, 2),
    },
}


@pytest.mark.parametrize("key", cost_ledger.GROUP_KEYS)
def test_every_group_for_every_key_sums_to_the_numbers_written_out_here(ledger: Path, key: str):
    grouped = cost_ledger.aggregate_groups(key, ledger)
    assert grouped.key == key
    assert {name: _summary(totals) for name, totals in grouped.groups.items()} == EXPECTED[key]


def test_the_totals_equal_aggregate_runs_and_carry_the_read_level_counts_once(ledger: Path):
    grouped = cost_ledger.aggregate_groups("provider", ledger)
    assert _summary(grouped.totals) == _summary(cost_ledger.aggregate_runs(ledger)) \
        == (5, 1.75, 1.50, (10000, 500, 0, 0, 1400), 1, 1, 4)
    assert sorted(run for totals in grouped.groups.values() for run in totals.runs) == sorted(grouped.totals.runs)
    assert (grouped.totals.legacy_lines, grouped.totals.unreadable_lines, grouped.totals.torn_tail) == (1, 1, True)
    for group in grouped.groups.values():  # the spool and the line counts belong to the read, not to a group
        assert (group.legacy_lines, group.unreadable_lines, group.torn_tail, group.spool_pending) == (0, 0, False, 0)


def test_the_table_prints_the_groups_the_totals_row_and_the_read_lines(ledger: Path, capsys):
    assert cost_ledger_report.main(["--ledger", str(ledger), "--by", "provider"]) == 0
    out = capsys.readouterr().out
    assert "cost ledger report by provider" in out and "claude" in out and "deepseek-harness" in out
    assert "1.00" in out and "1.75" in out and "1.50" in out and "4/5" in out
    assert "onvolledig: 1 regels in het oude formaat (legacy_lines, niet opgeteld)" in out
    assert "onvolledig: 1 onleesbare regels in het grootboek" in out
    assert "onvolledig: het grootboek eindigt op een afgebroken regel" in out


def test_the_table_rounds_to_the_cent_and_json_does_not(tmp_path: Path, capsys):
    path = _raw(tmp_path / "events" / "provider_costs.ndjson",
                {"schema": cost_ledger.RUN_SCHEMA, "record_id": "b" * 32, "run_id": f"{PROJECT}:dispatch:k5a-d8:1",
                 "role": "builder", "provider": "claude", "model": "sonnet",
                 "occurred_from": "2026-10-21T10:00:00Z", "billing": "metered", "cost_usd": 1 / 3,
                 "cost_source": "provider_reported", "tokens": _tokens(input_uncached=10, output=1),
                 "usage_completeness": "complete"})
    assert cost_ledger_report.main(["--ledger", str(path)]) == 0
    assert "0.33" in capsys.readouterr().out
    assert cost_ledger_report.main(["--ledger", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["cost_usd"] == pytest.approx(1 / 3, abs=1e-7)


def test_since_drops_the_run_a_correction_moved_before_the_date(ledger: Path, capsys):
    assert RUNS[1] in cost_ledger.aggregate_groups("day", ledger).totals.runs
    assert cost_ledger_report.main(["--ledger", str(ledger), "--by", "day", "--since", "2026-10-21", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [group["group"] for group in payload["groups"]] == ["2026-10-21"]
    assert (payload["totals"]["runs"], payload["totals"]["cost_usd"], payload["totals"]["api_equivalent_usd"]) \
        == (3, 0.75, 1.50)  # the corrected run is out, the three runs of 2026-10-21 stay


def test_a_waiting_spool_file_is_printed_and_carried_in_json(ledger: Path, monkeypatch, capsys):
    def enospc(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    with monkeypatch.context() as failing:
        failing.setattr(cost_ledger, "_append_record", enospc)
        spooled = cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5a-d6:1"), ledger_path=ledger)
    assert spooled.landed == "spool"
    assert cost_ledger_report.main(["--ledger", str(ledger)]) == 0
    assert "onvolledig: 1 runs wachten op replay" in capsys.readouterr().out
    assert cost_ledger_report.main(["--ledger", str(ledger), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["spool_pending"] == 1 and payload["totals"]["runs"] == 5  # the waiting run is in no sum
    assert payload["incomplete"] == "onvolledig: 1 run wacht op replay"


def test_a_record_without_a_model_lands_in_unknown_and_an_unknown_key_is_a_value_error(tmp_path: Path):
    path = _raw(tmp_path / "events" / "provider_costs.ndjson",
                {"schema": cost_ledger.RUN_SCHEMA, "record_id": "a" * 32, "run_id": f"{PROJECT}:dispatch:k5a-d9:1",
                 "role": "builder", "provider": "claude", "occurred_from": "2026-10-21T10:00:00Z",
                 "billing": "metered", "cost_usd": 0.10, "cost_source": "provider_reported",
                 "tokens": _tokens(input_uncached=10, output=1), "usage_completeness": "complete"})
    grouped = cost_ledger.aggregate_groups("model", path)
    assert list(grouped.groups) == [cost_ledger.UNKNOWN_GROUP]
    assert round(grouped.groups[cost_ledger.UNKNOWN_GROUP].cost_usd, 2) == 0.10
    with pytest.raises(ValueError, match="colour"):
        cost_ledger.aggregate_groups("colour", path)


def test_the_pip_cli_reports_json(ledger: Path, capfd):
    from vnx_cli.commands.costs import vnx_costs

    code = vnx_costs(argparse.Namespace(costs_command="report", by="provider", since=None,
                                        ledger=str(ledger), json=True))
    assert code == 0
    payload = json.loads(capfd.readouterr().out.strip().splitlines()[-1])
    assert {group["group"] for group in payload["groups"]} == {"claude", "deepseek-harness"}
    assert payload["totals"]["runs"] == 5


def test_an_unknown_by_is_a_usage_error(ledger: Path, capsys):
    assert cost_ledger_report.main(["--ledger", str(ledger), "--by", "colour"]) == 2
    assert "colour" in capsys.readouterr().err
