#!/usr/bin/env python3
"""Regression test for the `vnx handoff` argparse duplicate-dest bug.

Background: `handoff_parser` (the `vnx handoff` level) and its `show`/
`mark-ready` sub-subparsers both declare `--project-dir`/`--project-id`
with the same dest. argparse's SubParsersAction parses each subcommand into
a FRESH namespace and then copies every attribute from it back onto the
parent namespace — including untouched defaults — which silently clobbered
an explicit `--project-id`/`--project-dir` given at the parent position
(`vnx handoff --project-id foo show`) back to the subparser's own default
(None / "."). Fixed via `default=argparse.SUPPRESS` on the subparser copies
so an unset flag never overwrites a value already resolved by the parent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vnx_cli import main as vnx_main  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx")
    subparsers = parser.add_subparsers(dest="command")
    vnx_main._register_handoff_subparser(subparsers)
    return parser


class TestHandoffProjectIdNotClobbered:
    def test_project_id_at_parent_position_is_honored_by_show(self) -> None:
        """`vnx handoff --project-id foo show` must resolve project_id=foo,
        not fall back to the show-subparser's own None default."""
        args = _build_parser().parse_args(["handoff", "--project-id", "foo", "show"])
        assert getattr(args, "project_id", None) == "foo"

    def test_project_id_at_sub_position_is_still_honored_by_show(self) -> None:
        """The reverse order must also work — an explicit flag on the
        subcommand itself is honored regardless of position."""
        args = _build_parser().parse_args(["handoff", "show", "--project-id", "foo"])
        assert getattr(args, "project_id", None) == "foo"

    def test_project_dir_at_parent_position_is_honored_by_show(self) -> None:
        args = _build_parser().parse_args(["handoff", "--project-dir", "/x", "show"])
        assert getattr(args, "project_dir", None) == "/x"

    def test_no_project_id_anywhere_falls_back_to_none(self) -> None:
        """Absence of the flag at either level must still resolve to the
        documented auto-resolution fallback (None), not raise."""
        args = _build_parser().parse_args(["handoff", "show"])
        assert getattr(args, "project_id", None) is None
        assert getattr(args, "project_dir", None) == "."

    def test_project_id_at_parent_position_is_honored_by_mark_ready(self) -> None:
        args = _build_parser().parse_args(
            ["handoff", "--project-id", "foo", "mark-ready", "--rotation-id", "rid"]
        )
        assert getattr(args, "project_id", None) == "foo"
        assert args.rotation_id == "rid"

    def test_project_id_at_sub_position_is_still_honored_by_mark_ready(self) -> None:
        args = _build_parser().parse_args(
            ["handoff", "mark-ready", "--rotation-id", "rid", "--project-id", "foo"]
        )
        assert getattr(args, "project_id", None) == "foo"
