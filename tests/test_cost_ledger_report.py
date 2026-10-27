"""tests/test_cost_ledger_report.py — ``vnx costs report``: the ledger per model, role, day and provider (K5a).

Track ``kosten-per-model-in-het-grootboek``, deliverable K5a. One read of the
ledger (``cost_ledger.aggregate_groups``: one ``_ledger_and_spool`` scope, the
sums from the one ``_sum_runs`` loop that ``aggregate_runs`` uses) gives a
``LedgerTotals`` per group and the totals row for the same selection; the
script ``scripts/cost_ledger_report.py`` prints it, and ``vnx costs report`` on
the pip CLI and on ``bin/vnx`` runs that script.

The fixture ledger is written through the real writer (``record_run``, the
record builder) for every record the writer accepts, raw lines for the legacy
line and the torn tail it refuses. It holds two providers, three models, two
roles, runs on two UTC days, a subscription run (cost 0, api-equivalent > 0), a
correction that moves its run's ``occurred_from`` a day back and changes its
amount, an ``unavailable:*`` run, a ``partial:cache_hit`` run, one legacy line
and a torn tail. Every expected number below is written out by hand and the
sums are compared to the cent.

**Red run** on main before this PR: every test here fails — ``aggregate_groups``
does not exist, ``scripts/cost_ledger_report.py`` does not exist, and
``vnx costs report`` is a usage error.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
_SCRIPTS = REPO_ROOT / "scripts"
for _path in (str(REPO_ROOT), str(_LIB), str(_SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger  # noqa: E402
import cost_ledger_report  # noqa: E402

PROJECT = "vnx-dev"
RUN_1 = f"{PROJECT}:dispatch:k5a-d1:1"
RUN_2 = f"{PROJECT}:dispatch:k5a-d2:1"
RUN_3 = f"{PROJECT}:dispatch:k5a-d3:1"
RUN_4 = f"{PROJECT}:dispatch:k5a-d4:1"
RUN_5 = f"{PROJECT}:dispatch:k5a-d5:1"
_ZERO = dict.fromkeys(cost_ledger.TOKEN_FIELDS, 0)
# The one line of the old shape and the torn last line: both stay out of every sum.
LEGACY_LINE = '{"provider":"claude","model":"sonnet","cost_usd":9.99}\n'
TORN_TAIL = '{"schema":"provider_cost_run/1","run_id":"vnx-dev:dispatch:tor'


def _tokens(**values) -> dict:
    tokens = dict(_ZERO)
    tokens.update(values)
    return tokens


def _run(run_id: str, **overrides) -> "cost_ledger.RunCost":
    fields = dict(
        run_id=run_id, project_id=PROJECT, role="builder", provider="claude", model="sonnet",
        occurred_from="2026-10-20T07:00:00Z", occurred_to="2026-10-20T07:30:00Z",
        tokens=_tokens(input_uncached=1000, cache_hit=500, output=200), usage_completeness="complete",
        billing="metered", cost_usd=0.25, cost_source="provider_reported",
    )
    fields.update(overrides)
    return cost_ledger.RunCost(**fields)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """The fixture store: five effective runs over two UTC days, one legacy line and a torn tail."""
    path = tmp_path / "store" / "events" / "provider_costs.ndjson"

    def write(run: "cost_ledger.RunCost") -> dict:
        return cost_ledger.record_run(run, ledger_path=path)

    write(_run(RUN_1))
    charge = write(_run(RUN_2, role="review", occurred_from="2026-10-21T00:30:00Z",
                        occurred_to="2026-10-21T01:00:00Z",
                        tokens=_tokens(input_uncached=2000, output=300), cost_usd=0.50))
    write(_run(RUN_2, kind="correction", supersedes=charge["record_id"], reason="the run began before midnight UTC",
               role="review", occurred_from="2026-10-20T23:30:00Z", occurred_to="2026-10-21T00:10:00Z",
               tokens=_tokens(input_uncached=2000, output=300), cost_usd=0.75))
    write(_run(RUN_3, model="haiku", billing="subscription", cost_usd=None, api_equivalent_usd=1.50,
               occurred_from="2026-10-21T01:00:00Z", occurred_to="2026-10-21T01:30:00Z",
               tokens=_tokens(input_uncached=3000, output=400)))
    write(_run(RUN_4, provider="deepseek-harness", model="deepseek-v4-pro", role="review",
               occurred_from="2026-10-21T08:00:00Z", occurred_to="2026-10-21T08:30:00Z",
               tokens=_tokens(input_uncached=4000, cache_hit=None, output=500),
               usage_completeness="partial:cache_hit", cost_usd=0.75))
    write(_run(RUN_5, provider="deepseek-harness", model="deepseek-v4-flash",
               occurred_from="2026-10-21T09:00:00Z", occurred_to="2026-10-21T09:30:00Z",
               tokens=_tokens(), cost_usd=None, cost_source="unavailable:spawn_refused"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(LEGACY_LINE)
        fh.write(TORN_TAIL)
    return path


def _summary(totals) -> dict:
    """A group's numbers as the tests compare them: money to the cent, tokens as they are."""
    return {
        "runs": len(totals.runs),
        "cost_usd": round(totals.cost_usd, 2),
        "api_equivalent_usd": round(totals.api_equivalent_usd, 2),
        "tokens": dict(totals.tokens),
        "unknown_token_runs": totals.unknown_token_runs,
        "unavailable_runs": len(totals.unavailable),
        "complete_runs": totals.complete_runs,
    }


def _numbers(*, runs: int, cost: float, api: float, tokens: dict, unknown: int = 0, unavailable: int = 0,
             complete: int) -> dict:
    expected = dict.fromkeys(cost_ledger.TOKEN_FIELDS, 0)
    expected.update(tokens)
    return {"runs": runs, "cost_usd": cost, "api_equivalent_usd": api, "tokens": expected,
            "unknown_token_runs": unknown, "unavailable_runs": unavailable, "complete_runs": complete}


# Every number below is summed by hand from the fixture runs, to the cent.
EXPECTED = {
    "provider": {
        "claude": _numbers(runs=3, cost=1.00, api=1.50, complete=3,
                           tokens={"input_uncached": 6000, "cache_hit": 500, "output": 900}),
        "deepseek-harness": _numbers(runs=2, cost=0.75, api=0.00, unknown=1, unavailable=1, complete=1,
                                     tokens={"input_uncached": 4000, "output": 500}),
    },
    "model": {
        "sonnet": _numbers(runs=2, cost=1.00, api=0.00, complete=2,
                           tokens={"input_uncached": 3000, "cache_hit": 500, "output": 500}),
        "haiku": _numbers(runs=1, cost=0.00, api=1.50, complete=1,
                          tokens={"input_uncached": 3000, "output": 400}),
        "deepseek-v4-pro": _numbers(runs=1, cost=0.75, api=0.00, unknown=1, complete=0,
                                    tokens={"input_uncached": 4000, "output": 500}),
        "deepseek-v4-flash": _numbers(runs=1, cost=0.00, api=0.00, tokens={}, unavailable=1, complete=1),
    },
    "role": {
        "builder": _numbers(runs=3, cost=0.25, api=1.50, unavailable=1, complete=3,
                            tokens={"input_uncached": 4000, "cache_hit": 500, "output": 600}),
        "review": _numbers(runs=2, cost=1.50, api=0.00, unknown=1, complete=1,
                           tokens={"input_uncached": 6000, "output": 800}),
    },
    "day": {
        "2026-10-20": _numbers(runs=2, cost=1.00, api=0.00, complete=2,
                               tokens={"input_uncached": 3000, "cache_hit": 500, "output": 500}),
        "2026-10-21": _numbers(runs=3, cost=0.75, api=1.50, unknown=1, unavailable=1, complete=2,
                               tokens={"input_uncached": 7000, "output": 900}),
    },
}
EXPECTED_TOTALS = _numbers(runs=5, cost=1.75, api=1.50, unknown=1, unavailable=1, complete=4,
                           tokens={"input_uncached": 10000, "cache_hit": 500, "output": 1400})


# ---------------------------------------------------------------------------
# The grouping (the lib)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", cost_ledger.GROUP_KEYS)
def test_every_group_sums_to_the_numbers_written_out_here(ledger: Path, key: str):
    grouped = cost_ledger.aggregate_groups(key, ledger)

    assert grouped.key == key
    assert {name: _summary(totals) for name, totals in grouped.groups.items()} == EXPECTED[key]


def test_the_totals_row_equals_aggregate_runs_on_the_same_file(ledger: Path):
    grouped = cost_ledger.aggregate_groups("model", ledger)
    totals = cost_ledger.aggregate_runs(ledger)

    assert _summary(grouped.totals) == _summary(totals) == EXPECTED_TOTALS
    assert grouped.totals.runs == totals.runs


def test_the_groups_add_up_to_the_totals_and_carry_no_read_level_counts(ledger: Path):
    grouped = cost_ledger.aggregate_groups("provider", ledger)

    runs = [run_id for totals in grouped.groups.values() for run_id in totals.runs]
    assert sorted(runs) == sorted(grouped.totals.runs)
    assert (grouped.totals.legacy_lines, grouped.totals.unreadable_lines, grouped.totals.torn_tail) == (1, 1, True)
    for group in grouped.groups.values():
        assert (group.legacy_lines, group.unreadable_lines, group.torn_tail, group.spool_pending) == (0, 0, False, 0)


def test_an_unknown_group_key_is_a_value_error(ledger: Path):
    with pytest.raises(ValueError, match="colour"):
        cost_ledger.aggregate_groups("colour", ledger)


def test_a_record_without_a_model_lands_in_unknown(tmp_path: Path):
    path = tmp_path / "events" / "provider_costs.ndjson"
    path.parent.mkdir(parents=True)
    line = {"schema": cost_ledger.RUN_SCHEMA, "record_id": "a" * 32, "run_id": f"{PROJECT}:dispatch:k5a-d9:1",
            "role": "builder", "provider": "claude", "occurred_from": "2026-10-21T10:00:00Z",
            "billing": "metered", "cost_usd": 0.10, "cost_source": "provider_reported",
            "tokens": _tokens(input_uncached=10, output=1), "usage_completeness": "complete"}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")

    grouped = cost_ledger.aggregate_groups("model", path)

    assert list(grouped.groups) == [cost_ledger.UNKNOWN_GROUP]
    assert round(grouped.groups[cost_ledger.UNKNOWN_GROUP].cost_usd, 2) == 0.10


# ---------------------------------------------------------------------------
# The script: the table, the JSON, --since, the spool backlog
# ---------------------------------------------------------------------------


def test_the_table_prints_the_groups_the_totals_row_and_the_read_lines(ledger: Path, capsys):
    assert cost_ledger_report.main(["--ledger", str(ledger), "--by", "provider"]) == 0

    out = capsys.readouterr().out
    assert "cost ledger report by provider" in out
    assert "claude" in out and "deepseek-harness" in out
    assert "1.00" in out and "1.75" in out and "1.50" in out  # group and totals amounts, two decimals
    assert "4/5" in out  # completeness of the totals row
    assert "onvolledig: 1 regels in het oude formaat (legacy_lines, niet opgeteld)" in out
    assert "onvolledig: 1 onleesbare regels in het grootboek" in out
    assert "onvolledig: het grootboek eindigt op een afgebroken regel" in out


def test_the_table_rounds_to_the_cent_and_json_does_not(tmp_path: Path, capsys):
    path = tmp_path / "events" / "provider_costs.ndjson"
    path.parent.mkdir(parents=True)
    line = {"schema": cost_ledger.RUN_SCHEMA, "record_id": "b" * 32, "run_id": f"{PROJECT}:dispatch:k5a-d8:1",
            "role": "builder", "provider": "claude", "model": "sonnet", "occurred_from": "2026-10-21T10:00:00Z",
            "billing": "metered", "cost_usd": 1 / 3, "cost_source": "provider_reported",
            "tokens": _tokens(input_uncached=10, output=1), "usage_completeness": "complete"}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")

    assert cost_ledger_report.main(["--ledger", str(path)]) == 0
    assert "0.33" in capsys.readouterr().out
    assert cost_ledger_report.main(["--ledger", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["totals"]["cost_usd"] == pytest.approx(1 / 3, abs=1e-7)


def test_since_drops_the_run_a_correction_moved_before_the_date(ledger: Path, capsys):
    everything = cost_ledger.aggregate_groups("day", ledger)
    assert RUN_2 in everything.totals.runs

    assert cost_ledger_report.main(["--ledger", str(ledger), "--by", "day", "--since", "2026-10-21", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [group["group"] for group in payload["groups"]] == ["2026-10-21"]
    assert payload["totals"]["runs"] == 3
    assert payload["totals"]["cost_usd"] == 0.75
    assert payload["totals"]["api_equivalent_usd"] == 1.50


def _a_spooled_run(ledger: Path, monkeypatch) -> dict:
    """A whole run waiting in the spool, as the writer leaves it when its ledger append fails."""
    def enospc(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    with monkeypatch.context() as failing:
        failing.setattr(cost_ledger, "_append_record", enospc)
        record = cost_ledger.record_run(_run(f"{PROJECT}:dispatch:k5a-d6:1"), ledger_path=ledger)
    assert record.landed == "spool"
    return record


def test_a_waiting_spool_file_is_printed_and_carried_in_json(ledger: Path, monkeypatch, capsys):
    _a_spooled_run(ledger, monkeypatch)

    assert cost_ledger_report.main(["--ledger", str(ledger)]) == 0
    assert "onvolledig: 1 runs wachten op replay" in capsys.readouterr().out
    assert cost_ledger_report.main(["--ledger", str(ledger), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["spool_pending"] == 1
    assert payload["totals"]["runs"] == 5  # the waiting run is in no sum
    assert payload["incomplete"] == "onvolledig: 1 run wacht op replay"


# ---------------------------------------------------------------------------
# The CLI: the pip entrance and bin/vnx
# ---------------------------------------------------------------------------


def test_the_pip_cli_reports_json(ledger: Path, capfd):
    from vnx_cli.commands.costs import vnx_costs

    code = vnx_costs(argparse.Namespace(costs_command="report", by="provider", since=None,
                                        ledger=str(ledger), json=True))

    assert code == 0
    payload = json.loads(capfd.readouterr().out.strip().splitlines()[-1])
    assert payload["by"] == "provider"
    assert payload["totals"]["runs"] == 5
    assert {group["group"] for group in payload["groups"]} == {"claude", "deepseek-harness"}


def test_an_unknown_by_is_a_usage_error(ledger: Path, capsys):
    assert cost_ledger_report.main(["--ledger", str(ledger), "--by", "colour"]) == 2
    assert "colour" in capsys.readouterr().err


def test_bin_vnx_runs_the_report_script(ledger: Path):
    done = subprocess.run([str(REPO_ROOT / "bin" / "vnx"), "costs", "report", "--ledger", str(ledger), "--json"],
                          capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)

    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout.strip().splitlines()[-1])
    assert payload["totals"]["runs"] == 5
    wrong = subprocess.run(
        [str(REPO_ROOT / "bin" / "vnx"), "costs", "report", "--by", "colour", "--ledger", str(ledger)],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)
    assert wrong.returncode == 2 and "colour" in wrong.stderr
