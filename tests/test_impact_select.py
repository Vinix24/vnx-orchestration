"""Tests for scripts/ci/impact_select.py: the static test-to-module graph and its fail-expand rules."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_CI_DIR = Path(__file__).resolve().parents[1] / "scripts" / "ci"
if str(_CI_DIR) not in sys.path:
    sys.path.insert(0, str(_CI_DIR))

import impact_select as sel  # isort: skip

REAL_ROOT = Path(__file__).resolve().parents[1]


def make_tree(tmp_path: Path, files: dict[str, str], exclusions: str = "") -> Path:
    files = {"scripts/ci/test_exclusions.txt": exclusions, **files}
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def run(root: Path, *paths: str) -> dict:
    return sel.select(root, list(paths))


BASE = {
    "scripts/lib/core.py": "X = 1\n",
    "scripts/lib/mid.py": "import core\n",
    "tests/helper_mod.py": "from scripts.lib import mid\n",
    "tests/test_direct.py": "import core\n",
    "tests/test_transitive.py": "import helper_mod\n",
    "tests/test_unrelated.py": "import os\n",
}


def test_changed_module_selects_direct_and_transitive_tests(tmp_path):
    out = run(make_tree(tmp_path, BASE), "scripts/lib/core.py")
    assert out["scope"] == "impact" and out["expand_reason"] is None
    assert out["selected"] == ["tests/test_direct.py", "tests/test_transitive.py"]
    assert out["universe_count"] == 3


@pytest.mark.parametrize(
    "literal",
    ['"scripts/x.py"', 'ROOT / "scripts" / "x.py"', 'os.path.join(ROOT, "scripts", "x.py")', '["python3", "scripts/x.py", "--flag"]'],
)
def test_subprocess_path_literal_selects_on_script_and_its_imports(tmp_path, literal):
    files = {
        "scripts/x.py": "import dep\n",
        "scripts/lib/dep.py": "Y = 2\n",
        "tests/test_runs_x.py": f"import os\nROOT = None\nCMD = {literal}\n",
        "tests/test_other.py": "import os\n",
    }
    root = make_tree(tmp_path, files)
    assert run(root, "scripts/x.py")["selected"] == ["tests/test_runs_x.py"]
    assert run(root, "scripts/lib/dep.py")["selected"] == ["tests/test_runs_x.py"]


def test_shell_script_reference_reaches_the_python_it_runs(tmp_path):
    files = {
        "scripts/run.sh": "python3 scripts/lib/tool.py\n",
        "scripts/lib/tool.py": "Z = 3\n",
        "tests/test_sh.py": 'CMD = "scripts/run.sh"\n',
    }
    assert run(make_tree(tmp_path, files), "scripts/lib/tool.py")["selected"] == ["tests/test_sh.py"]


def test_bare_name_in_two_roots_selects_both(tmp_path):
    files = {
        "scripts/dup.py": "A = 1\n",
        "scripts/lib/dup.py": "A = 2\n",
        "tests/test_a.py": "import dup\n",
        "tests/test_b.py": "import os\n",
    }
    root = make_tree(tmp_path, files)
    assert run(root, "scripts/dup.py")["selected"] == ["tests/test_a.py"]
    assert run(root, "scripts/lib/dup.py")["selected"] == ["tests/test_a.py"]


def test_importlib_literal_and_relative_import_are_followed(tmp_path):
    files = {
        "scripts/lib/pkg/__init__.py": "",
        "scripts/lib/pkg/a.py": "from . import b\n",
        "scripts/lib/pkg/b.py": "B = 1\n",
        "scripts/lib/dyn.py": "D = 1\n",
        "tests/test_rel.py": "import pkg.a\n",
        "tests/test_dyn.py": 'import importlib\nimportlib.import_module("dyn")\n',
    }
    root = make_tree(tmp_path, files)
    assert run(root, "scripts/lib/pkg/b.py")["selected"] == ["tests/test_rel.py"]
    assert run(root, "scripts/lib/dyn.py")["selected"] == ["tests/test_dyn.py"]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("README_unknown/app.js", "path_outside_scope"),
        ("setup.cfg", "path_outside_scope"),
        ("tests/conftest.py", "conftest_changed"),
        ("tests/sub/conftest.py", "conftest_changed"),
        ("tests/fixtures/data.json", "test_infrastructure"),
        ("tests/fixtures/helper.py", "test_infrastructure"),
        ("tests/data/sample.txt", "test_infrastructure"),
        ("pyproject.toml", "test_infrastructure"),
        ("pytest.ini", "test_infrastructure"),
        ("scripts/ci/profile_a_constraints.txt", "test_infrastructure"),
        ("scripts/ci/test_exclusions.txt", "test_infrastructure"),
        ("scripts/ci/xdist_expected_skips.txt", "test_infrastructure"),
        (".github/workflows/vnx-ci.yml", "test_infrastructure"),
        ("scripts/lib/settings.json", "unknown_to_graph"),
        ("scripts/lib/gone.py", "unknown_to_graph"),
        ("tests/test_gone.py", "unknown_to_graph"),
    ],
)
def test_each_fail_expand_reason_gives_the_full_suite(tmp_path, path, reason):
    out = run(make_tree(tmp_path, BASE), path)
    assert out["scope"] == "full" and out["expand_reason"] == reason
    assert out["selected"] == sorted(p for p in BASE if p.startswith("tests/test_"))


def test_parse_error_expands_and_names_the_file(tmp_path):
    out = run(make_tree(tmp_path, {**BASE, "scripts/lib/broken.py": "def (:\n"}), "scripts/lib/core.py")
    assert out["scope"] == "full" and out["expand_reason"] == "parse_error"
    assert out["parse_errors"] == ["scripts/lib/broken.py"]


@pytest.mark.parametrize("bad", [None, "scripts/lib/core.py", ["scripts/lib/core.py\textra"], ['"scripts/caf\\303\\251.py"'], ["../x.py"], [5]])
def test_malformed_input_expands(tmp_path, bad):
    out = sel.select(make_tree(tmp_path, BASE), bad)
    assert out["scope"] == "full" and out["expand_reason"] == "malformed_input"


def test_docs_only_change_selects_nothing_and_does_not_expand(tmp_path):
    out = run(make_tree(tmp_path, BASE), "docs/guide.md", "README.md", "claudedocs/x/plan.md")
    assert (out["scope"], out["expand_reason"], out["selected"]) == ("impact", None, [])


def test_changed_test_file_selects_itself_and_excluded_is_reported(tmp_path):
    root = make_tree(tmp_path, BASE, exclusions="tests/test_direct.py  # red on main\n")
    out = run(root, "tests/test_direct.py")
    assert out["selected"] == ["tests/test_direct.py"]
    assert out["selected_excluded"] == ["tests/test_direct.py"]
    assert out["excluded_count"] == 1


def test_changed_module_no_test_reaches_is_unreached_without_expanding(tmp_path):
    out = run(make_tree(tmp_path, {**BASE, "scripts/lib/orphan.py": "O = 1\n"}), "scripts/lib/orphan.py")
    assert out["scope"] == "impact" and out["selected"] == []
    assert out["unreached"] == ["scripts/lib/orphan.py"]


def test_graph_version_is_stable_and_one_extra_edge_changes_it(tmp_path):
    first = run(make_tree(tmp_path / "a", BASE), "scripts/lib/core.py")
    again = run(make_tree(tmp_path / "b", BASE), "scripts/lib/core.py")
    extra = run(make_tree(tmp_path / "c", {**BASE, "tests/test_unrelated.py": "import core\n"}), "scripts/lib/core.py")
    assert first["graph_version"] == again["graph_version"]
    assert extra["graph_version"] != first["graph_version"]
    assert first["selector_version"] == sel.selector_version() and len(first["selector_version"]) == 64


def test_python_files_pattern_of_pytest_ini_defines_the_universe(tmp_path):
    files = {**BASE, "pytest.ini": "[pytest]\npython_files = check_*.py\n", "tests/check_core.py": "import core\n"}
    out = run(make_tree(tmp_path, files), "scripts/lib/core.py")
    assert out["universe_count"] == 1 and out["selected"] == ["tests/check_core.py"]


def _cli(root: Path, stdin: str | None = None, *extra: str):
    return subprocess.run(
        [sys.executable, str(_CI_DIR / "impact_select.py"), "--root", str(root), *extra],
        input=stdin, capture_output=True, text=True, check=False,
    )


def test_cli_prints_sorted_json_and_exits_zero_also_for_full(tmp_path):
    root = make_tree(tmp_path, BASE)
    impact = _cli(root, "scripts/lib/core.py\n")
    full = _cli(root, "pyproject.toml\n")
    assert impact.returncode == 0 and full.returncode == 0
    for proc, scope in ((impact, "impact"), (full, "full")):
        obj = json.loads(proc.stdout)
        assert list(obj) == sorted(obj) and obj["scope"] == scope
    paths_file = tmp_path / "paths.txt"
    paths_file.write_text("scripts/lib/core.py\n", encoding="utf-8")
    assert json.loads(_cli(root, None, "--paths-file", str(paths_file)).stdout)["selected"] == json.loads(impact.stdout)["selected"]


def test_cli_crash_prints_nothing_and_exits_nonzero(tmp_path):
    proc = _cli(tmp_path, "scripts/lib/core.py\n")  # no exclusions file: cannot decide
    assert proc.returncode != 0 and proc.stdout == ""


def test_real_repo_dispatch_serialization_selects_its_tests():
    out = sel.select(REAL_ROOT, ["scripts/lib/dispatch_serialization.py"])
    assert out["scope"] == "impact" and out["selected"]
    on_disk = {f"tests/{p.name}" for p in (REAL_ROOT / "tests").glob("test_dispatch_serialization*.py")}
    assert on_disk and on_disk <= set(out["selected"])
