"""Tree mode of wiring_gate, counting rule version 4, and a pin of the PR mode."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import wiring_gate


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _make(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _unused(repo: Path, tmp_path: Path, **kw) -> set:
    result = wiring_gate.check_tree_wiring("HEAD", repo=repo, state_dir=tmp_path / "s", **kw)
    return {s["name"] for syms in result["files"].values() for s in syms}


def test_own_file_use_counts(tmp_path):
    repo = _make(tmp_path, {"scripts/a.py": """
        def helper():
            return 1

        def entry_only_here():
            return helper()
        """})
    assert _unused(repo, tmp_path) == {"entry_only_here"}


def test_recursion_only_is_reported(tmp_path):
    repo = _make(tmp_path, {"scripts/a.py": """
        def rec(n):
            return rec(n - 1) if n else 0
        """})
    assert _unused(repo, tmp_path) == {"rec"}


def test_string_mention_in_own_file_does_not_count(tmp_path):
    repo = _make(tmp_path, {"scripts/a.py": '''
        def ghost():
            return 1

        # ghost is mentioned here
        NOTE = "ghost"
        '''})
    assert _unused(repo, tmp_path) == {"ghost"}


def test_heredoc_in_shell_file_counts(tmp_path):
    repo = _make(tmp_path, {
        "scripts/a.py": "def from_shell():\n    return 1\n\n\ndef from_c():\n    return 2\n",
        "scripts/run.sh": "#!/bin/bash\npython3 - <<'PY'\nfrom a import from_shell\nfrom_shell()\nPY\n"
                          "python3 -c \"import a; a.from_c()\"\n",
    })
    assert _unused(repo, tmp_path) == set()


def test_shell_text_outside_python_does_not_count(tmp_path):
    repo = _make(tmp_path, {
        "scripts/a.py": "def only_echoed():\n    return 1\n",
        "scripts/run.sh": "#!/bin/bash\necho only_echoed\n",
    })
    assert _unused(repo, tmp_path) == {"only_echoed"}


def test_lru_cache_is_not_registration_register_probe_is(tmp_path):
    repo = _make(tmp_path, {"scripts/a.py": """
        import functools

        @functools.lru_cache()
        def cached():
            return 1

        @register_probe("x")
        class Probe:
            pass

        app = FastAPI()

        @app.get("/x")
        def route():
            return 1

        @other.get("/y")
        def not_an_app():
            return 1
        """})
    assert _unused(repo, tmp_path) == {"cached", "not_an_app"}


def test_skip_entry_without_reason_exits_2(tmp_path):
    repo = _make(tmp_path, {
        "scripts/a.py": "def lonely():\n    return 1\n",
        "scripts/ci/wiring_tree_skips.yaml": "- name: lonely\n  file: scripts/a.py\n",
    })
    with pytest.raises(wiring_gate.WiringGateError):
        wiring_gate.check_tree_wiring("HEAD", repo=repo, state_dir=tmp_path / "s")
    proc = subprocess.run(
        [sys.executable, str(VNX_ROOT / "scripts/lib/wiring_gate.py"), "--tree"],
        cwd=repo, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "VNX_STATE_DIR": str(tmp_path / "s"),
             "PYTHONPATH": str(VNX_ROOT / "scripts" / "lib")},
    )
    assert proc.returncode == 2, proc.stderr


@pytest.mark.parametrize("bad", ["- name: x\n  file: scripts/a.py\n  reason: ''\n",
                                 "{}\n", "- just-a-string\n", "- [unclosed\n"])
def test_malformed_skip_file_is_refused(tmp_path, bad):
    repo = _make(tmp_path, {
        "scripts/a.py": "def lonely():\n    return 1\n",
        "scripts/ci/wiring_tree_skips.yaml": bad,
    })
    with pytest.raises(wiring_gate.WiringGateError):
        wiring_gate.check_tree_wiring("HEAD", repo=repo, state_dir=tmp_path / "s")


def test_skip_entry_with_reason_matches_name_and_file(tmp_path):
    repo = _make(tmp_path, {
        "scripts/a.py": "def lonely():\n    return 1\n",
        "scripts/b.py": "def other_one():\n    return 2\n",
        "scripts/ci/wiring_tree_skips.yaml":
            "- name: lonely\n  file: scripts/a.py\n  reason: dispatch via globals()\n"
            "- name: other_one\n  file: scripts/elsewhere.py\n  reason: wrong file\n",
    })
    result = wiring_gate.check_tree_wiring("HEAD", repo=repo, state_dir=tmp_path / "s")
    assert list(result["files"]) == ["scripts/b.py"]
    assert result["totals"]["skipped"] == 1


def test_field_tests_excluded_by_default_and_prefix_repeatable(tmp_path):
    repo = _make(tmp_path, {
        "scripts/benchmark/field-tests/f.py": "def in_field():\n    return 1\n",
        "scripts/spikes/s.py": "def in_spike():\n    return 1\n",
        "scripts/a.py": "def real():\n    return 1\n",
    })
    result = wiring_gate.check_tree_wiring("HEAD", repo=repo, state_dir=tmp_path / "s")
    assert set(result["files"]) == {"scripts/spikes/s.py", "scripts/a.py"}
    assert result["excluded_prefixes"] == ["scripts/benchmark/field-tests/"]
    more = wiring_gate.check_tree_wiring(
        "HEAD", repo=repo, state_dir=tmp_path / "s", exclude_prefixes=("scripts/spikes/",))
    assert set(more["files"]) == {"scripts/a.py"}
    assert "scripts/spikes/" in more["excluded_prefixes"]


_DIFF = textwrap.dedent('''\
    diff --git a/scripts/lib/pin_mod.py b/scripts/lib/pin_mod.py
    new file mode 100644
    --- /dev/null
    +++ b/scripts/lib/pin_mod.py
    @@ -0,0 +1,6 @@
    +def pin_only_here():
    +    return 1
    +
    +
    +def pin_also_here():
    +    return pin_only_here()
    ''')


def test_pr_mode_result_is_pinned(tmp_path):
    root = tmp_path / "checkout"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "scripts" / "lib" / "pin_mod.py").write_text(
        "def pin_only_here():\n    return 1\n\n\ndef pin_also_here():\n    return pin_only_here()\n")
    result = wiring_gate.analyse_diff(
        _DIFF, state_dir=tmp_path / "s", root=str(root), required=True)
    # own-file use stays NOT a caller in the PR mode
    assert result.status == "fail"
    assert [(s.name, s.file, s.line, s.kind) for s in result.unwired] == [
        ("pin_only_here", "scripts/lib/pin_mod.py", 1, "function"),
        ("pin_also_here", "scripts/lib/pin_mod.py", 5, "function"),
    ]
    assert result.total_checked == 2
    assert result.summary == "2 unwired symbol(s): pin_only_here, pin_also_here"
