"""tests/test_vnx_cli_worktree.py — pin `vnx worktree-release` on the pip CLI (OI-1389).

Docs describe `bin/vnx worktree-release` (scripts/commands/worktree_release.sh
::cmd_worktree_release, wrapping scripts/lib/worktree_release.py) as the
governed release path for locked worktrees, but the command existed only in
the fabric-repo bash entrance. Consumer repos (Mission Control, SEOcrawler_v2,
sales-copilot) hold only the pip-installed vnx_cli, which refused the name
with "invalid choice: 'worktree-release'". The engine
(scripts/lib/worktree_release.py) is pure stdlib and dry-run-first already, so
the pip CLI now exposes the exact same flag surface (--apply/--dry-run/--json)
and delegates to the same worktree_release.main() entry point in-process.

Red-against-main measurement: every test below fails on origin/main. The new
module vnx_cli/commands/worktree.py does not exist there (ImportError),
_register_worktree_release_subparser is absent from vnx_cli.main
(AttributeError), and _dispatch_command has no worktree-release branch (falls
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
    m._register_worktree_release_subparser(subparsers)
    return parser


# ---------------------------------------------------------------------------
# 1. Registration: the pip CLI accepts the documented invocation, flag-for-flag
#    with scripts/commands/worktree_release.sh::cmd_worktree_release
# ---------------------------------------------------------------------------

def test_worktree_release_subparser_parses_bare_form():
    """`vnx worktree-release` (no flags) must parse — on main the name is an
    invalid choice."""
    parser = _build_parser()
    args = parser.parse_args(["worktree-release"])
    assert args.command == "worktree-release"
    # Dry-run is the default, mirroring the bash wrapper's default.
    assert args.apply is False
    assert args.json is False


def test_worktree_release_subparser_accepts_apply():
    parser = _build_parser()
    args = parser.parse_args(["worktree-release", "--apply"])
    assert args.apply is True


def test_worktree_release_subparser_accepts_dry_run_explicitly():
    parser = _build_parser()
    args = parser.parse_args(["worktree-release", "--dry-run"])
    assert args.apply is False


def test_worktree_release_subparser_accepts_json():
    parser = _build_parser()
    args = parser.parse_args(["worktree-release", "--json"])
    assert args.json is True
    assert args.apply is False


def test_worktree_release_subparser_rejects_unknown_flag():
    """No flag beyond --apply/--dry-run/--json/-h is part of the documented
    surface — an unknown flag must still be refused by argparse."""
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["worktree-release", "--repo-root", "/tmp/x"])


# ---------------------------------------------------------------------------
# 2. Argv translation: only non-default flags are forwarded to the engine
# ---------------------------------------------------------------------------

def test_build_worktree_release_argv_default_is_dry_run():
    from vnx_cli.commands.worktree import build_worktree_release_argv

    ns = SimpleNamespace(apply=False, json=False)
    assert build_worktree_release_argv(ns) == []


def test_build_worktree_release_argv_apply_and_json():
    from vnx_cli.commands.worktree import build_worktree_release_argv

    ns = SimpleNamespace(apply=True, json=True)
    assert build_worktree_release_argv(ns) == ["--apply", "--json"]


# ---------------------------------------------------------------------------
# 3. Execution: the command delegates to the engine's own main(), no new flag
# ---------------------------------------------------------------------------

def test_vnx_worktree_release_delegates_to_engine_main(monkeypatch):
    import vnx_cli.commands.worktree as w

    monkeypatch.setattr(w._engine, "ensure_engine_on_path", lambda: Path("/tmp"))

    captured = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    import types
    fake_module = types.ModuleType("worktree_release")
    fake_module.main = fake_main
    monkeypatch.setitem(sys.modules, "worktree_release", fake_module)

    ns = SimpleNamespace(apply=True, json=False)
    rc = w.vnx_worktree_release(ns)

    assert rc == 0
    assert captured["argv"] == ["--apply"]


def test_vnx_worktree_release_dry_run_default_no_apply_flag(monkeypatch):
    """Without --apply, nothing destructive is passed to the engine — the
    default dry-run contract must hold at the pip-CLI layer too."""
    import vnx_cli.commands.worktree as w

    monkeypatch.setattr(w._engine, "ensure_engine_on_path", lambda: Path("/tmp"))

    captured = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    import types
    fake_module = types.ModuleType("worktree_release")
    fake_module.main = fake_main
    monkeypatch.setitem(sys.modules, "worktree_release", fake_module)

    ns = SimpleNamespace(apply=False, json=True)
    rc = w.vnx_worktree_release(ns)

    assert rc == 0
    assert "--apply" not in captured["argv"]
    assert captured["argv"] == ["--json"]


def test_vnx_worktree_release_propagates_nonzero_exit(monkeypatch):
    """A run with errors/partial cleanups must surface non-zero, matching
    worktree_release.main()'s own exit contract."""
    import vnx_cli.commands.worktree as w

    monkeypatch.setattr(w._engine, "ensure_engine_on_path", lambda: Path("/tmp"))

    import types
    fake_module = types.ModuleType("worktree_release")
    fake_module.main = lambda argv: 1
    monkeypatch.setitem(sys.modules, "worktree_release", fake_module)

    ns = SimpleNamespace(apply=True, json=False)
    assert w.vnx_worktree_release(ns) == 1


# ---------------------------------------------------------------------------
# 4. Dispatch wiring: main routes worktree-release to the command module
# ---------------------------------------------------------------------------

def test_dispatch_command_routes_worktree_release(monkeypatch):
    """On main the elif branch is absent: _dispatch_command falls through to
    print_help + exit 0, so the sentinel exit code below never appears."""
    import vnx_cli.main as m
    import vnx_cli.commands.worktree as w

    monkeypatch.setattr(w, "vnx_worktree_release", lambda args: 42)

    parser = argparse.ArgumentParser(prog="vnx")
    ns = argparse.Namespace(command="worktree-release", apply=False, json=False)
    with pytest.raises(SystemExit) as exc:
        m._dispatch_command(ns, parser)
    assert exc.value.code == 42


def test_main_registers_worktree_release_in_full_parser():
    """The real registration list in main() must include worktree-release —
    registering the helper but forgetting the call would still strand
    consumers."""
    import inspect

    import vnx_cli.main as m

    src = inspect.getsource(m.main)
    assert "_register_worktree_release_subparser(subparsers)" in src


# ---------------------------------------------------------------------------
# 5. Engine parity: the imported symbol is the real, dry-run-first engine
# ---------------------------------------------------------------------------

def test_engine_module_is_importable_via_engine_bootstrap():
    """`_engine.ensure_engine_on_path()` must put the real
    scripts/lib/worktree_release.py on sys.path — no vendored/duplicated copy."""
    from vnx_cli import _engine

    _engine.ensure_engine_on_path()
    import worktree_release

    assert Path(worktree_release.__file__).resolve() == (
        REPO_ROOT / "scripts" / "lib" / "worktree_release.py"
    ).resolve()
