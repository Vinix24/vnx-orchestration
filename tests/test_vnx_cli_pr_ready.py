"""tests/test_vnx_cli_pr_ready.py — pin `vnx pr-ready` on the pip CLI.

Mirrors tests/test_vnx_cli_gate_check.py: the fabric repo's `bin/vnx` gets
the command as a one-line delegation, and a consumer repo holds only the
pip-installed vnx_cli. Without this wiring the pip CLI refuses the name with
"invalid choice: 'pr-ready'" while the command is documented.

Imports of new symbols happen inside each test so collection does not abort
on a tree where the command module is absent.
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
    import vnx_cli.main as m

    parser = m._SuggestionArgumentParser(prog="vnx")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    m._register_pr_ready_subparser(subparsers)
    return parser


def test_subparser_parses_the_documented_form():
    args = _build_parser().parse_args(["pr-ready", "1703"])
    assert args.command == "pr-ready"
    assert args.pr == ["1703"]


def test_subparser_accepts_several_prs_in_one_call():
    """The manual count was done PR by PR; the point is to ask once."""
    args = _build_parser().parse_args(["pr-ready", "1703", "1704", "1705"])
    assert args.pr == ["1703", "1704", "1705"]


def test_subparser_requires_at_least_one_pr():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["pr-ready"])


def test_build_argv_forwards_only_non_defaults():
    from vnx_cli.commands.pr_ready import build_pr_ready_argv

    ns = SimpleNamespace(pr=["1703"], json=False, verbose=False,
                         protected_branch="main", project_root=None, timeout=20)
    assert build_pr_ready_argv(ns) == ["1703"]


def test_build_argv_forwards_the_full_flag_surface():
    from vnx_cli.commands.pr_ready import build_pr_ready_argv

    ns = SimpleNamespace(pr=["1703", "1704"], json=True, verbose=True,
                         protected_branch="release", project_root="/tmp/x", timeout=45)
    assert build_pr_ready_argv(ns) == [
        "1703", "1704", "--json", "--verbose",
        "--protected-branch", "release", "--project-root", "/tmp/x", "--timeout", "45",
    ]


def test_invokes_the_packaged_engine_script(tmp_path, monkeypatch):
    import vnx_cli.commands.pr_ready as cmd

    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    script = engine / "scripts" / "pr_ready.py"
    script.write_text("# stand-in for the packaged script\n")
    monkeypatch.setattr(cmd._engine, "engine_root", lambda: engine)

    captured = {}

    def fake_run(command, **_kwargs):
        captured["cmd"] = command
        return SimpleNamespace(returncode=2)  # UNMEASURABLE

    monkeypatch.setattr(cmd.subprocess, "run", fake_run)
    ns = SimpleNamespace(pr=["1703"], json=True, verbose=False,
                         protected_branch="main", project_root=None, timeout=20)

    assert cmd.vnx_pr_ready(ns) == 2, "the readiness exit code must propagate verbatim"
    assert captured["cmd"][0] == sys.executable
    assert captured["cmd"][1] == str(script)
    assert captured["cmd"][2:] == ["1703", "--json"]


def test_missing_script_fails_loudly_instead_of_reporting_ready(tmp_path, monkeypatch, capsys):
    """An incomplete install must never exit 0 — that would read as READY."""
    import vnx_cli.commands.pr_ready as cmd

    monkeypatch.setattr(cmd._engine, "engine_root", lambda: tmp_path)
    ns = SimpleNamespace(pr=["1703"], json=False, verbose=False,
                         protected_branch="main", project_root=None, timeout=20)

    rc = cmd.vnx_pr_ready(ns)
    assert rc != 0
    assert "pr_ready.py not found" in capsys.readouterr().err


def test_main_registers_pr_ready_in_the_full_parser():
    """Registering the helper but forgetting the call in main() would still
    strand consumers, so the call site itself is pinned."""
    import inspect

    import vnx_cli.main as m

    assert "_register_pr_ready_subparser(subparsers)" in inspect.getsource(m.main)


def test_dispatch_command_routes_pr_ready(monkeypatch):
    """Without the elif branch _dispatch_command falls through to print_help
    and exit 0 — which would read as READY for every PR."""
    import vnx_cli.commands.pr_ready as cmd
    import vnx_cli.main as m

    monkeypatch.setattr(cmd, "vnx_pr_ready", lambda args: 7)
    ns = SimpleNamespace(command="pr-ready", pr=["1703"], json=False, verbose=False,
                         protected_branch="main", project_root=None, timeout=20)
    parser = argparse.ArgumentParser(prog="vnx")
    with pytest.raises(SystemExit) as excinfo:
        m._dispatch_command(ns, parser)
    assert excinfo.value.code == 7


def test_the_script_ships_where_bin_vnx_and_the_wheel_both_look():
    assert (REPO_ROOT / "scripts" / "pr_ready.py").is_file()
    assert "pr-ready)" in (REPO_ROOT / "bin" / "vnx").read_text()
