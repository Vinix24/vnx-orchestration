#!/usr/bin/env python3
"""vnx guard — install and check the user-level T0 context guard.

  vnx guard install [--dry-run] [--config-dir DIR]...
  vnx guard check   [--config-dir DIR]... [--scan-root DIR]...

The logic lives in scripts/lib/guard_install.py and scripts/lib/guard_check.py. This file is the
one argument parser both entrances share: ``bin/vnx guard`` runs it directly and the pip CLI
(``vnx_cli/commands/guard.py``) hands it the same argv.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import guard_check
import guard_install


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vnx guard", description="Install and check the user-level T0 context guard.")
    subs = parser.add_subparsers(dest="guard_command", metavar="COMMAND")
    install = subs.add_parser(
        "install", help="register the guard in the Claude Code settings of every account")
    install.add_argument("--dry-run", action="store_true",
                         help="list the accounts and print the diff per file; write nothing")
    check = subs.add_parser(
        "check", help="report, per account and per VNX project, where the guard runs")
    check.add_argument("--scan-root", action="append", default=[], metavar="DIR",
                       help="also inventory every .vnx-project-id under DIR (depth 3); repeatable")
    for sub in (install, check):
        sub.add_argument("--config-dir", action="append", default=[], metavar="DIR",
                         help="a Claude config directory instead of discovery; repeatable")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    home = guard_install.home_dir()
    if args.guard_command == "install":
        return guard_install.run_install(home=home, config_dirs=args.config_dir,
                                         dry_run=args.dry_run, out=sys.stdout)
    if args.guard_command == "check":
        return guard_check.run_check_command(home=home, config_dirs=args.config_dir,
                                             scan_roots=args.scan_root, out=sys.stdout)
    parser.print_usage(sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
