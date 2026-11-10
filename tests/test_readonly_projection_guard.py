"""reattr L5 / ADR-068 besluit 10 and 18: the learning loop writes SQL only behind
an approval gate, and never anywhere a reviewer has not read.

``scripts/learning_loop.py`` is the one QI writer this track gates (ADR-068 besluit
10). L4 gave its persist path an approval record bound to the exact rows; this guard
is the regression net for the next change: every static SQL write in the module must
sit in a function named below with a reason, a dynamic statement the scanner cannot
read fails closed (``unreadable_sql``), and an allowlist entry that names no actual
write fails as stale. The guard takes the module path, so the fixtures under
``tests/fixtures/readonly_guard/`` prove both directions against it.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEARNING_LOOP = ROOT / "scripts" / "learning_loop.py"
FIXTURES = ROOT / "tests" / "fixtures" / "readonly_guard"

# Statements whose first keyword is one of these mutate a store. A literal SELECT or
# EXPLAIN is a read, a PRAGMA only in a proven read-only form (see below). Any other keyword (WITH, VACUUM, a non-SQL string)
# cannot be classified with certainty, so it fails closed like an unreadable one.
WRITE_KEYWORDS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"})
READ_KEYWORDS = frozenset({"SELECT", "PRAGMA", "EXPLAIN"})
SQL_METHODS = frozenset({"execute", "executemany", "executescript"})
# Always a write, whatever the argument: splitting a script into statements needs an
# SQL tokenizer (a string or comment can hold a ``;``) and this guard must not rely on one.
ALWAYS_WRITE_METHODS = frozenset({"executemany", "executescript"})
# PRAGMA is a read only in a form proven read-only, from these two closed sets. Any
# ``= value``, any parenthesized argument on another name and any other name is a write.
PRAGMA_WITH_ARGUMENT = frozenset({
    "table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
    "foreign_key_list", "integrity_check", "quick_check",
})
PRAGMA_BARE = PRAGMA_WITH_ARGUMENT | frozenset({
    "user_version", "schema_version", "journal_mode", "database_list", "compile_options",
})
_PRAGMA_FORM = re.compile(r"PRAGMA\s+(?:\w+\.)?(\w+)\s*(.*)\Z", re.IGNORECASE | re.DOTALL)
_QUOTES = "'\"`["
UNREADABLE_SQL = "unreadable_sql"

# One line per entry, each naming why this function may write SQL. Built from
# origin/main after L4 (dlv-e89570f23f95). A stale entry fails
# ``test_every_allowlist_entry_is_a_real_write_and_every_write_is_allowlisted``.
# The approval-gated bridge itself (``_write_planned``) contains no execute call:
# its rows go through ``scripts/lib/pattern_upsert.py``, which ADR-068 besluit 10
# leaves outside this track. Review D2/D9 before adding a new writer to the module.
ALLOWED_WRITES = {
    "load_pattern_metrics": "CREATE TABLE IF NOT EXISTS pattern_usage, the loop's own bookkeeping table, on the in-memory shadow copy unless persist mode; it writes no intelligence row",
    "update_confidence_scores": "UPDATE pattern_usage bookkeeping counters; no intelligence table row",
    "ingest_approved_rules": "INSERT prevention_rules only for rules the operator already marked status='approved' in pending_rules.json; persist mode only, pre-L4 operator gate",
    "save_pattern_metrics": "INSERT OR REPLACE pattern_usage bookkeeping rows; persist mode only",
    "_apply_supersede": "operator-approved archival disposition: apply_archival_decision validates approval_id, reason and approver before valid_until is set",
    "_apply_archive": "operator-approved archival disposition: only the DELETE of one pattern_usage row after the same validation",
}


@dataclass(frozen=True)
class Finding:
    """One SQL-writing call: the innermost function, its line, and why it blocks."""

    function: str
    line: int
    reason: str

    def render(self) -> str:
        return f"{self.function}:{self.line}: {self.reason}"


def _literal_sql_head(node: ast.AST) -> str | None:
    """The readable leading text of a SQL argument, or None when there is none.

    A plain string is itself. An f-string or a ``+``/``%`` concatenation yields the
    literal prefix before the first interpolated value; ``"INSERT " + table`` is
    readable, ``table + " INSERT"`` is not. ``"...".format(x)`` yields the template.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                break
        return "".join(parts) or None
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _literal_sql_head(node.left)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _literal_sql_head(node.func.value)
    return None


def _strip_leading_comments(sql: str) -> str:
    """``sql`` after leading whitespace and ``--`` / ``/* */`` comments. An unclosed
    comment leaves nothing readable and yields an empty string."""
    body = sql.lstrip()
    while True:
        if body.startswith("--"):
            newline = body.find("\n")
            if newline == -1:
                return ""
            body = body[newline + 1:].lstrip()
        elif body.startswith("/*"):
            end = body.find("*/", 2)
            if end == -1:
                return ""
            body = body[end + 2:].lstrip()
        else:
            return body


def _first_keyword(sql: str) -> str:
    """The first SQL word, after leading whitespace and ``--`` / ``/* */`` comments."""
    words = _strip_leading_comments(sql).split()
    return words[0].upper() if words else ""


def _pragma_is_read(statement: str) -> bool:
    """True only for a PRAGMA in one of the two proven read-only forms."""
    match = _PRAGMA_FORM.match(_strip_leading_comments(statement).rstrip())
    if match is None:
        return False
    name, rest = match.group(1).lower(), match.group(2)
    if not rest:
        return name in PRAGMA_BARE
    return rest.startswith("(") and rest.endswith(")") and name in PRAGMA_WITH_ARGUMENT


def _statements(head: str, complete: bool) -> list[str] | None:
    """The statements in ``head``, or None when the split is not certain.

    Without a ``;`` the head is one statement. With one, a quote anywhere or a tail
    that is not a complete literal makes the split uncertain; otherwise comments are
    removed and the head is cut on ``;`` (no quote means no ``;`` inside a string).
    """
    if ";" not in head:
        return [head]
    if not complete or any(quote in head for quote in _QUOTES):
        return None
    without_comments = re.sub(r"--[^\n]*", " ", head)
    if "/*" in re.sub(r"/\*.*?\*/", " ", without_comments, flags=re.DOTALL):
        return None
    without_comments = re.sub(r"/\*.*?\*/", " ", without_comments, flags=re.DOTALL)
    return [part for part in without_comments.split(";") if part.strip()]


def _classify_statement(statement: str) -> str | None:
    """``write:<KEYWORD>`` for a write, UNREADABLE_SQL when it cannot be classified,
    None for a proven read."""
    keyword = _first_keyword(statement)
    if keyword in WRITE_KEYWORDS:
        return f"write:{keyword}"
    if keyword == "PRAGMA":
        return None if _pragma_is_read(statement) else "write:PRAGMA"
    return None if keyword in READ_KEYWORDS else UNREADABLE_SQL


def _classify_sql(argument: ast.AST) -> str | None:
    """The blocking reason for an ``execute`` argument: a write in any statement
    wins over an unreadable one; None only when every statement is a proven read."""
    head = _literal_sql_head(argument)
    if head is None:
        return UNREADABLE_SQL
    complete = isinstance(argument, ast.Constant)
    statements = _statements(head, complete)
    if statements is None:
        return UNREADABLE_SQL
    reasons = [_classify_statement(statement) for statement in statements] or [UNREADABLE_SQL]
    writes = [reason for reason in reasons if reason is not None and reason.startswith("write:")]
    if writes:
        return writes[0]
    return UNREADABLE_SQL if UNREADABLE_SQL in reasons else None


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> str:
    """Innermost function name around ``call``, or ``<module>`` at module level."""
    node: ast.AST = call
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def scan_module(path: Path) -> list[Finding]:
    """Every SQL-writing call in ``path``: static writes in any statement, unreadable
    statements, writing PRAGMAs and every ``executemany`` / ``executescript`` call (a
    write even when its argument looks like a read)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = _parent_map(tree)
    findings: list[Finding] = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        if call.func.attr not in SQL_METHODS:
            continue
        function = _enclosing_function(call, parents)
        if call.func.attr in ALWAYS_WRITE_METHODS:
            findings.append(Finding(function, call.lineno, f"write:{call.func.attr}"))
            continue
        reason = _classify_sql(call.args[0]) if call.args else UNREADABLE_SQL
        if reason is not None:
            findings.append(Finding(function, call.lineno, reason))
    return findings


def blocking(findings: list[Finding], allowed: dict[str, str]) -> list[Finding]:
    """Findings whose function the allowlist does not name. Reading the list is the
    positive evidence; a missing, empty or unknown function name stays blocking."""
    return [finding for finding in findings if finding.function not in allowed]


def stale_entries(findings: list[Finding], allowed: dict[str, str]) -> list[str]:
    """Allowlist names that contain no actual write: an entry gone stale.

    An ``unreadable_sql`` finding is not a write, so a function whose only call is
    a dynamic statement does not keep its entry alive (the entry may still exempt
    that call from ``blocking`` — it just cannot claim the module was reviewed).
    """
    written = {finding.function for finding in findings if finding.reason.startswith("write:")}
    return sorted(name for name in allowed if name not in written)


def test_the_learning_loop_writes_only_inside_allowlisted_functions():
    findings = scan_module(LEARNING_LOOP)
    assert [finding.render() for finding in blocking(findings, ALLOWED_WRITES)] == []
    assert stale_entries(findings, ALLOWED_WRITES) == []
    # The guard is not vacuous: the six known writes are still what it sees.
    assert {finding.function for finding in findings} == set(ALLOWED_WRITES)


def test_every_allowlist_entry_gives_its_reason_on_one_line():
    for function, reason in ALLOWED_WRITES.items():
        assert reason.strip() == reason and len(reason) >= 20, function
        assert "\n" not in reason, function


@pytest.mark.parametrize(
    "fixture_name, expected",
    [("write_outside_allowlist.py", [("persist_without_approval", 9, "write:INSERT")])],
)
def test_an_injected_insert_outside_the_allowlist_fails_with_function_and_line(fixture_name, expected):
    findings = scan_module(FIXTURES / fixture_name)
    assert [(f.function, f.line, f.reason) for f in findings] == expected
    problems = blocking(findings, ALLOWED_WRITES)
    assert problems == findings
    assert "persist_without_approval" in problems[0].render()
    assert ":9:" in problems[0].render()


def test_a_write_inside_an_allowlisted_function_passes_the_guard():
    findings = scan_module(FIXTURES / "allowed_function_write.py")
    scoped = {"save_pattern_metrics": "fixture copy of the loop's bookkeeping write"}
    assert blocking(findings, scoped) == []
    assert stale_entries(findings, scoped) == []


def test_an_unreadable_dynamic_statement_fails_closed_outside_the_allowlist():
    findings = scan_module(FIXTURES / "dynamic_sql.py")
    assert [(f.function, f.line, f.reason) for f in findings] == [("ingest_rules_unreadable", 9, UNREADABLE_SQL)]
    assert blocking(findings, ALLOWED_WRITES) == findings
    assert blocking(findings, {"ingest_rules_unreadable": "fixture: reviewed dynamic statement"}) == []


def test_a_literal_select_is_a_read_but_every_executemany_is_a_write():
    findings = scan_module(FIXTURES / "reader_and_executemany.py")
    assert [(f.function, f.line, f.reason) for f in findings] == [("write_anyway", 13, "write:executemany")]


@pytest.mark.parametrize(
    "snippet, expected_reason",
    [
        ("conn.execute()", UNREADABLE_SQL),
        ("conn.execute(None)", UNREADABLE_SQL),
        ("conn.execute(statement)", UNREADABLE_SQL),
        ("conn.execute(f'{table} rows')", UNREADABLE_SQL),
        ('conn.execute("")', UNREADABLE_SQL),
        ('conn.execute("VACUUM")', UNREADABLE_SQL),
        ('conn.execute("INSERT INTO t VALUES (1)")', "write:INSERT"),
    ],
)
def test_missing_none_malformed_and_unknown_arguments_stay_blocking(tmp_path, snippet, expected_reason):
    module = tmp_path / "case.py"
    module.write_text(
        "def not_allowlisted(conn, statement=None, table=None):\n    " + snippet + "\n",
        encoding="utf-8",
    )
    findings = scan_module(module)
    assert [(f.function, f.reason) for f in findings] == [("not_allowlisted", expected_reason)]
    assert blocking(findings, ALLOWED_WRITES) == findings


def test_a_write_at_module_level_is_outside_every_allowed_function(tmp_path):
    module = tmp_path / "module_level.py"
    module.write_text('conn.execute("INSERT INTO t VALUES (1)")\n', encoding="utf-8")
    findings = scan_module(module)
    assert [(f.function, f.reason) for f in findings] == [("<module>", "write:INSERT")]
    assert blocking(findings, ALLOWED_WRITES) == findings


def test_a_missing_module_stays_blocking_instead_of_scanning_clean():
    with pytest.raises(FileNotFoundError):
        scan_module(FIXTURES / "does_not_exist.py")


def test_leading_whitespace_and_comments_do_not_hide_a_write(tmp_path):
    module = tmp_path / "commented.py"
    module.write_text(
        "def hidden(conn):\n"
        '    conn.execute("  -- a note\\n  /* another */\\n  DELETE FROM pattern_usage")\n',
        encoding="utf-8",
    )
    findings = scan_module(module)
    assert [(f.function, f.reason) for f in findings] == [("hidden", "write:DELETE")]


def _scan_snippet(tmp_path, snippet: str) -> list[Finding]:
    module = tmp_path / "snippet.py"
    module.write_text("def not_allowlisted(conn):\n    " + snippet + "\n", encoding="utf-8")
    return scan_module(module)


@pytest.mark.parametrize(
    "snippet, expected_reason",
    [
        ('conn.executescript("SELECT 1; DELETE FROM t;")', "write:executescript"),
        ('conn.executescript("SELECT 1")', "write:executescript"),
        ("conn.executescript(script)", "write:executescript"),
        ("conn.executescript()", "write:executescript"),
        ('conn.execute("SELECT 1; DELETE FROM t")', "write:DELETE"),
        ('conn.execute("SELECT 1;\\n-- note\\nUPDATE t SET a = 1;")', "write:UPDATE"),
        ('conn.execute("SELECT 1; /* ; */ DROP TABLE t")', "write:DROP"),
        ('conn.execute("SELECT 1; WITH x AS (SELECT 1) SELECT * FROM x")', UNREADABLE_SQL),
        ('conn.execute("SELECT 1; " + tail)', UNREADABLE_SQL),
        ('conn.execute("SELECT \'a;b\'; DELETE FROM t")', UNREADABLE_SQL),
        ('conn.execute("SELECT 1; /* open")', UNREADABLE_SQL),
        ('conn.execute("SELECT \'a;b\'")', UNREADABLE_SQL),
        ('conn.execute("PRAGMA user_version = 42")', "write:PRAGMA"),
        ('conn.execute("PRAGMA main.user_version=42")', "write:PRAGMA"),
        ('conn.execute("PRAGMA journal_mode = WAL")', "write:PRAGMA"),
        ('conn.execute("PRAGMA journal_mode(WAL)")', "write:PRAGMA"),
        ('conn.execute("PRAGMA user_version(42)")', "write:PRAGMA"),
        ('conn.execute("PRAGMA foo")', "write:PRAGMA"),
        ('conn.execute("PRAGMA foo(1)")', "write:PRAGMA"),
        ('conn.execute("PRAGMA")', "write:PRAGMA"),
        ('conn.execute("PRAGMA table_info(" + name + ")")', "write:PRAGMA"),
        ('conn.execute("SELECT 1; PRAGMA user_version = 1")', "write:PRAGMA"),
    ],
)
def test_every_statement_is_read_and_a_writing_pragma_or_script_blocks(tmp_path, snippet, expected_reason):
    findings = _scan_snippet(tmp_path, snippet)
    assert [(f.function, f.reason) for f in findings] == [("not_allowlisted", expected_reason)]
    assert blocking(findings, ALLOWED_WRITES) == findings
    scoped = {"not_allowlisted": "fixture: reviewed statement"}
    assert blocking(findings, scoped) == []


@pytest.mark.parametrize(
    "snippet",
    [
        'conn.execute("PRAGMA table_info(x)")',
        'conn.execute("PRAGMA table_xinfo(\'x\')")',
        'conn.execute("PRAGMA index_list(x)")',
        'conn.execute("PRAGMA index_info(i)")',
        'conn.execute("PRAGMA index_xinfo(i)")',
        'conn.execute("PRAGMA foreign_key_list(x)")',
        'conn.execute("PRAGMA integrity_check(5)")',
        'conn.execute("PRAGMA quick_check(5)")',
        'conn.execute("PRAGMA user_version")',
        'conn.execute("PRAGMA main.schema_version")',
        'conn.execute("PRAGMA journal_mode")',
        'conn.execute("PRAGMA database_list")',
        'conn.execute("PRAGMA compile_options")',
        'conn.execute("PRAGMA integrity_check")',
        'conn.execute("pragma table_info(x);")',
        'conn.execute("SELECT 1; SELECT 2;")',
        'conn.execute("SELECT 1 -- a; b\\n")',
    ],
)
def test_proven_read_forms_stay_reads(tmp_path, snippet):
    assert _scan_snippet(tmp_path, snippet) == []
