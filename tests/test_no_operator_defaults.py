"""Guard: no new operator name (kimi-k3, Vinix24, claude-salesminds, vnx-dev, vincentvandeth, salesminds) in shipped code.

Every finding in scope has a row in tests/fixtures/operator_names/findings.tsv with the
same (path, name, fingerprint) and count. The allowlist is generated from that table only.
``vnx-dev`` counts quoted and bare (AG-G3), which includes ``DEFAULT 'vnx-dev'``; the scope covers
``dashboard/``, ``configs/``, the two installers and ``CODEOWNERS``; ``migration_linter.py``
runs on this matcher and this table.
"""
import copy
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))
import operator_names as on

ALLOWED = 'MODEL = "kimi-k3"  # catalogue entry\n'


def _tree_findings(root=on.REPO):
    return on.scan(root)


def test_allowlist_equals_generator_output_on_the_table():
    expected = on.render(on.generate(on.load_table()))
    assert on.ALLOWLIST.read_text(encoding="utf-8") == expected


def test_every_finding_has_a_table_row_and_every_row_a_finding():
    import json
    allowlist = json.loads(on.ALLOWLIST.read_text(encoding="utf-8"))
    problems = on.compare(_tree_findings(), allowlist)
    assert problems == [], "\n".join(problems)


def test_table_rules_hold():
    rows = on.load_table()
    assert len(rows) == 403 and sum(r["count"] for r in rows) == 434
    assert on.validate_table(rows) == []


def _row(**kw):
    row = dict(fingerprint="0" * 16, path="a.py", name="kimi-k3", count=1,
               category="model_catalog", reason="catalogue", group="")
    row.update(kw)
    return row


@pytest.mark.parametrize("override, needle", [
    (dict(category="bogus"), "unknown category"),
    (dict(category=""), "unknown category"),
    (dict(category=None), "unknown category"),
    (dict(reason=""), "empty reason"),
    (dict(reason="   "), "empty reason"),
    (dict(category="te_repareren", group=""), "needs a group"),
    (dict(category="te_repareren", group="AG-x"), "needs a group"),
    (dict(name="bogus"), "unknown name"),
    (dict(name="kimi"), "unknown name"),
])
def test_validate_table_refuses_a_bad_row(override, needle):
    problems = on.validate_table([_row(**override)])
    assert any(needle in p for p in problems), problems


def test_validate_table_accepts_a_good_te_repareren_row():
    assert on.validate_table([_row(category="te_repareren", group="AG-2")]) == []


def test_te_repareren_row_is_never_also_under_another_category():
    rows = [_row(category="te_repareren", group="AG-2"), _row(category="model_catalog")]
    assert any("listed twice" in p for p in on.validate_table(rows))


@pytest.mark.parametrize("text, needle", [
    ("", "header"),
    ("path\tname\n", "header"),
    ("\t".join(on.COLUMNS) + "\nx\ty\n", "columns"),
    ("\t".join(on.COLUMNS) + "\n" + "\t".join(["f", "p", "kimi-k3", "0", "model_catalog", "r", ""]) + "\n", "count"),
    ("\t".join(on.COLUMNS) + "\n" + "\t".join(["f", "p", "kimi-k3", "None", "model_catalog", "r", ""]) + "\n", "count"),
])
def test_load_table_refuses_a_malformed_table(tmp_path, text, needle):
    table = tmp_path / "t.tsv"
    table.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=needle):
        on.load_table(table)


def test_load_table_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        on.load_table(tmp_path / "absent.tsv")


def test_generator_reads_only_the_table_rows():
    rows = on.load_table()
    changed = copy.deepcopy(rows)
    for r in changed:
        r["reason"], r["category"], r["group"] = "x", "model_catalog", ""
    assert on.generate(changed) == on.generate(rows)


# --- the guard bites, on a temporary git repo ---------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def _repo(tmp_path, files):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    return repo


def _verdict(repo, files_allowed):
    """Allowlist built from the findings of the pristine tree, as a table row per finding."""
    rows = [_row(fingerprint=f, path=p, name=n, count=c) for (p, n, f), (c, _) in files_allowed.items()]
    return on.generate(rows)


def _baseline(tmp_path):
    repo = _repo(tmp_path, {"scripts/a.py": ALLOWED, "scripts/b.py": "x = 1\n"})
    return repo, _verdict(repo, on.scan(repo))


def test_clean_tree_is_green(tmp_path):
    repo, allow = _baseline(tmp_path)
    assert on.compare(on.scan(repo), allow) == []


def test_new_line_in_a_file_that_already_has_an_allowed_one_is_red(tmp_path):
    repo, allow = _baseline(tmp_path)
    (repo / "scripts/a.py").write_text(ALLOWED + 'DEFAULT = "kimi-k3"\n', encoding="utf-8")
    problems = on.compare(on.scan(repo), allow)
    assert len(problems) == 1 and problems[0].startswith("NEW scripts/a.py kimi-k3 ")
    assert 'DEFAULT = "kimi-k3"' in problems[0]


def test_second_copy_of_an_allowed_line_is_red_on_the_count(tmp_path):
    repo, allow = _baseline(tmp_path)
    (repo / "scripts/a.py").write_text(ALLOWED * 2, encoding="utf-8")
    problems = on.compare(on.scan(repo), allow)
    assert len(problems) == 1 and "COUNT scripts/a.py kimi-k3" in problems[0] and "found 2" in problems[0]


def test_moved_line_with_changed_whitespace_stays_green(tmp_path):
    repo, allow = _baseline(tmp_path)
    (repo / "scripts/a.py").write_text("\n\n" + 'MODEL   =  "kimi-k3"  # catalogue    entry\n', encoding="utf-8")
    assert on.compare(on.scan(repo), allow) == []


def test_editing_an_allowed_line_is_red_both_ways(tmp_path):
    repo, allow = _baseline(tmp_path)
    (repo / "scripts/a.py").write_text('MODEL = "kimi-k3"  # changed\n', encoding="utf-8")
    problems = on.compare(on.scan(repo), allow)
    assert sorted(p.split()[0] for p in problems) == ["GONE", "NEW"]


def test_deleting_a_fixed_finding_without_its_row_is_red(tmp_path):
    repo, allow = _baseline(tmp_path)
    (repo / "scripts/a.py").write_text("MODEL = None\n", encoding="utf-8")
    problems = on.compare(on.scan(repo), allow)
    assert len(problems) == 1 and problems[0].startswith("GONE scripts/a.py kimi-k3 ")


def test_new_file_and_other_names_are_red(tmp_path):
    repo, allow = _baseline(tmp_path)
    (repo / "scripts/c.py").write_text("# Vinix24/x\nP = '~/.claude-salesminds'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    names = sorted(p.split()[2] for p in on.compare(on.scan(repo), allow))
    assert names == ["Vinix24", "claude-salesminds"]


def test_untracked_files_tests_and_markdown_are_out_of_scope(tmp_path):
    repo, allow = _baseline(tmp_path)
    (repo / "scripts/untracked.py").write_text('"kimi-k3"\n', encoding="utf-8")
    (repo / "scripts/notes.md").write_text("kimi-k3\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_x.py").write_text("kimi-k3\n", encoding="utf-8")
    _git(repo, "add", "scripts/notes.md", "tests/test_x.py")
    assert on.compare(on.scan(repo), allow) == []


def test_scan_outside_a_git_repo_fails_loudly(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        on.scan(tmp_path)


# --- the fourth name: quoted vnx-dev, including DEFAULT 'vnx-dev' ------------------------------

DEV = "vnx-" + "dev"
DEFAULT_LINE = f"project_id TEXT NOT NULL DEFAULT '{DEV}',\n"


@pytest.mark.parametrize("line, hits", [
    (f"x = '{DEV}'", 1), (f'x = "{DEV}"', 1), (f"DEFAULT '{DEV}'", 1),
    (f"x = {DEV}", 1), (f"/{DEV}/state", 1), (f"# the {DEV} store", 1),
    ("x = 'vnx'", 0), ("dev-vnx", 0),
])
def test_matcher_counts_the_quoted_and_the_bare_form(tmp_path, line, hits):
    repo = _repo(tmp_path, {"scripts/a.py": line + "\n"})
    assert sum(c for (_, n, _), (c, _) in on.scan(repo).items() if n == DEV) == hits


def test_new_default_clause_under_vnx_cli_is_red(tmp_path):
    repo, allow = _baseline(tmp_path)
    (repo / "vnx_cli").mkdir()
    (repo / "vnx_cli/schema.py").write_text(DEFAULT_LINE, encoding="utf-8")
    _git(repo, "add", "-A")
    problems = on.compare(on.scan(repo), allow)
    assert len(problems) == 1 and problems[0].startswith(f"NEW vnx_cli/schema.py {DEV} ")


def test_new_default_clause_in_an_already_listed_file_is_red(tmp_path):
    repo = _repo(tmp_path, {"scripts/a.py": DEFAULT_LINE})
    allow = _verdict(repo, on.scan(repo))
    (repo / "scripts/a.py").write_text(DEFAULT_LINE + f"x TEXT DEFAULT \"{DEV}\"\n", encoding="utf-8")
    problems = on.compare(on.scan(repo), allow)
    assert len(problems) == 1 and problems[0].startswith(f"NEW scripts/a.py {DEV} ")


def test_editing_an_allowed_quoted_line_is_red_both_ways(tmp_path):
    repo = _repo(tmp_path, {"scripts/a.py": DEFAULT_LINE})
    allow = _verdict(repo, on.scan(repo))
    (repo / "scripts/a.py").write_text(DEFAULT_LINE.replace("TEXT", "VARCHAR"), encoding="utf-8")
    assert sorted(p.split()[0] for p in on.compare(on.scan(repo), allow)) == ["GONE", "NEW"]


def test_table_rows_for_the_fourth_name_are_complete_and_valid():
    rows = [r for r in on.load_table() if r["name"] == DEV]
    assert len(rows) == 321 and sum(r["count"] for r in rows) == 349
    migrations = [r for r in rows if r["path"].startswith("schemas/migrations/")]
    assert len(migrations) == 65 and sum(r["count"] for r in migrations) == 75
    assert all(r["category"] != "te_repareren" or r["group"] for r in rows)


# --- AG-G3: bare vnx-dev, the wider scope, two more names ---------------------------------------

VINCENT = "vincent" + "vandeth"
SALES = "sales" + "minds"


def _new_file_verdict(tmp_path, rel, text):
    repo, allow = _baseline(tmp_path)
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    return on.compare(on.scan(repo), allow)


@pytest.mark.parametrize("rel", ["scripts/hooks/guard.sh", "scripts/lib/project_id_migration.py",
                                 "scripts/quality_db_init.py"])
def test_new_file_with_a_bare_vnx_dev_is_red(tmp_path, rel):
    problems = _new_file_verdict(tmp_path, rel, f'[ "$P" = {DEV} ] && exit 1\n')
    assert len(problems) == 1 and problems[0].startswith(f"NEW {rel} {DEV} ")


def test_ag2a1_packaged_worker_default_remains_in_scope(tmp_path):
    rel = "scripts/lib/defaults/vnx_workers.default.yaml"
    problems = _new_file_verdict(tmp_path, rel, f"project: {DEV}\n")
    assert len(problems) == 1 and problems[0].startswith(f"NEW {rel} {DEV} ")


@pytest.mark.parametrize("rel", ["dashboard/app.js", "dashboard/api/x.py", "configs/panel.yaml"])
def test_new_file_with_a_quoted_vnx_dev_under_dashboard_or_configs_is_red(tmp_path, rel):
    problems = _new_file_verdict(tmp_path, rel, f'project: "{DEV}"\n')
    assert len(problems) == 1 and problems[0].startswith(f"NEW {rel} {DEV} ")


@pytest.mark.parametrize("rel", ["install-central.sh", "install-remote.sh", "CODEOWNERS"])
def test_installers_and_codeowners_are_in_scope(tmp_path, rel):
    problems = _new_file_verdict(tmp_path, rel, f"owner {VINCENT}\n")
    assert len(problems) == 1 and problems[0].startswith(f"NEW {rel} {VINCENT} ")


@pytest.mark.parametrize("name", [VINCENT, SALES])
def test_new_file_with_each_extra_name_is_red(tmp_path, name):
    problems = _new_file_verdict(tmp_path, "scripts/n.py", f"# owner: {name}\n")
    assert len(problems) == 1 and problems[0].startswith(f"NEW scripts/n.py {name} ")


def test_claude_salesminds_is_one_finding_not_two(tmp_path):
    repo = _repo(tmp_path, {"scripts/a.py": "P = '~/.claude-" + SALES + "'\n"})
    assert sorted(n for (_, n, _) in on.scan(repo)) == ["claude-" + SALES]


def test_extra_names_are_case_sensitive_and_markdown_stays_out(tmp_path):
    repo = _repo(tmp_path, {"dashboard/a.js": f"// {VINCENT.upper()} {SALES.title()}\n", "configs/n.md": VINCENT + "\n"})
    assert on.scan(repo) == {}


def test_files_outside_the_scope_stay_out(tmp_path):
    repo = _repo(tmp_path, {"docs/a.txt": f"{DEV} {VINCENT}\n", "other/b.py": f"'{DEV}'\n"})
    assert on.scan(repo) == {}


def test_an_unreadable_scope_file_fails_loudly(tmp_path):
    repo = _repo(tmp_path, {"dashboard/a.js": "x\n"})
    (repo / "dashboard/a.js").unlink()
    with pytest.raises(FileNotFoundError):
        on.scan(repo)
