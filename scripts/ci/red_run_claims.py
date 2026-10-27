#!/usr/bin/env python3
"""red_run_claims: turn a PR description into ``red_on_base.py --claims`` input.

A claim starts with a label from ``report_body_contract.RED_RUN_LABELS`` (case as in the
tuple, optionally wrapped in ``**``) followed by ``:`` or ``on <sha>:``; its nodeids are the
backticked or bare ``tests/...py::...`` tokens on that line and on the ``- `` / ``* `` list
lines below it, up to the first blank line. Deduplicated, in body order. The labels are
imported, never copied, so a new label reaches this parser.

Exit 2 when the body cannot be read; no claims file is written then (an empty file would
silently drop every claim).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from report_body_contract import RED_RUN_LABELS

_LABEL_ALTERNATION = "|".join(
    re.escape(label) for label in sorted(RED_RUN_LABELS, key=len, reverse=True))
_CLAIM_LINE = re.compile(
    rf"^(?:\*\*)?(?:{_LABEL_ALTERNATION})(?:\*\*)?(?:\s+on\s+[^\s:]+)?\s*:")
_BACKTICKED = re.compile(r"`([^`]+)`")
_NODEID = re.compile(r"^tests/[^\s`]+\.py::[^\s`]+$")
_LIST_MARKERS = ("- ", "* ")


def _nodeids(line: str) -> list[str]:
    """The backticked and bare ``tests/...py::...`` tokens of one line, in reading order."""
    found = _BACKTICKED.findall(line)
    found.extend(token.rstrip(",;.") for token in _BACKTICKED.sub(" ", line).split())
    return [token for token in found if _NODEID.match(token)]


def parse_claims(body: str) -> list[str]:
    """Claimed red-on-base nodeids from *body*, deduplicated in body order."""
    claims: list[str] = []
    in_list = False
    for raw in body.splitlines():
        if not raw.strip():
            in_list = False
        elif _CLAIM_LINE.match(raw):
            in_list = True
            claims.extend(_nodeids(raw))
        elif in_list and raw.lstrip().startswith(_LIST_MARKERS):
            claims.extend(_nodeids(raw))
        else:
            in_list = False
    return list(dict.fromkeys(claims))


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("body", nargs="?", help="PR body file; stdin when omitted or '-'")
    parser.add_argument("--out", help="claims file to write; stdout when omitted")
    args = parser.parse_args(argv)
    try:
        body = (sys.stdin.read() if args.body in (None, "-")
                else Path(args.body).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"red_run_claims: cannot read the PR body: {exc}", file=sys.stderr)
        return 2
    text = "".join(f"{claim}\n" for claim in parse_claims(body))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
