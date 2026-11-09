"""DR-1: a stable finding id, including scopes inside compound statements.

Pure tests, no tmp files and no git. Plan "Klaar per levering" item 2: the
same finding on another line gives the same id, another function gives another
id — also when the functions sit inside ``if``/``try``/``with``/``for``/
``match``. The fail-closed status tests follow in the second half of the split.
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

CONDITIONAL_SOURCE = """\
if TYPE_CHECKING:
    def first():
        value = 1

    def second():
        value = 1
"""

FALLBACK_SOURCE = """\
try:
    def fast():
        return 1
except ImportError:
    def slow():
        return 2
"""

CLASS_CONDITIONAL_SOURCE = """\
class Outer:
    if True:
        def method(self):
            return 1
"""

WITH_SOURCE = """\
with open("config.toml") as handle:
    def inside_with():
        return handle
"""

FOR_ELSE_SOURCE = """\
for item in ():
    def in_loop():
        return 1
else:
    def in_else():
        return 2
"""

FINALLY_SOURCE = """\
try:
    def in_try():
        return 1
finally:
    def in_finally():
        return 2
"""

MATCH_SOURCE = """\
match command:
    case "run":
        def run():
            return 1
    case _:
        def other():
            return 2
"""

TRY_STAR_SOURCE = """\
try:
    def primary():
        return 1
except* ValueError:
    def fallback():
        return 2
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


def test_symbolic_context_distinguishes_functions_inside_module_level_if():
    first = {"file_path": "a.py", "line": 3, "message": "same text"}
    second = {"file_path": "a.py", "line": 6, "message": "same text"}
    assert rf.symbolic_context(CONDITIONAL_SOURCE, 3) == "first"
    assert rf.symbolic_context(CONDITIONAL_SOURCE, 6) == "second"
    assert rf.id_for_finding("codex", first, CONDITIONAL_SOURCE) != rf.id_for_finding(
        "codex", second, CONDITIONAL_SOURCE
    )


def test_symbolic_context_distinguishes_try_and_except_fallback():
    assert rf.symbolic_context(FALLBACK_SOURCE, 3) == "fast"
    assert rf.symbolic_context(FALLBACK_SOURCE, 5) == "slow"


def test_symbolic_context_method_inside_if_in_class_body():
    assert rf.symbolic_context(CLASS_CONDITIONAL_SOURCE, 4) == "Outer.method"


@pytest.mark.parametrize(
    "source, line, expected",
    [
        (WITH_SOURCE, 3, "inside_with"),
        (FOR_ELSE_SOURCE, 3, "in_loop"),
        (FOR_ELSE_SOURCE, 6, "in_else"),
        (FINALLY_SOURCE, 3, "in_try"),
        (FINALLY_SOURCE, 6, "in_finally"),
        (MATCH_SOURCE, 4, "run"),
        (MATCH_SOURCE, 7, "other"),
        (TRY_STAR_SOURCE, 3, "primary"),
        (TRY_STAR_SOURCE, 6, "fallback"),
    ],
)
def test_symbolic_context_inside_compound_statements(source, line, expected):
    assert rf.symbolic_context(source, line) == expected


def test_symbolic_context_module_level_code_inside_if_stays_empty():
    assert rf.symbolic_context("if True:\n    value = 1\n", 2) == ""


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


# --- id_for_finding contract ----------------------------------------------


def test_id_for_finding_requires_message_and_mapping():
    with pytest.raises(ValueError):
        rf.id_for_finding("codex", {"file_path": "a.py", "line": 1}, SOURCE)
    with pytest.raises(ValueError):
        rf.id_for_finding("codex", {"file_path": "a.py", "line": 1, "message": "  "}, SOURCE)
    with pytest.raises(TypeError):
        rf.id_for_finding("codex", ["not", "a", "mapping"], SOURCE)  # type: ignore[arg-type]

