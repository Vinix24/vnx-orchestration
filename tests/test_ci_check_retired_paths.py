#!/usr/bin/env python3
"""Tests for scripts/ci/check_retired_paths.py (golf 1, C1).

The check decides what counts as evidence that a removed path, symbol or env
var stays removed, so these tests pin both sides of every rule on a fake git
tree: each executable reference form fails the check, and each non-executable
mention (comment, docstring, markdown, frozen history) does not.  One tree per
kind of list line: path, ``path::symbol`` and ``env:NAME``.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent

sys.path.insert(0, str(VNX_ROOT / "scripts" / "ci"))

docs_check = importlib.import_module("check_docs_file_line_refs")
check = importlib.import_module("check_retired_paths")


def make_tree(tmp_path: Path, files: dict[str, str], symlinks: dict[str, str] | None = None) -> Path:
    """A git repo with *files* in the index (no commit needed for ls-files)."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for rel, target in (symlinks or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def violations(root: Path, list_text: str) -> list[check.Violation]:
    return check.check(root, check.parse_list(list_text))


def wheres(found: list[check.Violation]) -> set[str]:
    return {v.where for v in found}


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def test_parse_list_reads_three_kinds_and_skips_comments() -> None:
    entries = check.parse_list(
        "# header\n"
        "\n"
        "scripts/foo.py   # gone in C1\n"
        "scripts/lib/mod.py::Klass.method\n"
        "env:VNX_OLD_FLAG\n"
    )
    assert [(e.kind, e.path, e.symbol, e.name) for e in entries] == [
        ("path", "scripts/foo.py", "", ""),
        ("symbol", "scripts/lib/mod.py", "Klass.method", ""),
        ("env", "", "", "VNX_OLD_FLAG"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "env:1BAD",
        "env:",
        "/abs/scripts/foo.py",
        "scripts/../foo.py",
        "scripts//foo.py",
        "scripts/foo.sh::symbol",
        "scripts/mod.py::bad-name",
        "scripts/mod.py::",
    ],
)
def test_parse_list_refuses_malformed_lines(line: str) -> None:
    with pytest.raises(check.ListError):
        check.parse_list(line + "\n")


def test_parse_list_refuses_duplicates() -> None:
    with pytest.raises(check.ListError, match="duplicate"):
        check.parse_list("scripts/foo.py\nscripts/foo.py  # again\n")


def test_shipped_list_parses() -> None:
    entries = check.parse_list((VNX_ROOT / check.LIST_REL).read_text(encoding="utf-8"))
    assert entries, "the shipped list is empty"


def test_frozen_doc_dirs_are_imported_not_copied() -> None:
    assert check._EXEMPT_DOC_SUBDIRS is docs_check._EXEMPT_DOC_SUBDIRS
    for sub in docs_check._EXEMPT_DOC_SUBDIRS:
        assert check.is_exempt(f"docs/{sub}/run.sh")


# ---------------------------------------------------------------------------
# Kind 1: a path
# ---------------------------------------------------------------------------

PATH_LIST = "scripts/foo.py\n"


def test_path_still_present_fails(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"scripts/foo.py": "print('x')\n"})
    found = violations(root, PATH_LIST)
    assert [(v.where, v.detail) for v in found] == [("scripts/foo.py", "still exists")]


def test_path_gone_and_unreferenced_is_clean(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"scripts/bar.py": "print('x')\n"})
    assert violations(root, PATH_LIST) == []


@pytest.mark.parametrize(
    ("rel", "content", "line"),
    [
        ("scripts/a.py", "import foo\n", 1),
        ("scripts/a.py", "x = 1\nfrom foo import main\n", 2),
        ("scripts/a.py", "from scripts.foo import main\n", 1),
        ("scripts/a.py", "from scripts import foo\n", 1),
        ("scripts/a.py", "import importlib\nimportlib.import_module('scripts.foo')\n", 2),
        ("scripts/a.py", "import runpy\nrunpy.run_module('foo')\n", 2),
        ("scripts/a.py", "import subprocess\nsubprocess.run(['python3', '-m', 'scripts.foo'])\n", 2),
        ("scripts/a.py", "CMD = 'python3 -m scripts.foo --x'\n", 1),
        ("scripts/a.py", "import subprocess\nsubprocess.run(['python3', 'scripts/foo.py'])\n", 2),
        ("scripts/a.py", "from pathlib import Path\nP = Path(__file__).parent / 'foo.py'\n", 2),
        ("scripts/run.sh", "#!/bin/bash\nbash \"$VNX_HOME/scripts/foo.py\" --go\n", 2),
        ("scripts/run.sh", "python3 \"${ROOT}/scripts/foo.py\"\n", 1),
        ("scripts/run.sh", "echo '# not a comment' && python3 scripts/foo.py\n", 1),
        ("scripts/run.sh", "python3 -c 'import foo'\n", 1),
        (".github/workflows/ci.yml", "jobs:\n  run: python3 scripts/foo.py  # step\n", 2),
        ("templates/settings.json.j2", '{"command": "python3 $VNX_HOME/scripts/foo.py"}\n', 1),
        ("launchd/com.vnx.x.plist", "<plist>\n<string>/opt/vnx/scripts/foo.py</string>\n</plist>\n", 2),
        ("bin/tool", "#!/usr/bin/env bash\nexec \"$HOME/scripts/foo.py\"\n", 2),
        ("scripts/ci/test_exclusions.txt", "# header\nscripts/foo.py  # red on main\n", 2),
        ("dashboard/run.ts", "spawn('python3', ['scripts/foo.py']);\n", 1),
        ("pkg/install.toml", "cmd = \"scripts/foo.py\"\n", 1),
    ],
)
def test_path_executable_reference_fails(tmp_path: Path, rel: str, content: str, line: int) -> None:
    root = make_tree(tmp_path, {rel: content})
    assert wheres(violations(root, PATH_LIST)) == {f"{rel}:{line}"}


def test_path_symlink_to_retired_file_fails(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"scripts/bar.py": "x = 1\n"}, symlinks={"vendor/foo.py": "../scripts/foo.py"})
    found = violations(root, PATH_LIST)
    assert [(v.where, v.detail) for v in found] == [("vendor/foo.py", "symlink to ../scripts/foo.py")]


LINKED_LIST = "scripts/runner_v8.sh\n"


def test_path_relative_symlink_in_subdirectory_resolves_against_its_directory(tmp_path: Path) -> None:
    # The codex_gate reproduction (scripts/compat/dispatcher.sh -> ../dispatcher_v8_minimal.sh)
    # with a fake name, because the real one is on the shipped list: the link text alone has no
    # scripts/ component; only resolved against scripts/compat/ does it name the retired file.
    root = make_tree(
        tmp_path,
        {"scripts/runner.sh": "echo ok\n"},
        symlinks={"scripts/compat/runner.sh": "../runner_v8.sh"},
    )
    found = violations(root, LINKED_LIST)
    assert [(v.where, v.detail) for v in found] == [("scripts/compat/runner.sh", "symlink to ../runner_v8.sh")]


@pytest.mark.parametrize("spelling", ["as_given", "real_path"])
def test_path_absolute_symlink_into_the_repo_fails(tmp_path: Path, spelling: str) -> None:
    # The repo sits behind a symlinked directory, so the root as given and its real path differ;
    # an absolute link written in either spelling points into the repo.
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    base = alias if spelling == "as_given" else Path(os.path.realpath(real))
    link = str(base / "repo" / "scripts" / "foo.py")
    root = make_tree(alias, {"scripts/bar.py": "x = 1\n"}, symlinks={"vendor/deep/foo.py": link})
    found = violations(root, PATH_LIST)
    assert [(v.where, v.detail) for v in found] == [("vendor/deep/foo.py", f"symlink to {link}")]


def test_path_symlink_into_a_retired_directory_fails(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"scripts/a.py": "x = 1\n"}, symlinks={"bin/tool.sh": "../scripts/old/tool.sh"})
    assert wheres(violations(root, "scripts/old\n")) == {"bin/tool.sh"}


def test_path_symlink_outside_the_repo_is_read_by_its_tail(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path,
        {"scripts/a.py": "x = 1\n"},
        symlinks={"vendor/foo.py": "/opt/vnx/scripts/foo.py", "vendor/other.py": "/opt/other/lib/foo.py"},
    )
    assert wheres(violations(root, PATH_LIST)) == {"vendor/foo.py"}


@pytest.mark.parametrize(
    "link",
    [
        "../runner.sh",
        "../lib/scripts/runner_v8.sh",
        "./runner_v8.sh",
        "..",
    ],
)
def test_path_symlink_resolving_elsewhere_is_clean(tmp_path: Path, link: str) -> None:
    # The text of the second and third link ends in the retired name, but resolved against
    # scripts/compat/ none of them is scripts/runner_v8.sh.
    root = make_tree(tmp_path, {"scripts/runner.sh": "echo ok\n"}, symlinks={"scripts/compat/x.sh": link})
    assert violations(root, LINKED_LIST) == []


@pytest.mark.parametrize(
    ("rel", "content"),
    [
        ("scripts/a.py", "# runs scripts/foo.py and import foo\nx = 1\n"),
        ("scripts/a.py", '"""Replaces scripts/foo.py (python3 -m scripts.foo)."""\n'),
        ("scripts/a.py", 'def f():\n    """Was scripts/foo.py."""\n    return 1\n'),
        ("scripts/a.py", "from bar import foo\nfoo()\n"),
        ("scripts/a.py", "from scripts.bar import foo\n"),
        ("scripts/a.py", "NAME = 'foo.pyc'\nOTHER = 'old_foo.py'\n"),
        ("scripts/run.sh", "# calls scripts/foo.py\necho done\n"),
        (".github/workflows/ci.yml", "# python3 scripts/foo.py\nrun: echo ok\n"),
        ("launchd/x.plist", "<plist><!-- scripts/foo.py\n retired --></plist>\n"),
        ("dashboard/run.ts", "// scripts/foo.py\n/* foo.py */ const a = 1;\n"),
        ("docs/guide.md", "Run `python3 scripts/foo.py` (retired).\n"),
        ("docs/investigations/2026-run.sh", "python3 scripts/foo.py\n"),
        ("docs/_archive/old.sh", "python3 scripts/foo.py\n"),
        ("docs/manifesto/x.yaml", "run: scripts/foo.py\n"),
        ("tests/fixtures/settings.json", '{"command": "scripts/foo.py"}\n'),
        ("packages/p/tests/fixtures/run.sh", "python3 scripts/foo.py\n"),
        ("CHANGELOG.md", "- removed scripts/foo.py\n"),
        ("changelog.d/c1.md", "- removed scripts/foo.py\n"),
        ("scripts/ci/retired_paths.txt", "scripts/foo.py\n"),
        ("docs/data.csv", "scripts/foo.py,gone\n"),
    ],
)
def test_path_non_executable_mention_is_clean(tmp_path: Path, rel: str, content: str) -> None:
    root = make_tree(tmp_path, {rel: content})
    assert violations(root, PATH_LIST) == []


def test_path_with_same_name_sibling_counts_only_attributable_references(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path,
        {
            "scripts/lib/foo.py": "x = 1\n",
            "scripts/a.py": "A = 'scripts/foo.py'\n",
            "scripts/b.py": "B = 'lib/foo.py'\n",
            "scripts/c.py": "C = 'foo.py'\n",
            "scripts/d.py": "import foo\n",
            "scripts/e.py": "from scripts import foo\n",
        },
    )
    assert wheres(violations(root, PATH_LIST)) == {"scripts/a.py:1", "scripts/e.py:1"}


def test_extensionless_path_needs_a_directory_component(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path,
        {"scripts/a.sh": "tool --help\n", "scripts/b.sh": "\"$ROOT/bin/tool\" --help\n"},
    )
    assert wheres(violations(root, "bin/tool\n")) == {"scripts/b.sh:1"}


def test_non_ascii_file_name_is_scanned(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"scripts/überprüfung.sh": "python3 scripts/foo.py\n"})
    assert wheres(violations(root, PATH_LIST)) == {"scripts/überprüfung.sh:1"}


def test_retired_file_present_is_not_scanned_for_its_own_references(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"scripts/foo.py": "PROG = 'foo.py'\n"})
    assert [v.detail for v in violations(root, PATH_LIST)] == ["still exists"]


# ---------------------------------------------------------------------------
# Kind 2: path::symbol
# ---------------------------------------------------------------------------

SYMBOL_LIST = "scripts/lib/mod.py::gone\n"
MOD_WITHOUT = "def kept():\n    return 1\n"


@pytest.mark.parametrize(
    ("module", "line"),
    [
        ("def gone():\n    return 1\n", 1),
        ("x = 1\n\nclass gone:\n    pass\n", 3),
        ("try:\n    import fast\nexcept ImportError:\n    def gone():\n        return 0\n", 4),
        ("from elsewhere import gone\n", 1),
    ],
)
def test_symbol_still_defined_fails(tmp_path: Path, module: str, line: int) -> None:
    root = make_tree(tmp_path, {"scripts/lib/mod.py": module})
    found = violations(root, SYMBOL_LIST)
    assert [(v.where, v.detail) for v in found] == [(f"scripts/lib/mod.py:{line}", "still defines gone")]


def test_dotted_symbol_checks_the_class_body(tmp_path: Path) -> None:
    present = make_tree(tmp_path / "a", {"scripts/lib/mod.py": "class K:\n    def gone(self):\n        pass\n"})
    absent = make_tree(tmp_path / "b", {"scripts/lib/mod.py": "class K:\n    def kept(self):\n        pass\n"})
    assert wheres(violations(present, "scripts/lib/mod.py::K.gone\n")) == {"scripts/lib/mod.py:2"}
    assert violations(absent, "scripts/lib/mod.py::K.gone\n") == []


def test_symbol_module_that_does_not_parse_fails_closed(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"scripts/lib/mod.py": "def (:\n"})
    found = violations(root, SYMBOL_LIST)
    assert len(found) == 1 and "cannot prove" in found[0].detail


@pytest.mark.parametrize(
    ("content", "line"),
    [
        ("from mod import gone\n", 1),
        ("from scripts.lib.mod import kept, gone\n", 1),
        ("import mod\nmod.gone()\n", 2),
        ("import scripts.lib.mod as m\nm.gone()\n", 2),
        ("from scripts.lib import mod\nmod.gone\n", 2),
        ("from unittest.mock import patch\n\n@patch('mod.gone')\ndef t(m):\n    pass\n", 3),
        ("import mod as m\n\ndef t(monkeypatch):\n    monkeypatch.setattr(m, 'gone', 1)\n", 4),
        ("CMD = 'python3 -c \"from mod import gone\"'\n", 1),
    ],
)
def test_symbol_reference_fails(tmp_path: Path, content: str, line: int) -> None:
    root = make_tree(tmp_path, {"scripts/lib/mod.py": MOD_WITHOUT, "tests/test_x.py": content})
    assert wheres(violations(root, SYMBOL_LIST)) == {f"tests/test_x.py:{line}"}


def test_symbol_reference_from_shell_fails(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path,
        {"scripts/lib/mod.py": MOD_WITHOUT, "scripts/run.sh": "python3 -c 'from mod import gone; gone()'\n"},
    )
    assert wheres(violations(root, SYMBOL_LIST)) == {"scripts/run.sh:1"}


def test_symbol_used_inside_its_own_module_fails(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"scripts/lib/mod.py": "def kept():\n    return gone()\n"})
    assert wheres(violations(root, SYMBOL_LIST)) == {"scripts/lib/mod.py:2"}


@pytest.mark.parametrize(
    "content",
    [
        "from mod import kept\n",
        "from other import gone\n",
        "import other\nother.gone()\n",
        "import mod\nmod.kept()\n",
        "# mod.gone was removed\nx = 1\n",
        '"""mod.gone moved; from mod import gone no longer works."""\n',
        "from unittest.mock import patch\n\n@patch('other.gone')\ndef t(m):\n    pass\n",
    ],
)
def test_symbol_non_reference_is_clean(tmp_path: Path, content: str) -> None:
    root = make_tree(tmp_path, {"scripts/lib/mod.py": MOD_WITHOUT, "tests/test_x.py": content})
    assert violations(root, SYMBOL_LIST) == []


def test_symbol_with_module_gone_and_no_reference_is_clean(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"scripts/lib/other.py": "x = 1\n"})
    assert violations(root, SYMBOL_LIST) == []


# ---------------------------------------------------------------------------
# Kind 2b: path::Class.method (the class survives, the method is retired)
# ---------------------------------------------------------------------------

MEMBER_LIST = "scripts/lib/mod.py::K.gone\n"
MOD_K = "class K:\n    def kept(self):\n        return 1\n"


def test_member_surviving_class_import_is_clean(tmp_path: Path) -> None:
    # The codex_gate reproduction: K keeps K.kept, the consumer imports K, calls K().kept()
    # and mentions gone in a comment. Importing the surviving class is no reference.
    consumer = "from scripts.lib.mod import K\n\n# K.gone was retired in golf 1\nK().kept()\n"
    root = make_tree(tmp_path, {"scripts/lib/mod.py": MOD_K, "tests/test_x.py": consumer})
    assert violations(root, MEMBER_LIST) == []


@pytest.mark.parametrize(
    ("rel", "content"),
    [
        ("tests/test_x.py", "from mod import K\nk = K()\nk.kept()  # not k.gone\n"),
        ("tests/test_x.py", "import mod\nk = getattr(mod, 'K')\n# gone\n"),
        ("tests/test_x.py", "from mod import K\n\ndef a():\n    k = K()\n\ndef b(k):\n    return k.gone()\n"),
        ("tests/test_x.py", "from mod import K, Other\nk = K()\nk = Other()\nk.gone()\n"),
        ("tests/test_x.py", "from mod import Other\nOther.gone()\nOther().gone()\n"),
        ("tests/test_x.py", "from other import K\nK.gone()\nK().gone()\n"),
        ("tests/test_x.py", "from mod import K\n\nclass T:\n    k = K()\n\n    def m(self):\n        return k.gone()\n"),
        ("tests/test_x.py", "CMD = 'python3 -c \"from mod import K; K().kept()\"'  # gone\n"),
        ("scripts/run.sh", "# K.gone is retired\npython3 -c 'from mod import K; K().kept()'\n"),
    ],
)
def test_member_non_reference_is_clean(tmp_path: Path, rel: str, content: str) -> None:
    root = make_tree(tmp_path, {"scripts/lib/mod.py": MOD_K, rel: content})
    assert violations(root, MEMBER_LIST) == []


@pytest.mark.parametrize(
    ("content", "line"),
    [
        ("from mod import K\nK.gone()\n", 2),
        ("from mod import K\nK().gone()\n", 2),
        ("from mod import K\nK(1, x=2).gone\n", 2),
        ("from mod import K as Alias\nAlias().gone()\n", 2),
        ("import mod\nmod.K.gone()\n", 2),
        ("import scripts.lib.mod as m\nm.K().gone()\n", 2),
        ("from mod import K\n\ndef f():\n    k = K(1)\n    return k.gone()\n", 5),
        ("from mod import K\nCLIENT = K()\n\ndef f():\n    return CLIENT.gone()\n", 5),
        ("from mod import K\nif (k := K()):\n    k.gone()\n", 3),
        ("from mod import K\nfn = getattr(K, 'gone')\n", 2),
        ("from mod import K\nk = K()\nok = hasattr(k, 'gone')\n", 3),
        ("from mod import K\nfn = getattr(K(), 'gone')\n", 2),
        ("from unittest import mock\nfrom mod import K\n\n@mock.patch.object(K, 'gone')\ndef t(m):\n    pass\n", 4),
        ("from unittest.mock import patch\nimport mod\n\nclass T:\n    @patch.object(mod.K, 'gone')\n    def t(self, m):\n        pass\n", 5),
        ("from mod import K\n\ndef t(monkeypatch):\n    monkeypatch.setattr(K, 'gone', lambda self: 1)\n", 4),
        ("from unittest.mock import patch\n\n@patch('mod.K.gone')\ndef t(m):\n    pass\n", 3),
        ("from unittest.mock import patch\n\n@patch('scripts.lib.mod.K.gone')\ndef t(m):\n    pass\n", 3),
        ("CMD = 'python3 -c \"from mod import K; K().gone()\"'\n", 1),
    ],
)
def test_member_access_fails(tmp_path: Path, content: str, line: int) -> None:
    root = make_tree(tmp_path, {"scripts/lib/mod.py": MOD_K, "tests/test_x.py": content})
    assert wheres(violations(root, MEMBER_LIST)) == {f"tests/test_x.py:{line}"}


def test_member_access_from_shell_fails(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path,
        {"scripts/lib/mod.py": MOD_K, "scripts/run.sh": "python3 -c 'from mod import K; K().gone()'\n"},
    )
    assert wheres(violations(root, MEMBER_LIST)) == {"scripts/run.sh:1"}


def test_member_access_inside_its_own_module_fails(tmp_path: Path) -> None:
    module = MOD_K + "\n\nK.gone\nINSTANCE = K()\nINSTANCE.gone()\n"
    root = make_tree(tmp_path, {"scripts/lib/mod.py": module})
    assert wheres(violations(root, MEMBER_LIST)) == {"scripts/lib/mod.py:6", "scripts/lib/mod.py:8"}


def test_member_of_nested_class_follows_the_full_owner(tmp_path: Path) -> None:
    module = "class K:\n    class Inner:\n        def kept(self):\n            return 1\n"
    consumer = "from mod import K\nK.Inner().kept()\nK.Inner.gone()\nK.gone()\n"
    root = make_tree(tmp_path, {"scripts/lib/mod.py": module, "tests/test_x.py": consumer})
    assert wheres(violations(root, "scripts/lib/mod.py::K.Inner.gone\n")) == {"tests/test_x.py:3"}


# ---------------------------------------------------------------------------
# Kind 3: env:NAME
# ---------------------------------------------------------------------------

ENV_LIST = "env:VNX_OLD_FLAG\n"


@pytest.mark.parametrize(
    ("rel", "content", "line"),
    [
        ("scripts/a.py", "import os\nv = os.environ.get('VNX_OLD_FLAG')\n", 2),
        ("scripts/a.py", "import os\nv = os.getenv('VNX_OLD_FLAG', '0')\n", 2),
        ("scripts/a.py", "import os\nv = os.environ['VNX_OLD_FLAG']\n", 2),
        ("scripts/a.py", "import os\nif 'VNX_OLD_FLAG' in os.environ:\n    pass\n", 2),
        ("scripts/a.py", "from env_flags import env_flag\nON = env_flag('VNX_OLD_FLAG')\n", 2),
        ("scripts/a.py", "def f(env):\n    return env.pop('VNX_OLD_FLAG', None)\n", 2),
        ("scripts/a.py", "CMD = 'echo ${VNX_OLD_FLAG:-0}'\n", 1),
        ("scripts/run.sh", "if [ -n \"${VNX_OLD_FLAG:-}\" ]; then exit 0; fi\n", 1),
        ("scripts/run.sh", "echo $VNX_OLD_FLAG\n", 1),
        (".github/workflows/ci.yml", "steps:\n  - if: ${{ env.VNX_OLD_FLAG == '1' }}\n", 2),
        ("dashboard/x.ts", "const on = process.env.VNX_OLD_FLAG === '1';\n", 1),
    ],
)
def test_env_read_fails(tmp_path: Path, rel: str, content: str, line: int) -> None:
    root = make_tree(tmp_path, {rel: content})
    assert wheres(violations(root, ENV_LIST)) == {f"{rel}:{line}"}


@pytest.mark.parametrize(
    ("rel", "content"),
    [
        ("tests/test_a.py", "def t(monkeypatch):\n    monkeypatch.setenv('VNX_OLD_FLAG', '1')\n"),
        ("tests/test_a.py", "def t(monkeypatch):\n    monkeypatch.delenv('VNX_OLD_FLAG', raising=False)\n"),
        ("scripts/a.py", "import os\nos.environ['VNX_OLD_FLAG'] = '1'\n"),
        ("scripts/a.py", "def f(env):\n    env.pop('VNX_OLD_FLAG', None)\n"),
        ("scripts/a.py", "ENV = {'VNX_OLD_FLAG': '1'}\n"),
        ("scripts/a.py", "# reads $VNX_OLD_FLAG\nx = 1\n"),
        ("scripts/a.py", '"""Honours ${VNX_OLD_FLAG}."""\n'),
        ("scripts/a.py", "v = 'VNX_OLD_FLAG_EXTRA'\nw = '$VNX_OLD_FLAG_EXTRA'\n"),
        ("scripts/run.sh", "export VNX_OLD_FLAG=1\n# echo $VNX_OLD_FLAG\n"),
        ("docs/flags.md", "Set `$VNX_OLD_FLAG`.\n"),
    ],
)
def test_env_non_read_is_clean(tmp_path: Path, rel: str, content: str) -> None:
    root = make_tree(tmp_path, {rel: content})
    assert violations(root, ENV_LIST) == []


# ---------------------------------------------------------------------------
# Readers, comment stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "first_line", "reader"),
    [
        ("scripts/a.py", "", "python"),
        ("templates/settings.json.j2", "{", "plain"),
        ("templates/worker_permissions.yaml.tmpl", "", "hash"),
        ("scripts/launchd/x.plist.template", "<?xml", "xml"),
        ("vnx.env.example", "", "hash"),
        ("Makefile", "", "hash"),
        ("bin/vnx", "#!/usr/bin/env bash", "hash"),
        ("hooks/git/pre-push", "#!/bin/sh", "hash"),
        ("bin/tool", "#!/usr/bin/env python3", "python"),
        ("bin/notes", "plain words", None),
        ("docs/a.md", "", None),
        ("scripts/x.sql", "", None),
        ("state/receipts.ndjson", "", None),
    ],
)
def test_classify(rel: str, first_line: str, reader: str | None) -> None:
    assert check.classify(rel, first_line) == reader


def test_strip_hash_comments_keeps_quoted_hashes_and_array_lengths() -> None:
    text = 'echo "a # b" ${#ARR[@]} # tail\n# whole line\nx=1'
    assert check.strip_hash_comments(text) == 'echo "a # b" ${#ARR[@]} \n\nx=1'


# ---------------------------------------------------------------------------
# main: flags, exit codes, and the red run
# ---------------------------------------------------------------------------


def test_main_red_run_fails_on_every_line_then_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    list_file = tmp_path / "retired_paths.txt"
    list_file.write_text("scripts/foo.py\nscripts/lib/mod.py::gone\nenv:VNX_OLD_FLAG\n", encoding="utf-8")
    base = make_tree(
        tmp_path / "base",
        {
            "scripts/foo.py": "x = 1\n",
            "scripts/lib/mod.py": "def gone():\n    return 1\n",
            "scripts/run.sh": "echo ${VNX_OLD_FLAG}\n",
        },
    )
    head = make_tree(tmp_path / "head", {"scripts/lib/mod.py": MOD_WITHOUT, "scripts/run.sh": "echo ok\n"})

    assert check.main(["--root", str(base), "--list", str(list_file)]) == 1
    red = capsys.readouterr().out
    assert "3 of 3 retired entries" in red
    for entry in ("scripts/foo.py —", "scripts/lib/mod.py::gone —", "env:VNX_OLD_FLAG —"):
        assert entry in red

    assert check.main(["--root", str(head), "--list", str(list_file)]) == 0
    assert "none of the 3 retired entries" in capsys.readouterr().out


def test_main_skips_the_list_inside_the_root(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"lists/retired.txt": "scripts/foo.py\n"})
    assert check.main(["--root", str(root), "--list", str(root / "lists" / "retired.txt")]) == 0


def test_main_malformed_list_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree(tmp_path, {"a.py": "x = 1\n"})
    bad = tmp_path / "bad.txt"
    bad.write_text("env:not-a-name\n", encoding="utf-8")
    assert check.main(["--root", str(root), "--list", str(bad)]) == 2
    assert "env entry" in capsys.readouterr().err


def test_main_missing_list_exits_2(tmp_path: Path) -> None:
    root = make_tree(tmp_path, {"a.py": "x = 1\n"})
    assert check.main(["--root", str(root), "--list", str(tmp_path / "absent.txt")]) == 2


def test_main_root_outside_git_exits_2(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    list_file = tmp_path / "list.txt"
    list_file.write_text("scripts/foo.py\n", encoding="utf-8")
    assert check.main(["--root", str(plain), "--list", str(list_file)]) == 2
