"""tests/test_vnx_cli_gate_check.py — pin `vnx gate-check` on the pip CLI (OI-1135).

The docs prescribe `vnx gate-check --pr <ID>` to consumers, but the command
existed only on the fabric repo's `bin/vnx` (a one-line delegation to
scripts/pre_merge_gate.py). Consumer repos hold the pip-installed vnx_cli,
which refused the name with "invalid choice: 'gate-check'". The gate script
itself ships in the wheel (pyproject package-data carries scripts/**/*), so
the pip CLI now exposes the same machinery: resolve the engine root, exec
the packaged scripts/pre_merge_gate.py as a subprocess.

Red-against-main measurement: every test fails on origin/main. The new
module vnx_cli/commands/gate_check.py does not exist there (ImportError),
_register_gate_check_subparser is absent from vnx_cli.main
(AttributeError), and _dispatch_command has no gate-check branch (falls
through to print_help + exit 0). Imports of new symbols happen inside each
test so collection itself does not abort on main.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _build_parser():
    """Build the pip-CLI parser exactly as vnx_cli.main.main() does, minus
    the .vnx-version re-exec (irrelevant for parsing)."""
    import vnx_cli.main as m

    parser = m._SuggestionArgumentParser(prog="vnx")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    m._register_gate_check_subparser(subparsers)
    return parser


# ---------------------------------------------------------------------------
# 1. Registration: the pip CLI accepts the documented invocation
# ---------------------------------------------------------------------------

def test_gate_check_subparser_parses_documented_form():
    """`vnx gate-check --pr 1449` must parse — on main the registration
    helper does not exist and the name is an invalid choice."""
    parser = _build_parser()
    args = parser.parse_args(["gate-check", "--pr", "1449"])
    assert args.command == "gate-check"
    assert args.pr == "1449"
    # Defaults mirror pre_merge_gate.py's own parser.
    assert args.project_root is None
    assert args.json is False
    assert args.output_file is None
    assert args.skip_pytest is False
    assert args.store is True


def test_gate_check_subparser_accepts_full_flag_surface():
    parser = _build_parser()
    args = parser.parse_args([
        "gate-check", "--pr", "PR-6",
        "--project-root", "/tmp/proj",
        "--json",
        "--output-file", "/tmp/out.json",
        "--skip-pytest",
        "--no-store",
    ])
    assert args.pr == "PR-6"
    assert args.project_root == "/tmp/proj"
    assert args.json is True
    assert args.output_file == "/tmp/out.json"
    assert args.skip_pytest is True
    assert args.store is False


def test_gate_check_requires_pr():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["gate-check"])


# ---------------------------------------------------------------------------
# 2. Argv translation: subprocess sees the same argv bin/vnx would forward
# ---------------------------------------------------------------------------

def test_build_gate_check_argv_minimal():
    from vnx_cli.commands.gate_check import build_gate_check_argv

    ns = SimpleNamespace(pr="1449", project_root=None, json=False,
                         output_file=None, skip_pytest=False, store=True)
    assert build_gate_check_argv(ns) == ["--pr", "1449"]


def test_build_gate_check_argv_full():
    from vnx_cli.commands.gate_check import build_gate_check_argv

    ns = SimpleNamespace(pr="PR-6", project_root="/tmp/proj", json=True,
                         output_file="/tmp/out.json", skip_pytest=True,
                         store=False)
    assert build_gate_check_argv(ns) == [
        "--pr", "PR-6",
        "--project-root", "/tmp/proj",
        "--json",
        "--output-file", "/tmp/out.json",
        "--skip-pytest",
        "--no-store",
    ]


# ---------------------------------------------------------------------------
# 3. Execution: the command runs the packaged engine script
# ---------------------------------------------------------------------------

def test_vnx_gate_check_invokes_packaged_engine_script(tmp_path, monkeypatch):
    import vnx_cli.commands.gate_check as gc

    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    script = engine / "scripts" / "pre_merge_gate.py"
    script.write_text("# stand-in for the packaged gate script\n")

    monkeypatch.setattr(gc._engine, "engine_root", lambda: engine)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=1)  # HOLD

    monkeypatch.setattr(gc.subprocess, "run", fake_run)

    ns = SimpleNamespace(pr="1449", project_root=None, json=True,
                         output_file=None, skip_pytest=True, store=True)
    rc = gc.vnx_gate_check(ns)

    assert rc == 1, "exit code must propagate the gate verdict (1 = HOLD)"
    assert captured["cmd"][0] == sys.executable
    assert captured["cmd"][1] == str(script)
    assert captured["cmd"][2:] == ["--pr", "1449", "--json", "--skip-pytest"]


def test_vnx_gate_check_missing_script_fails_with_io_exit(tmp_path, monkeypatch, capsys):
    """An incomplete install must fail loudly with the gate's I/O exit code,
    never silently succeed."""
    import vnx_cli.commands.gate_check as gc

    monkeypatch.setattr(gc._engine, "engine_root", lambda: tmp_path)

    ns = SimpleNamespace(pr="1449", project_root=None, json=False,
                         output_file=None, skip_pytest=False, store=True)
    rc = gc.vnx_gate_check(ns)

    assert rc == 20
    err = capsys.readouterr().err
    assert "pre_merge_gate.py not found" in err


# ---------------------------------------------------------------------------
# 4. Dispatch wiring: main routes gate-check to the command module
# ---------------------------------------------------------------------------

def test_dispatch_command_routes_gate_check(monkeypatch):
    """On main the elif branch is absent: _dispatch_command falls through to
    print_help + exit 0, so the sentinel exit code below never appears."""
    import vnx_cli.main as m
    import vnx_cli.commands.gate_check as gc

    monkeypatch.setattr(gc, "vnx_gate_check", lambda args: 42)

    parser = argparse.ArgumentParser(prog="vnx")
    ns = argparse.Namespace(command="gate-check", pr="1449",
                            project_root=None, json=False, output_file=None,
                            skip_pytest=False, store=True)
    with pytest.raises(SystemExit) as exc:
        m._dispatch_command(ns, parser)
    assert exc.value.code == 42


def test_main_registers_gate_check_in_full_parser():
    """The real registration list in main() must include gate-check —
    registering the helper but forgetting the call would still strand
    consumers."""
    import inspect

    import vnx_cli.main as m

    src = inspect.getsource(m.main)
    assert "_register_gate_check_subparser(subparsers)" in src


def test_documented_gate_script_ships_in_package_data():
    """The wheel must actually carry the machinery this command runs:
    pyproject's package-data glob scripts/**/* under vnx_orchestration."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'scripts/**/*' in pyproject
    assert (REPO_ROOT / "scripts" / "pre_merge_gate.py").is_file()
