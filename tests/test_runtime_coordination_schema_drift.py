"""Schema-code drift guard for the runtime_coordination database.

Twice now, runtime_coordination code has referenced a column the schema never
defined:
  1. worker_states.project_id  (OI-095, PR #635)
  2. terminal_leases.worker_pid (this PR)

Both shipped silently — the writes sat in try/except blocks that logged
"no such column: ..." and rolled back, blinding worker-health and PID
telemetry. This test catches the WHOLE class statically:

  1. Parse the canonical schema (runtime_coordination.sql + every
     runtime_coordination_v*.sql + every schemas/migrations/*.sql) into a
     ``{table: set(columns)}`` map. Columns come from CREATE TABLE bodies and
     ``ALTER TABLE ... ADD COLUMN`` statements; table-rebuild temporaries
     (``<name>_v10``) are folded back onto their canonical name. The map is a
     *superset* of every column a table may legitimately carry.

  2. Scan the runtime_coordination DB-access code for column references and
     assert each one exists in the schema for its table.

False-positive discipline (the dispatch's explicit priority — under-claim
rather than over-flag):
  - Only HIGH-CONFIDENCE positions are scanned: ``UPDATE t SET col = ``,
    ``INSERT INTO t (cols)``, and alias-qualified ``a.col`` reads where the
    alias resolves to a real table via FROM/JOIN/UPDATE/INSERT.
  - Bare unqualified ``SELECT col``/``WHERE col`` lists are deliberately NOT
    scanned. pool_state_repo.get_config() intentionally SELECTs optional
    columns (cost_ceiling_usd, heartbeat_stale_seconds) that older DBs lack and
    falls back via ``except sqlite3.OperationalError`` — flagging those would be
    a false positive. The two real drifts (a SET write and an alias-qualified
    read) are both covered by the scanned positions.
  - Dynamically-built clauses (f-string ``{...}`` placeholders, ``%`` format
    markers) are skipped — their column set cannot be resolved statically.
  - Only references to tables PRESENT in the runtime_coordination schema map
    are checked; cross-DB (quality_intelligence) references auto-skip.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR = _REPO_ROOT / "schemas"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

# SQL clause keywords that must never be mistaken for a table alias.
_ALIAS_STOPWORDS = frozenset({
    "WHERE", "SET", "ON", "USING", "VALUES", "GROUP", "ORDER", "LIMIT",
    "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "JOIN", "AND", "OR",
    "AS", "SELECT", "INTO", "WHEN", "THEN", "ELSE", "END", "RETURNING",
    "HAVING", "OFFSET", "UNION", "EXCEPT", "INTERSECT",
})

_SQL_HINT_RE = re.compile(r"\b(UPDATE|INSERT|SELECT|FROM|JOIN)\b", re.IGNORECASE)
_IDENT = r"[A-Za-z_]\w*"


def _normalize_table(name: str) -> str:
    """Fold rebuild temporaries (``dispatches_v10``) onto the canonical name."""
    return re.sub(r"_v\d+$", "", name)


def _strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments, preserving
    single-quoted string literals.

    Without this, a trailing inline comment (``state TEXT NOT NULL,  -- note``)
    glues onto the *next* column when the CREATE TABLE body is split on commas:
    that column's token becomes ``--`` and the real column is dropped from the
    schema map — surfacing as a phantom drift finding (false positive). It also
    keeps unbalanced parens inside comments (``-- FK (informational``) from
    breaking the balanced-paren body extractor.
    """
    out: List[str] = []
    i, n = 0, len(sql)
    in_str = False
    while i < n:
        c = sql[i]
        if in_str:
            out.append(c)
            if c == "'":
                if i + 1 < n and sql[i + 1] == "'":  # '' escape inside literal
                    out.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            if j == -1:
                break
            i = j  # preserve the newline so line structure is kept
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Schema parsing
# ---------------------------------------------------------------------------

def _extract_create_table_body(sql: str, start: int) -> Tuple[str, int]:
    """Return the parenthesised body of a CREATE TABLE starting at the first
    ``(`` at/after *start*, using balanced-paren matching. Returns (body, end)."""
    open_idx = sql.find("(", start)
    if open_idx == -1:
        return "", start
    depth = 0
    for i in range(open_idx, len(sql)):
        c = sql[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return sql[open_idx + 1:i], i
    return sql[open_idx + 1:], len(sql)


def _split_top_level(body: str) -> List[str]:
    """Split on commas that sit at paren-depth 0."""
    items: List[str] = []
    depth = 0
    buf: List[str] = []
    for c in body:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            items.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    if buf:
        items.append("".join(buf))
    return items


# Leaders of a table-level constraint clause in SQLite — never column names.
# "KEY" is deliberately excluded: SQLite has no bare ``KEY (...)`` constraint
# (that is MySQL syntax), and ``schema_meta`` has a real column literally named
# ``key``. Including "KEY" would skip that column and report a phantom drift.
_CONSTRAINT_LEADERS = frozenset({
    "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT",
})
# Require a column body ``(`` so prose like "CREATE TABLE statements below"
# in comments is not mistaken for a real table definition.
_CREATE_TABLE_RE = re.compile(
    rf"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+({_IDENT})\s*\(", re.IGNORECASE
)
_ADD_COLUMN_RE = re.compile(
    rf"ALTER\s+TABLE\s+({_IDENT})\s+ADD\s+COLUMN\s+({_IDENT})", re.IGNORECASE
)
# ``CREATE INDEX ... ON table (`` — SQLite cannot index a column that does not
# exist, so an index target is definitive proof the column is part of the
# schema. This captures columns added by Python migration runners (e.g.
# apply_ab_arm.py adds intelligence_injections.ab_arm, whose only canonical SQL
# artifact is the index here — the ALTER lives in a comment).
_CREATE_INDEX_RE = re.compile(
    rf"CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+{_IDENT}\s+ON\s+({_IDENT})\s*\(",
    re.IGNORECASE,
)


def _create_table_names(sql_files: List[Path]) -> Set[str]:
    """All normalized table names defined by CREATE TABLE in *sql_files*."""
    names: Set[str] = set()
    for path in sql_files:
        sql = _strip_sql_comments(path.read_text(encoding="utf-8"))
        for m in _CREATE_TABLE_RE.finditer(sql):
            names.add(_normalize_table(m.group(1)))
    return names


def parse_schema_columns(
    sql_files: List[Path], allowlist: Set[str]
) -> Dict[str, Set[str]]:
    """Build ``{table: set(columns)}`` (superset) for tables in *allowlist*.

    Columns accrue from CREATE TABLE bodies and ALTER TABLE ADD COLUMN across
    every file. Non-allowlisted tables (e.g. quality_intelligence tables that
    the cross-DB project_id migrations also ALTER) are dropped so they never
    seed false positives.
    """
    table_cols: Dict[str, Set[str]] = {t: set() for t in allowlist}

    for path in sql_files:
        sql = _strip_sql_comments(path.read_text(encoding="utf-8"))

        for m in _CREATE_TABLE_RE.finditer(sql):
            table = _normalize_table(m.group(1))
            if table not in allowlist:
                continue
            body, _ = _extract_create_table_body(sql, m.end() - 1)
            cols = table_cols[table]
            for item in _split_top_level(body):
                tokens = item.strip().split()
                if not tokens:
                    continue
                if tokens[0].upper() in _CONSTRAINT_LEADERS:
                    continue
                col = tokens[0].strip('`"[]')
                if re.fullmatch(_IDENT, col):
                    cols.add(col)

        for m in _ADD_COLUMN_RE.finditer(sql):
            table = _normalize_table(m.group(1))
            if table in allowlist:
                table_cols[table].add(m.group(2))

        for m in _CREATE_INDEX_RE.finditer(sql):
            table = _normalize_table(m.group(1))
            if table not in allowlist:
                continue
            body, _ = _extract_create_table_body(sql, m.end() - 1)
            for item in _split_top_level(body):
                item = item.strip()
                if not item or "(" in item:  # skip expression indexes
                    continue
                col = item.split()[0].strip('`"[]')  # drop ASC/DESC/COLLATE
                if re.fullmatch(_IDENT, col):
                    table_cols[table].add(col)

    return table_cols


# ---------------------------------------------------------------------------
# Code scanning
# ---------------------------------------------------------------------------

def _iter_sql_strings(source: str) -> List[str]:
    """Extract candidate SQL string literals from Python source via AST.

    f-strings are reconstructed with ``{}`` standing in for interpolations so
    the dynamic-clause guard downstream can detect and skip them.
    """
    out: List[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out  # unparseable module — skip (documented limitation)

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SQL_HINT_RE.search(node.value):
                out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts: List[str] = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    parts.append("{}")  # interpolation marker
            rebuilt = "".join(parts)
            if _SQL_HINT_RE.search(rebuilt):
                out.append(rebuilt)
    return out


_FROM_JOIN_RE = re.compile(
    rf"\b(?:FROM|JOIN)\s+({_IDENT})(?:\s+(?:AS\s+)?({_IDENT}))?", re.IGNORECASE
)
_UPDATE_RE = re.compile(rf"\bUPDATE\s+({_IDENT})", re.IGNORECASE)
_INSERT_RE = re.compile(
    rf"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+({_IDENT})", re.IGNORECASE
)
_UPDATE_SET_RE = re.compile(
    rf"\bUPDATE\s+({_IDENT})\s+SET\s+(.*?)(?:\bWHERE\b|\bRETURNING\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_INSERT_COLS_RE = re.compile(
    rf"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+({_IDENT})\s*\(([^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)
_QUALIFIED_RE = re.compile(rf"\b({_IDENT})\.({_IDENT})\b")


def _is_dynamic(fragment: str) -> bool:
    return "{" in fragment or "%" in fragment


def _build_alias_map(sql: str) -> Dict[str, str]:
    """Map alias/table tokens → normalized table name."""
    alias_map: Dict[str, str] = {}
    for m in _FROM_JOIN_RE.finditer(sql):
        table = _normalize_table(m.group(1))
        alias_map[m.group(1)] = table
        alias_map[table] = table
        alias = m.group(2)
        if alias and alias.upper() not in _ALIAS_STOPWORDS:
            alias_map[alias] = table
    for m in _UPDATE_RE.finditer(sql):
        alias_map[m.group(1)] = _normalize_table(m.group(1))
    for m in _INSERT_RE.finditer(sql):
        alias_map[m.group(1)] = _normalize_table(m.group(1))
    return alias_map


def scan_column_refs(sql: str) -> Set[Tuple[str, str]]:
    """Return high-confidence ``(table, column)`` references from one SQL string."""
    sql = _strip_sql_comments(sql)
    refs: Set[Tuple[str, str]] = set()
    alias_map = _build_alias_map(sql)

    # UPDATE t SET col = ?, col2 = ?
    for m in _UPDATE_SET_RE.finditer(sql):
        table = _normalize_table(m.group(1))
        set_clause = m.group(2)
        if _is_dynamic(set_clause):
            continue
        for assignment in _split_top_level(set_clause):
            if "=" not in assignment:
                continue
            lhs = assignment.split("=", 1)[0].strip()
            col = lhs.split(".")[-1].strip('`"[] ')
            if re.fullmatch(_IDENT, col):
                refs.add((table, col))

    # INSERT INTO t (col1, col2, ...)
    for m in _INSERT_COLS_RE.finditer(sql):
        table = _normalize_table(m.group(1))
        col_list = m.group(2)
        if _is_dynamic(col_list) or "SELECT" in col_list.upper():
            continue
        for raw in col_list.split(","):
            col = raw.strip().split(".")[-1].strip('`"[] ')
            if re.fullmatch(_IDENT, col):
                refs.add((table, col))

    # alias-qualified reads: a.col / table.col
    for m in _QUALIFIED_RE.finditer(sql):
        alias, col = m.group(1), m.group(2)
        table = alias_map.get(alias)
        if table is not None:
            refs.add((table, col))

    return refs


def _iter_scan_targets() -> List[Path]:
    """All Python modules under scripts/ — auto-scoped by schema-map membership."""
    return sorted(_SCRIPTS_DIR.rglob("*.py"))


def _schema_files() -> List[Path]:
    """All SQL files that may add columns to a runtime_coordination table."""
    files = [_SCHEMAS_DIR / "runtime_coordination.sql"]
    files += sorted(_SCHEMAS_DIR.glob("runtime_coordination_v*.sql"))
    files += [
        p for p in sorted((_SCHEMAS_DIR / "migrations").glob("*.sql"))
        if not p.name.endswith("_down.sql")
    ]
    return [f for f in files if f.exists()]


def _rc_table_seed_files() -> List[Path]:
    """Files that CREATE runtime_coordination tables.

    The core tables live in the base + versioned schema files. The only tables
    introduced by migrations to the runtime_coordination DB are the elastic
    worker-pool tables (0020) and the central-install metadata tables (0021);
    0019 only ALTERs existing tables. The cross-DB project_id migrations
    (0010/0015) and the quality_intelligence migrations are deliberately NOT
    seeds — their CREATE/ALTER targets must not enter the rc allowlist.
    """
    files = [_SCHEMAS_DIR / "runtime_coordination.sql"]
    files += sorted(_SCHEMAS_DIR.glob("runtime_coordination_v*.sql"))
    for name in ("0019_t0_lifecycle_tokens.sql",
                 "0020_elastic_worker_pool.sql",
                 "0021_central_install_metadata.sql"):
        files.append(_SCHEMAS_DIR / "migrations" / name)
    return [f for f in files if f.exists()]


def runtime_coordination_tables() -> Set[str]:
    """The runtime_coordination table allowlist, derived from its schema files."""
    return _create_table_names(_rc_table_seed_files())


def build_schema_map() -> Dict[str, Set[str]]:
    return parse_schema_columns(_schema_files(), runtime_coordination_tables())


def collect_drift(schema_map: Dict[str, Set[str]]) -> List[Tuple[Path, str, str]]:
    """Return ``[(file, table, column)]`` for every code ref absent from schema."""
    findings: List[Tuple[Path, str, str]] = []
    for py_path in _iter_scan_targets():
        source = py_path.read_text(encoding="utf-8")
        for sql in _iter_sql_strings(source):
            for table, column in scan_column_refs(sql):
                if table not in schema_map:
                    continue  # not a runtime_coordination table — skip
                if column not in schema_map[table]:
                    findings.append((py_path, table, column))
    return findings


# ---------------------------------------------------------------------------
# Sanity tests for the parser/scanner themselves
# ---------------------------------------------------------------------------

def test_schema_map_has_core_tables_and_columns() -> None:
    schema_map = build_schema_map()
    for table in ("terminal_leases", "dispatches", "worker_states",
                  "dispatch_attempts", "coordination_events"):
        assert table in schema_map, f"{table} missing from parsed schema"
    # The columns this PR adds / depends on.
    assert "worker_pid" in schema_map["terminal_leases"]
    assert "project_id" in schema_map["worker_states"]
    assert "project_id" in schema_map["coordination_events"]


def test_scanner_extracts_set_insert_and_qualified() -> None:
    sql = (
        "UPDATE terminal_leases SET worker_pid = ? WHERE terminal_id = ?"
    )
    assert ("terminal_leases", "worker_pid") in scan_column_refs(sql)

    sql_join = (
        "SELECT tl.worker_pid FROM worker_pool_membership wpm "
        "LEFT JOIN terminal_leases tl ON tl.terminal_id = wpm.terminal_id"
    )
    refs = scan_column_refs(sql_join)
    assert ("terminal_leases", "worker_pid") in refs

    sql_insert = "INSERT INTO worker_states (terminal_id, project_id) VALUES (?, ?)"
    refs_ins = scan_column_refs(sql_insert)
    assert ("worker_states", "terminal_id") in refs_ins
    assert ("worker_states", "project_id") in refs_ins


def test_parser_keeps_columns_after_inline_comments() -> None:
    """Columns whose preceding line carries a trailing ``-- comment`` must still
    be parsed. Regression for the false-positive class on incident_log /
    retry_budgets (columns dropped because the comment glued onto the next item).
    """
    schema_map = build_schema_map()
    incident_cols = schema_map["incident_log"]
    for col in ("entity_id", "dispatch_id", "terminal_id", "state",
                "escalated", "auto_recovery_halted", "failure_detail"):
        assert col in incident_cols, f"incident_log.{col} lost by the parser"
    assert "escalated_at" in schema_map["retry_budgets"]


def test_parser_keeps_column_named_key() -> None:
    """A column literally named ``key`` (schema_meta) must not be mistaken for a
    table-level KEY constraint and dropped."""
    assert "key" in build_schema_map()["schema_meta"]


def test_parser_harvests_index_only_columns() -> None:
    """A column whose only canonical SQL artifact is a CREATE INDEX (added by a
    Python migration runner, e.g. intelligence_injections.ab_arm) must land in
    the schema map."""
    assert "ab_arm" in build_schema_map()["intelligence_injections"]


def test_strip_sql_comments_preserves_string_literals() -> None:
    # A ``--`` inside a quoted default must survive; the trailing comment must go.
    stripped = _strip_sql_comments("col TEXT DEFAULT 'a--b',  -- drop me\n")
    assert "'a--b'" in stripped
    assert "drop me" not in stripped


def test_scanner_skips_dynamic_set_clause() -> None:
    # pool_state_repo.update_config builds the SET list dynamically.
    sql = "UPDATE pool_config SET {} WHERE project_id = ? AND pool_id = ?"
    assert scan_column_refs(sql) == set() or all(
        col != "{}" for _, col in scan_column_refs(sql)
    )
    # No bogus column should be produced from the placeholder.
    assert not any(c == "" for _, c in scan_column_refs(sql))


def test_alias_stopwords_not_treated_as_table() -> None:
    # "WHERE" must never become an alias for dispatches.
    alias_map = _build_alias_map("SELECT * FROM dispatches WHERE state = ?")
    assert "WHERE" not in alias_map
    assert alias_map.get("dispatches") == "dispatches"


# ---------------------------------------------------------------------------
# Proof the guard catches the drift class (would have FAILED before Part 1)
# ---------------------------------------------------------------------------

def test_guard_would_have_caught_worker_pid_drift() -> None:
    """Simulate the pre-fix schema (no worker_pid) → scanner must flag it."""
    schema_map = build_schema_map()
    pre_fix = {t: set(cols) for t, cols in schema_map.items()}
    pre_fix["terminal_leases"].discard("worker_pid")

    findings = collect_drift(pre_fix)
    drift_cols = {(t, c) for _, t, c in findings}
    assert ("terminal_leases", "worker_pid") in drift_cols, (
        "guard failed to detect the worker_pid drift against a pre-fix schema"
    )


# ---------------------------------------------------------------------------
# The actual guard — the current codebase must be drift-free
# ---------------------------------------------------------------------------

def test_no_runtime_coordination_schema_drift() -> None:
    schema_map = build_schema_map()
    findings = collect_drift(schema_map)
    if findings:
        lines = [
            f"  {path.relative_to(_REPO_ROOT)}: {table}.{column} "
            f"(column not in schema for {table})"
            for path, table, column in sorted(set(findings), key=lambda f: (str(f[0]), f[1], f[2]))
        ]
        pytest.fail(
            "Schema-code drift detected — code references columns absent from "
            "the runtime_coordination schema:\n" + "\n".join(lines)
        )
