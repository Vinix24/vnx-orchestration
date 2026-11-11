#!/usr/bin/env python3
"""Operator steering CLI (ADR-072, OB-1a): ``bind``. Exit codes (docs/EXIT_CODES.md): 0 ok, 2 invalid arguments."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import operator_steering_source as source
from project_root import resolve_central_data_dir, resolve_project_id


def _state_dir(args: argparse.Namespace) -> Path:
    return Path(args.state_dir) if args.state_dir else resolve_central_data_dir(resolve_project_id()) / "state"


def _cmd_bind(args: argparse.Namespace) -> int:
    try:
        record = source.bind(_state_dir(args), Path(args.projects_root), [args.transcript_dir], Path(args.memory_dir))
    except source.BindingError as exc:
        print(f"bind geweigerd: {exc}", file=sys.stderr)
        return 2
    print(f"gebonden: project {record['project_id']}, transcript-map {args.transcript_dir}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="project state dir (default: central store of this project)")
    parser.add_argument("--projects-root", default=str(source.DEFAULT_PROJECTS_ROOT))
    bind = parser.add_subparsers(dest="command", required=True).add_parser("bind", help="bind to a transcript dir and a memory dir")
    bind.add_argument("--transcript-dir", required=True, help="slug; write --transcript-dir=<slug> (slugs start with a dash)")
    bind.add_argument("--memory-dir", required=True)
    bind.set_defaults(func=_cmd_bind)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
