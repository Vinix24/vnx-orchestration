"""vnx worktree-release — governed release path for locked worktrees (OI-1052),
exposed on the pip CLI (OI-1389).

The fabric repo's `bin/vnx worktree-release` is a bash wrapper
(scripts/commands/worktree_release.sh::cmd_worktree_release) that shells out
to scripts/lib/worktree_release.py, always injecting `--repo-root
"$PROJECT_ROOT"` internally — that flag is never exposed to the operator, only
`--apply` / `--dry-run` / `--json` / `-h` are documented. Consumer repos
(Mission Control, SEOcrawler_v2, sales-copilot) have no `bin/vnx` next to
them, only the pip-installed `vnx`, which lacked this command entirely
(`invalid choice: 'worktree-release'`) even though the engine itself
(scripts/lib/worktree_release.py) is fully governed and dry-run-first.

worktree_release.py has no self-bootstrap dependency on being invoked as a
script (pure stdlib), so — unlike gate-check's packaged-script subprocess
call — this imports the engine in-process after `_engine.ensure_engine_on_path()`
puts scripts/lib on sys.path, the same bootstrap `pool.py` uses. No
`--repo-root` flag is added here either: the engine's own
`_resolve_repo_root()` already falls back to the `PROJECT_ROOT` env var, else
`git rev-parse --show-toplevel` from the current working directory, which is
the same mechanism a `cd <repo> && vnx worktree-release --apply` invocation
resolves through today.
"""

from __future__ import annotations

import argparse
from typing import List

from vnx_cli import _engine


def register_worktree_release_subparser(subparsers: argparse.Action) -> None:
    """Register `vnx worktree-release`.

    Flag surface mirrors scripts/commands/worktree_release.sh::cmd_worktree_release
    one-to-one: --apply, --dry-run, --json (plus argparse's own -h/--help).
    """
    wr_parser = subparsers.add_parser(
        "worktree-release",
        help="governed release path for locked worktrees (OI-1052); dry-run by default, --apply to release",
    )
    wr_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually unlock, rescue, and remove worktrees (default: dry-run)",
    )
    wr_parser.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="Report only, make no changes (default)",
    )
    wr_parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON output",
    )
    wr_parser.set_defaults(apply=False)


def build_worktree_release_argv(args: argparse.Namespace) -> List[str]:
    """Translate the parsed vnx_cli namespace into worktree_release.main() argv.

    Only non-default flags are forwarded, mirroring build_gate_check_argv's
    convention in gate_check.py.
    """
    argv: List[str] = []
    if getattr(args, "apply", False):
        argv.append("--apply")
    if getattr(args, "json", False):
        argv.append("--json")
    return argv


def vnx_worktree_release(args: argparse.Namespace) -> int:
    """Run the release engine in-process.

    Delegates to worktree_release.main(), the same entry point the fabric's
    bash wrapper subprocesses to, so behavior (dry-run default, rescue
    classification, report file, exit code on error/partial cleanup) is
    identical between both entrances.
    """
    _engine.ensure_engine_on_path()
    from worktree_release import main as worktree_release_main

    return worktree_release_main(build_worktree_release_argv(args))
