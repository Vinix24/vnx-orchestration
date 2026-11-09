"""DR-1: a stable finding id and a status that never closes silently.

Pure tests, no tmp files and no git. Plan "Klaar per levering" item 2: the
same finding on another line gives the same id, another function gives another
id, a vanished anchor gives ``unverifiable`` and ``fixed`` -> ``regressed`` is
recognised.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import review_findings as rf  # noqa: E402

SOURCE = """\
class Outer:
    def first(self):
        value = 1
        return value

    def second(self):
        value = 1

        return value


def module_function():
    return 1
"""

LONG_SOURCE = """\
def handler():
    a = 1
    b = 2
    c = 3
    d = 4
    e = 5
    f = 6
    g = 7
    h = 8
    i = 9
    j = 10
    k = 11
    l = 12
"""


# --- identity --------------------------------------------------------------


def test_same_finding_moved_ten_lines_in_same_function_keeps_id():
    low = {"file_path": "./scripts/lib/x.py", "line": 3, "message": "unused variable value"}
    high = {"file_path": "scripts/lib/x.py", "line": 13, "message": "unused variable value"}
    assert rf.id_for_finding("codex", low, LONG_SOURCE) == rf.id_for_finding(
        "codex", high, LONG_SOURCE
    )


def test_same_text_in_another_function_gets_another_id():
    first = {"file_path": "a.py", "line": 3, "message": "same text"}
    second = {"file_path": "a.py", "line": 7, "message": "same text"}
    assert rf.id_for_finding("codex", first, SOURCE) != rf.id_for_finding(
        "codex", second, SOURCE
    )


def test_same_text_in_another_seat_gets_another_id():
    assert rf.finding_id("codex", "a.py", "Outer.first", "text") != rf.finding_id(
        "kimi", "a.py", "Outer.first", "text"
    )


def test_text_differing_only_in_line_reference_gets_same_id():
    assert rf.finding_id("codex", "foo.py", "", "see foo.py:12") == rf.finding_id(
        "codex", "foo.py", "", "see foo.py:40"
    )


def test_path_normalises_to_posix_without_dot_prefix():
    base = rf.finding_id("codex", "scripts/lib/x.py", "", "text")
    assert rf.finding_id("codex", "./scripts/lib/x.py", "", "text") == base
    assert rf.finding_id("codex", "scripts\\lib\\x.py", "", "text") == base


# --- normalisation ---------------------------------------------------------


def test_normalize_nfc_and_casefold():
    composed = "caf\u00e9 BUG"
    decomposed = unicodedata.normalize("NFD", composed)
    assert rf.normalize_text(composed) == rf.normalize_text(decomposed)
    assert rf.normalize_text("Caf\u00e9 BUG") == rf.normalize_text("cafe\u0301 bug")


def test_normalize_collapses_whitespace():
    assert rf.normalize_text("  a\t\n  b   c ") == "a b c"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("broken call in foo.py:12", "broken call in foo.py"),
        ("broken call in foo.py:123:7", "broken call in foo.py"),
        ("broken call in ./scripts/foo.py:12", "broken call in ./scripts/foo.py"),
        ("broken call at line 12", "broken call at"),
        ("broken call at lines 12-14", "broken call at"),
        ("broken call at L123", "broken call at"),
        ("broken call see :123: here", "broken call see here"),
        ("broken call see :123:7: here", "broken call see here"),
    ],
)
def test_normalize_drops_line_references(text, expected):
    assert rf.normalize_text(text) == expected


def test_normalize_keeps_numbers_that_are_not_line_references():
    assert rf.normalize_text("exit 3") == "exit 3"
    assert rf.normalize_text("mode 0o444") == "mode 0o444"
    assert rf.normalize_text("450 lines") == "450 lines"
    assert rf.normalize_text("bare 123") == "bare 123"


def test_normalize_keeps_clock_times():
    assert rf.normalize_text("ran at 12:34:56") == "ran at 12:34:56"


def test_normalize_rejects_non_string():
    with pytest.raises(TypeError):
        rf.normalize_text(None)  # type: ignore[arg-type]


# --- symbolic context ------------------------------------------------------


def test_symbolic_context_nested_class_method():
    source = "class Outer:\n    class Inner:\n        def method(self):\n            return 1\n"
    assert rf.symbolic_context(source, 4) == "Outer.Inner.method"


def test_symbolic_context_method_and_async_function():
    assert rf.symbolic_context("class A:\n    def m(self):\n        pass\n", 3) == "A.m"
    assert rf.symbolic_context("async def fetch():\n    return 1\n", 2) == "fetch"


@pytest.mark.parametrize(
    "source, line",
    [
        ("x = 1\ny = 2\n", 2),  # module level
        ("def broken(:\n    pass\n", 1),  # unparseable
        ("const x = 1;\nfunction f() {}\n", 2),  # non-Python
        (SOURCE, 999),  # outside every scope
        (SOURCE, None),
        (None, 3),
    ],
)
def test_symbolic_context_empty_cases(source, line):
    assert rf.symbolic_context(source, line) == ""


# --- transitions -----------------------------------------------------------


@pytest.mark.parametrize(
    "previous, answer, anchor, expected",
    [
        (None, None, True, rf.STATUS_OPEN),  # unanswered id stays open
        ("open", "", True, rf.STATUS_OPEN),
        (None, "not-a-status", True, rf.STATUS_OPEN),
        (None, "open", True, rf.STATUS_OPEN),
        ("open", "fixed", True, rf.STATUS_FIXED),
        (None, "regressed", True, rf.STATUS_REGRESSED),
        ("fixed", "open", True, rf.STATUS_REGRESSED),  # fixed -> there again
        ("fixed", "regressed", True, rf.STATUS_REGRESSED),
        ("fixed", "fixed", True, rf.STATUS_FIXED),
        (None, "waived", True, rf.STATUS_OPEN),  # reviewer cannot waive
        ("open", "waived", True, rf.STATUS_OPEN),
        ("waived", "waived", True, rf.STATUS_WAIVED),  # carried from previous
        ("waived", "fixed", True, rf.STATUS_FIXED),
        (None, None, False, rf.STATUS_UNVERIFIABLE),  # anchor wins
        ("open", "fixed", False, rf.STATUS_UNVERIFIABLE),
        ("fixed", "fixed", False, rf.STATUS_UNVERIFIABLE),
        ("waived", "waived", False, rf.STATUS_UNVERIFIABLE),
    ],
)
def test_next_status(previous, answer, anchor, expected):
    assert rf.next_status(previous, answer, anchor) == expected


# --- blocking (the C2 rule for DR-4a) --------------------------------------


@pytest.mark.parametrize(
    "status, expected",
    [
        ("open", True),
        ("regressed", True),
        ("unverifiable", True),
        ("fixed", False),
        ("waived", False),
        (None, True),  # fail-closed
        ("something-else", True),
    ],
)
def test_blocks(status, expected):
    assert rf.blocks(status) is expected


# --- id_for_finding contract ----------------------------------------------


def test_id_for_finding_requires_message_and_mapping():
    with pytest.raises(ValueError):
        rf.id_for_finding("codex", {"file_path": "a.py", "line": 1}, SOURCE)
    with pytest.raises(ValueError):
        rf.id_for_finding("codex", {"file_path": "a.py", "line": 1, "message": "  "}, SOURCE)
    with pytest.raises(TypeError):
        rf.id_for_finding("codex", ["not", "a", "mapping"], SOURCE)  # type: ignore[arg-type]

