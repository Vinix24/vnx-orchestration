#!/usr/bin/env python3
"""red_run_claims: turn a PR description into ``red_on_base.py --claims`` input.

A claim starts with a label from ``report_body_contract.RED_RUN_LABELS`` (case as in the
tuple, optionally wrapped in ``**``) followed by ``:`` or ``on <sha>:``; its nodeids are the
backticked or bare ``tests/...py::...`` tokens on that line and on the ``- `` / ``* `` list
lines below it, up to the first blank line. Deduplicated, in body order. The labels are
imported, never copied, so a new label reaches this parser.

Whitespace is allowed only inside a parametrize part: ``tests/test_a.py::test_one[hello
world]`` is a nodeid, ``tests/my test.py::test_one`` is not. A token inside a claim that
looks like a nodeid but cannot be read is never dropped silently, because a claim the parser
cannot read must not become "no claim": it exits 3 and writes no claims file.

Exit 2 when the body cannot be read; exit 3 when a claim cannot be read.
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
# A nodeid: a whitespace-free path and test address. The second branch is the parametrize
# form, where whitespace is allowed inside ``[...]`` and nowhere else. The first branch is
# the older all-whitespace-free shape, kept so every id it already accepted still parses.
_NODEID = re.compile(r"^tests/[^\s`]+\.py::(?:[^\s`]+|[^\s`\[`]+\[[^`]*\])$")
_LIST_MARKERS = ("- ", "* ")


class UnreadableClaim(Exception):
    """A token inside a claim that is shaped like a nodeid but cannot be read as one.

    The caller refuses on this instead of dropping the token, so an unreadable claim never
    turns into "no claim".
    """

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = list(dict.fromkeys(tokens))
        super().__init__(", ".join(self.tokens))


def _looks_like_a_claim(token: str) -> bool:
    """True when *token* is shaped like a nodeid: a path under ``tests/`` plus ``.py::``."""
    return token.startswith("tests/") and ".py::" in token


def _unreadable(token: str, backticked: bool) -> bool:
    """True when *token* claims a red run but is not a nodeid this parser can read.

    A backticked token is bounded by its backticks, so the whole token is the claim and every
    shape that is not a nodeid is refused. A bare token is no more than one whitespace-split
    word, so an opening ``[`` without its ``]`` means the parameter part was split on a space
    inside it: that is refused before the nodeid shape is consulted, because the first half of
    a split id still looks like a whitespace-free nodeid and must not be claimed as one.
    """
    if not _looks_like_a_claim(token):
        return False
    if not backticked and "[" in token and "]" not in token:
        return True
    return not _NODEID.match(token)


def _take(token: str, backticked: bool, found: list[str], unreadable: list[str]) -> None:
    """Sort one token of a claim line into *found* or *unreadable*; other words go to neither."""
    if _unreadable(token, backticked):
        unreadable.append(token)
    elif _NODEID.match(token):
        found.append(token)


def _nodeids(line: str) -> list[str]:
    """The backticked and bare ``tests/...py::...`` tokens of one claim line, in reading order.

    Raises ``UnreadableClaim`` when the line holds a token shaped like a nodeid that cannot be
    read; nothing of that line is kept then, and the caller writes no claims file.
    """
    found: list[str] = []
    unreadable: list[str] = []
    for token in _BACKTICKED.findall(line):
        _take(token, True, found, unreadable)
    for token in _BACKTICKED.sub(" ", line).split():
        _take(token.rstrip(",;."), False, found, unreadable)
    if unreadable:
        raise UnreadableClaim(unreadable)
    return found


def parse_claims(body: str) -> list[str]:
    """Claimed red-on-base nodeids from *body*, deduplicated in body order.

    Raises ``UnreadableClaim`` for a token in a claim that cannot be read as a nodeid.
    """
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
    try:
        claims = parse_claims(body)
    except UnreadableClaim as exc:
        for token in exc.tokens:
            print(f"red_run_claims: unreadable claim: {token}", file=sys.stderr)
        return 3
    text = "".join(f"{claim}\n" for claim in claims)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
