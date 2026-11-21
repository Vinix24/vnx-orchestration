"""Every insert into the six runtime tables that keep ``DEFAULT 'vnx-dev'`` names ``project_id``.

Source of the rule: agnostisch-standaard V7.3 (INV-A, 18-11). Six runtime tables still carry the
sentinel as a DDL default on a fresh store. No writer may reach it: a statement without
``project_id`` (or BOTH ``from_project_id`` and ``to_project_id`` on track_dependencies) would stamp the
operator's own tenant on another project's row. The scan reads the shipped source (never tests/),
resolves a column list built in a variable, and lists the three statements that legitimately
do not name the column, each with its reason.
"""
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCOPE = ("scripts", "vnx_cli", "bin", "hooks")
TABLES = ("dispatches", "tracks", "track_phase_history", "track_dependencies",
          "track_open_items", "track_pr_delivery")
INSERT = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(" + "|".join(TABLES) + r")\b", re.I)
LIST_VAR = re.compile(r"\(\{(?:', '|\", \")\.join\((\w+)\)\}\)|\(\{(\w+)\}\)")

# (path, normalised column head) -> reason. Anything else without project_id is a failure.
EXEMPT = {
    ("scripts/lib/runtime_state_machine.py", "(dispatch_id, state, terminal_id, track, priority, pr_ref, gate,"):
        "branch for a database that has no project_id column, so no tenant default exists there",
    ("scripts/migrate_future_system.py", "({col_list})"):
        "copies SELECT of the pre-migration table's own columns, project_id included",
}


def _head(text, start):
    window = text[start:start + 400]
    cut = re.search(r"\b(?:VALUES|SELECT)\b", window, re.I)
    return " ".join((window[:cut.start()] if cut else window).split()).strip(' "\'f')


def _assigned_expression(text, eq_end):
    """Text from just after ``=`` to the end of the statement: the first newline outside every
    bracket and string. Returns None when the statement never closes (unreadable)."""
    depth, quote, i = 0, None, eq_end
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "\n" and depth <= 0:
            return text[eq_end:i]
        i += 1
    return None if quote or depth > 0 else text[eq_end:]


def _columns_of(text, start, var, depth=0):
    """The expression(s) assigned to ``var`` (last assignment before the insert), followed through
    ``", ".join(other)``. None when no assignment is found or readable: never a pass."""
    assigns = list(re.finditer(rf"^[ \t]*{var}\b[^=\n]*=(?!=)", text[:start], re.M))
    if not assigns:
        return None
    expr = _assigned_expression(text, assigns[-1].end())
    if expr is None:
        return None
    joined = re.search(r"\.join\((\w+)\)", expr)
    if joined and depth < 2:
        inner = _columns_of(text, assigns[-1].start(), joined.group(1), depth + 1)
        return expr + "\n" + inner if inner is not None else expr
    return expr


def _names_tenant(table, columns):
    """track_dependencies has two tenant columns, both defaulting to the sentinel: it needs both.
    Every other table needs exactly ``project_id`` (a from_/to_ prefixed name is not it)."""
    if table.lower() == "track_dependencies":
        return all(re.search(rf"\b{c}\b", columns) for c in ("from_project_id", "to_project_id"))
    return bool(re.search(r"\bproject_id\b", columns))


def statements_without_project_id(root=REPO):
    files = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *SCOPE],
                           capture_output=True, check=True).stdout.split(b"\0")
    bad = []
    for raw in files:
        rel = raw.decode("utf-8", "surrogateescape")
        if not rel or rel.endswith(".json"):
            continue
        text = (Path(root) / rel).read_bytes().decode("utf-8", "replace")
        for m in INSERT.finditer(text):
            window = text[m.end():m.end() + 400]
            cut = re.search(r"\b(?:VALUES|SELECT)\b", window, re.I)
            head = window[:cut.start()] if cut else window
            if _names_tenant(m.group(1), head):
                continue
            var = LIST_VAR.search(head)
            name = var and (var.group(1) or var.group(2))
            columns = _columns_of(text, m.start(), name) if name else None
            if columns is not None and _names_tenant(m.group(1), columns):
                continue
            norm = _head(text, m.end())
            if any(rel == p and norm.startswith(h) for (p, h) in EXEMPT):
                continue
            line = text.count("\n", 0, m.start()) + 1
            bad.append(f"{rel}:{line} INSERT INTO {m.group(1)} {norm[:60]}")
    return bad


def test_every_runtime_insert_names_project_id():
    assert statements_without_project_id() == []


def _repo(tmp_path, text):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/w.py").write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    return repo


@pytest.mark.parametrize("table", TABLES)
def test_an_insert_without_project_id_is_caught_per_table(tmp_path, table):
    repo = _repo(tmp_path, f'c.execute("INSERT INTO {table} (a, b) VALUES (?, ?)")\n')
    assert len(statements_without_project_id(repo)) == 1


@pytest.mark.parametrize("verb", ["INSERT", "INSERT OR IGNORE", "insert or replace"])
def test_insert_verbs_are_all_caught(tmp_path, verb):
    repo = _repo(tmp_path, f'c.execute("{verb} INTO dispatches (a) VALUES (?)")\n')
    assert len(statements_without_project_id(repo)) == 1


def test_an_insert_naming_project_id_passes(tmp_path):
    repo = _repo(tmp_path, 'c.execute("INSERT INTO dispatches (dispatch_id, project_id) VALUES (?, ?)")\n')
    assert statements_without_project_id(repo) == []


def test_track_dependencies_accepts_both_endpoint_columns(tmp_path):
    repo = _repo(tmp_path, 'c.execute("INSERT INTO track_dependencies (from_track_id, from_project_id, to_track_id, to_project_id) VALUES (?, ?, ?, ?)")\n')
    assert statements_without_project_id(repo) == []


@pytest.mark.parametrize("cols", ["from_track_id, from_project_id", "to_track_id, to_project_id",
                                  "from_track_id, to_track_id", "from_track_id, project_id"])
def test_track_dependencies_needs_both_endpoint_columns(tmp_path, cols):
    """Both endpoint columns default to 'vnx-dev' (migration 0024): one of them is not enough."""
    repo = _repo(tmp_path, f'c.execute("INSERT INTO track_dependencies ({cols}) VALUES (?, ?)")\n')
    assert len(statements_without_project_id(repo)) == 1


@pytest.mark.parametrize("table", [t for t in TABLES if t != "track_dependencies"])
def test_a_prefixed_endpoint_column_does_not_stamp_a_single_tenant_table(tmp_path, table):
    repo = _repo(tmp_path, f'c.execute("INSERT INTO {table} (a, from_project_id) VALUES (?, ?)")\n')
    assert len(statements_without_project_id(repo)) == 1


def test_an_unrelated_project_id_between_the_list_and_the_insert_does_not_count(tmp_path):
    repo = _repo(tmp_path, 'cols = ["dispatch_id"]\nproject_id = pid\nc.execute(f"INSERT INTO dispatches ({\', \'.join(cols)}) VALUES (?)")\n')
    assert len(statements_without_project_id(repo)) == 1


def test_a_multiline_column_list_naming_project_id_passes(tmp_path):
    repo = _repo(tmp_path, 'cols = [\n    "dispatch_id",\n    "project_id",\n]\nc.execute(f"INSERT INTO dispatches ({\', \'.join(cols)}) VALUES (?)")\n')
    assert statements_without_project_id(repo) == []


def test_a_column_list_that_cannot_be_resolved_fails_naming_file_and_line(tmp_path):
    repo = _repo(tmp_path, 'cols = build_columns()\nc.execute(f"INSERT INTO dispatches ({\', \'.join(cols)}) VALUES (?)")\n')
    bad = statements_without_project_id(repo)
    assert len(bad) == 1 and bad[0].startswith("scripts/w.py:2 ")


def test_a_dependency_column_list_needs_both_endpoints(tmp_path):
    repo = _repo(tmp_path, 'cols = ["from_project_id", "x"]\nc.execute(f"INSERT INTO track_dependencies ({\', \'.join(cols)}) VALUES (?)")\n')
    assert len(statements_without_project_id(repo)) == 1
    (repo / "scripts/w.py").write_text('cols = ["from_project_id", "to_project_id"]\nc.execute(f"INSERT INTO track_dependencies ({\', \'.join(cols)}) VALUES (?)")\n')
    assert statements_without_project_id(repo) == []


def test_a_variable_column_list_without_project_id_is_caught(tmp_path):
    repo = _repo(tmp_path, 'cols = ["a", "b"]\nc.execute(f"INSERT INTO tracks ({\', \'.join(cols)}) VALUES (?)")\n')
    assert len(statements_without_project_id(repo)) == 1


def test_a_variable_column_list_with_project_id_passes(tmp_path):
    repo = _repo(tmp_path, 'cols = ["a", "project_id"]\nc.execute(f"INSERT INTO tracks ({\', \'.join(cols)}) VALUES (?)")\n')
    assert statements_without_project_id(repo) == []


def test_an_undefined_column_variable_stays_blocking(tmp_path):
    repo = _repo(tmp_path, 'c.execute(f"INSERT INTO tracks ({\', \'.join(cols)}) VALUES (?)")\n')
    assert len(statements_without_project_id(repo)) == 1


def test_other_tables_and_json_are_out_of_scope(tmp_path):
    repo = _repo(tmp_path, 'c.execute("INSERT INTO pattern_usage (a) VALUES (?)")\n')
    (repo / "scripts/r.json").write_text('"INSERT INTO dispatches (a) VALUES (1)"', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert statements_without_project_id(repo) == []


def test_scan_outside_a_git_repo_fails_loudly(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        statements_without_project_id(tmp_path)


def test_a_joined_column_list_resolves_through_its_source(tmp_path):
    good = _repo(tmp_path, 'cols = ["project_id"]\ncol_list = ", ".join(cols)\nc.execute(f"INSERT INTO dispatches ({col_list}) VALUES (?)")\n')
    assert statements_without_project_id(good) == []
    (good / "scripts/w.py").write_text('cols = ["a"]\ncol_list = ", ".join(cols)\nc.execute(f"INSERT INTO dispatches ({col_list}) VALUES (?)")\n')
    assert len(statements_without_project_id(good)) == 1
