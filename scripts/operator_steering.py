#!/usr/bin/env python3
"""Operator steering CLI (ADR-072): ``bind`` and ``show --prompt``.

Exit codes (docs/EXIT_CODES.md): 0 ok, 1 source not readable or prompt not found,
2 invalid arguments or a refused binding. ``show`` prints to the terminal and writes nothing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import operator_steering_source as source
from project_root import resolve_central_data_dir, resolve_project_id


def _state_dir(args: argparse.Namespace) -> Path:
    if args.state_dir:
        return Path(args.state_dir)
    return resolve_central_data_dir(resolve_project_id()) / "state"


def _cmd_bind(args: argparse.Namespace) -> int:
    try:
        record = source.bind(_state_dir(args), Path(args.projects_root), [args.transcript_dir], Path(args.memory_dir))
    except source.BindingError as exc:
        print(f"bind geweigerd: {exc}", file=sys.stderr)
        return 2
    print(f"gebonden: project {record['project_id']}, transcript-map {args.transcript_dir}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    result = source.read_prompts(_state_dir(args), Path(args.projects_root))
    if result.state != "ok":
        names = ", ".join(result.refused_files)
        print(f"bron {result.state}: {result.reason}" + (f" ({names})" if names else ""), file=sys.stderr)
        return 1
    for prompt in result.prompts:
        if prompt.prompt_id == args.prompt:
            print(f"{prompt.source_file}:{prompt.line}")
            print(prompt.text)
            return 0
    counts = ", ".join(f"{k}={v}" for k, v in sorted(result.excluded.items())) or "geen"
    print(f"prompt-id niet gevonden onder de doorgelaten prompts; uitgesloten per reden: {counts}", file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="project state dir (default: central store of this project)")
    parser.add_argument("--projects-root", default=str(source.DEFAULT_PROJECTS_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind", help="bind this project to a transcript dir and a memory dir")
    bind.add_argument("--transcript-dir", required=True, help="slug directly under the projects root; write --transcript-dir=<slug>, slugs start with a dash")
    bind.add_argument("--memory-dir", required=True)
    bind.set_defaults(func=_cmd_bind)
    show = sub.add_parser("show", help="print one passed prompt")
    show.add_argument("--prompt", required=True, help="prompt id (16 hex)")
    show.set_defaults(func=_cmd_show)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
