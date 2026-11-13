"""vnx impact: the CLI over impact_ledger, and the bin/vnx branch that reaches it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import impact_backtest  # noqa: E402
import impact_cli  # noqa: E402
import impact_ledger as ledger  # noqa: E402
from test_impact_ledger import FakeGh, _job, _run, _shadow  # noqa: E402
from test_impact_backtest import Git, Gh as BacktestGh, run as backtest_run  # noqa: E402
from vnx_mode import TIER_STARTER_OPERATOR, VNXMode, check_command_allowed  # noqa: E402


def _seed(state):
    gh = FakeGh([_run(1), _run(2)], {(1, "impact-shadow"): _shadow(1, failed=["tests/a.py::t"], misses=["tests/a.py::t"]),
                                     (1, "impact-shadow-job"): _job(1, wall=300)})
    assert impact_cli.main(["collect", "--state-dir", str(state)], runner=gh, out=lambda _: None) == 0


def test_report_json_is_the_structure_of_report(tmp_path):
    _seed(tmp_path)
    printed = []
    assert impact_cli.main(["report", "--json", "--state-dir", str(tmp_path)], out=printed.append) == 0
    expected = ledger.report(ledger.read_ledger(ledger.ledger_path(tmp_path)))
    assert json.loads(printed[0]) == expected
    assert expected["counted"] == ["1/1"] and [e["attempt"] for e in expected["excluded"]] == ["2/1"]


def test_report_markdown_names_the_exclusion_the_miss_and_the_red_line(tmp_path):
    _seed(tmp_path)
    printed = []
    impact_cli.main(["report", "--state-dir", str(tmp_path)], out=printed.append)
    text = printed[0]
    assert "2/1: missing:impact-shadow" in text and "tests/a.py::t (run 1/1)" in text
    assert "Counted attempts with a failing test: 1" in text and "Taken over 1 attempts" in text


def test_report_on_an_empty_store_exits_zero_and_says_nothing_is_counted(tmp_path):
    printed = []
    assert impact_cli.main(["report", "--state-dir", str(tmp_path)], out=printed.append) == 0
    assert "Nothing is counted yet" in printed[0]
    assert not ledger.ledger_path(tmp_path).exists()


def test_collect_exit_1_names_the_attempt_gh_could_not_read(tmp_path):
    printed = []
    gh = FakeGh([_run(1)], {}, fail_download=1)
    assert impact_cli.main(["collect", "--state-dir", str(tmp_path)], runner=gh, out=printed.append) == 1
    assert "1/1" in "\n".join(printed) and not ledger.ledger_path(tmp_path).exists()


CASES = {"tests/test_other.py::t": "fail", "tests/test_core.py::t": "pass", "tests/test_plain.py::t": "pass"}


def test_backtest_writes_its_own_ledger_and_report_reads_both(tmp_path):
    _seed(tmp_path)
    printed = []
    gh = BacktestGh([backtest_run(7)], {7: CASES})
    assert impact_cli.main(["backtest", "--state-dir", str(tmp_path)], runner=gh, git=Git(["scripts/lib/core.py"]), out=printed.append) == 0
    assert printed == ["impact backtest ledger: added 1, already stored 0, failed 0"]
    assert len(ledger.read_ledger(impact_backtest.backtest_path(tmp_path))) == 1 and len(ledger.read_ledger(ledger.ledger_path(tmp_path))) == 2
    out = []
    impact_cli.main(["report", "--json", "--state-dir", str(tmp_path)], out=out.append)
    rep = json.loads(out[0])
    assert rep["by_source"]["live"]["counted"] == ["1/1"] and rep["by_source"]["backtest"]["counted"] == ["7/1"]
    assert rep["counted"] == ["1/1", "7/1"] and len(rep["by_source"]["backtest"]["misses"]) == 1
    text = []
    impact_cli.main(["report", "--state-dir", str(tmp_path)], out=text.append)
    assert "- backtest: attempts 1, counted 1, excluded 0, reduced 1, misses 1, red runs 1 (reduced 1)" in text[0]
    assert "- live: attempts 2, counted 1" in text[0] and "- together: attempts 3, counted 2" in text[0]


def test_backtest_exit_1_names_the_attempt_gh_could_not_read_and_writes_no_line(tmp_path):
    printed = []
    gh = BacktestGh([backtest_run(7)], {}, failing={7: "HTTP 403: Attention Required"})
    assert impact_cli.main(["backtest", "--state-dir", str(tmp_path)], runner=gh, git=Git([]), out=printed.append) == 1
    assert "7/1" in "\n".join(printed) and not impact_backtest.backtest_path(tmp_path).exists()


def test_bad_input_exits_two(tmp_path, capsys):
    assert impact_cli.main(["report", "--since", "soon", "--state-dir", str(tmp_path)]) == 2
    assert impact_cli.main(["nonsense"]) == 2


def test_impact_is_allowed_in_both_modes():
    assert "impact" in TIER_STARTER_OPERATOR
    for mode in VNXMode:
        check_command_allowed("impact", mode)


def test_bin_vnx_impact_report_reaches_the_script(tmp_path):
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    _seed(data / "state")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update(VNX_HOME=str(REPO_ROOT), VNX_MODE="operator")
    done = subprocess.run([str(REPO_ROOT / "bin" / "vnx"), "impact", "report", "--json", "--state-dir", str(data / "state")],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout[done.stdout.index("{"):])["counted"] == ["1/1"]
