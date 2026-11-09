"""Command discovery tests for the daily-metrics CLI (PR-3c, decision 4/14).

Each test runs ``scripts/daily_metrics.py`` through ``subprocess`` with
``--store``, ``--state-dir``, ``VNX_DAILY_METRICS_SOURCES_DIR`` and
``VNX_DAILY_METRICS_COMMANDS_DIR`` pinned under ``tmp_path`` and the clock fixed
through ``--now``, following ``test_cli.py``; no test touches the real store.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "daily_metrics.py"
NOW = "2026-11-08T03:00:00Z"
LAST_SETTLED_DAY = "2026-11-07"

STEP_MODULE = '''\
import json
import os

DAILY_STEP = {step}


def register(subparsers):
    subparsers.add_parser({stem!r}, help="record this step's run")


def run(args):
    context = {{
        "step": {stem!r},
        "project_id": args.project_id,
        "now": args.now.isoformat(),
        "store": type(args.store).__name__,
        "state_dir": str(args.state_dir),
    }}
    with open(os.environ["ORDER_FILE"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(context) + "\\n")
'''

SUBCOMMAND_MODULE = '''\
def register(subparsers):
    parser = subparsers.add_parser("marker", help="write a marker file")
    parser.add_argument("--out", required=True)


def run(args):
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write("marker")
'''

BROKEN_MODULE = "raise RuntimeError('boom')\n"
NOT_A_COMMAND_MODULE = "DAILY_STEP = 1\nVALUE = 1\n"
NEVER_IMPORTED_INIT = "raise RuntimeError('__init__ must never be imported as a command')\n"


def _cli_module():
    # An ambient PYTHONPATH can put ``scripts/`` ahead of ``scripts/lib``, where
    # ``daily_metrics`` resolves to this CLI instead of the package; mirror the
    # interpreter that runs the script, where ``sys.path[0]`` is ``scripts/lib``.
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    spec = importlib.util.spec_from_file_location("daily_metrics_cli_commands_under_test", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _empty_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _commands(tmp_path: Path, modules: dict[str, str], init: str | None = None) -> Path:
    directory = _empty_dir(tmp_path / "commands")
    for stem, text in modules.items():
        (directory / f"{stem}.py").write_text(text, encoding="utf-8")
    if init is not None:
        (directory / "__init__.py").write_text(init, encoding="utf-8")
    return directory


def _step(stem: str, step: int) -> str:
    return STEP_MODULE.format(step=step, stem=stem)


def _run_cli(tmp_path: Path, commands: Path, args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("VNX_PROJECT_ID", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["VNX_DAILY_METRICS_SOURCES_DIR"] = str(_empty_dir(tmp_path / "sources"))
    env["VNX_DAILY_METRICS_COMMANDS_DIR"] = str(commands)
    env["ORDER_FILE"] = str(tmp_path / "order.jsonl")
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        env=env, capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def _args(tmp_path: Path, extra: list[str] | None = None) -> list[str]:
    return ["--store", str(tmp_path / "store"), "--state-dir", str(tmp_path / "state"),
            "--project-id", "p", *(extra or []), "--now", NOW]


def _stderr_failures(proc: subprocess.CompletedProcess) -> list[dict]:
    return [json.loads(line) for line in proc.stderr.splitlines() if line.startswith("{")]


@pytest.fixture()
def empty_commands(tmp_path: Path) -> Path:
    return _empty_dir(tmp_path / "commands")


def test_discover_commands_is_exported():
    """Red run: fails on origin/main, where the command-discovery round was deferred."""
    module = _cli_module()
    assert hasattr(module, "discover_commands")
    assert callable(module.discover_commands)


def test_discover_commands_returns_commands_and_failures(tmp_path):
    commands = _commands(tmp_path, {
        "broken": BROKEN_MODULE,
        "stepped": _step("stepped", 2),
        "plain": NOT_A_COMMAND_MODULE,
    }, init=NEVER_IMPORTED_INIT)
    modules, failures = _cli_module().discover_commands(commands)
    assert [module.__name__.rsplit(".", 1)[-1] for module in modules] == ["stepped"]
    assert failures == [("broken", "RuntimeError")]


def test_broken_command_exits_one_reports_it_and_keeps_the_days(tmp_path):
    commands = _commands(tmp_path, {"broken_step": BROKEN_MODULE})
    proc = _run_cli(tmp_path, commands, ["daily", *_args(tmp_path)])
    assert proc.returncode == 1, proc.stderr
    assert _stderr_failures(proc) == [
        {"command_import_failed": "broken_step", "exception": "RuntimeError"}
    ]
    assert (tmp_path / "store" / f"{LAST_SETTLED_DAY}.json").is_file()
    assert json.loads((tmp_path / "state" / "health" / "daily_metrics.json").read_text())["status"]


def test_equal_daily_steps_run_in_module_name_order_and_a_lower_step_first(tmp_path):
    commands = _commands(tmp_path, {
        "zeta_step": _step("zeta_step", 5),
        "alpha_step": _step("alpha_step", 5),
        "first_step": _step("first_step", 1),
    })
    proc = _run_cli(tmp_path, commands, ["daily", *_args(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    steps = [json.loads(line) for line in (tmp_path / "order.jsonl").read_text().splitlines()]
    assert [item["step"] for item in steps] == ["first_step", "alpha_step", "zeta_step"]
    assert steps[0]["project_id"] == "p"
    assert steps[0]["now"] == "2026-11-08T03:00:00+00:00"
    assert steps[0]["store"] == "DayStore"
    assert steps[0]["state_dir"] == str(tmp_path / "state")


def test_register_and_run_become_a_subcommand(tmp_path):
    commands = _commands(tmp_path, {"marker_cmd": SUBCOMMAND_MODULE})
    marker = tmp_path / "marker.txt"
    proc = _run_cli(tmp_path, commands, ["marker", "--out", str(marker)])
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text() == "marker"
    assert not (tmp_path / "order.jsonl").exists()


def test_module_without_register_or_run_is_neither_command_nor_failure(tmp_path):
    commands = _commands(tmp_path, {"not_a_command": NOT_A_COMMAND_MODULE})
    daily = _run_cli(tmp_path, commands, ["daily", *_args(tmp_path)])
    assert daily.returncode == 0, daily.stderr
    assert daily.stderr == ""
    assert not (tmp_path / "order.jsonl").exists()
    named = _run_cli(tmp_path, commands, ["not_a_command"])
    assert named.returncode == 2


def test_init_is_never_imported_as_a_command(tmp_path):
    commands = _commands(tmp_path, {}, init=NEVER_IMPORTED_INIT)
    daily = _run_cli(tmp_path, commands, ["daily", *_args(tmp_path)])
    assert daily.returncode == 0, daily.stderr
    assert daily.stderr == ""
    named = _run_cli(tmp_path, commands, ["__init__"])
    assert named.returncode == 2
