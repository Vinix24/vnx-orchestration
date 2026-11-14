"""Tree mode of wiring_gate (golf schoon W0a) and a characterisation of the PR mode."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

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


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "scripts" / "lib_a.py").write_text(
        "def used_fn():\n    return 1\n\n\ndef lonely_fn():\n    return 2\n\n\n"
        "class LonelyClass:\n    pass\n\n\ndef _private():\n    pass\n"
    )
    (root / "scripts" / "caller.py").write_text(
        "from lib_a import used_fn\n\nused_fn()\n"
    )
    (root / "tests" / "test_x.py").write_text("from lib_a import lonely_fn\nlonely_fn()\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


class TestTreeMode:
    def test_lists_unused_and_not_used(self, repo, tmp_path):
        result = wiring_gate.check_tree_wiring("HEAD", repo=repo, state_dir=tmp_path / "state")
        by_file = result["files"]["scripts/lib_a.py"]
        names = {s["name"]: s for s in by_file}
        assert set(names) == {"lonely_fn", "LonelyClass"}  # tests do not count as callers
        assert names["lonely_fn"]["line"] == 5
        assert names["LonelyClass"]["kind"] == "class"
        assert "scripts/caller.py" not in result["files"]
        assert result["sha"] == _git(repo, "rev-parse", "HEAD")
        assert result["ref"] == "HEAD"
        assert result["totals"]["unused"] == 2
        assert result["totals"]["unparsed"] == 0

    def test_reads_ref_not_working_tree(self, repo, tmp_path):
        (repo / "scripts" / "caller.py").write_text("lonely_fn = 1\nLonelyClass = 2\n")
        (repo / "scripts" / "new_file.py").write_text("def brand_new():\n    pass\n")
        result = wiring_gate.check_tree_wiring("HEAD", repo=repo, state_dir=tmp_path / "s")
        assert "scripts/new_file.py" not in result["files"]
        assert result["totals"]["unused"] == 2

    def test_parse_failure_is_listed_not_zero(self, repo, tmp_path):
        (repo / "scripts" / "broken.py").write_text("def oops(:\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "broken")
        result = wiring_gate.check_tree_wiring("HEAD", repo=repo, state_dir=tmp_path / "s")
        assert result["unparsed"] == ["scripts/broken.py"]
        assert result["totals"]["unparsed"] == 1

    def test_skip_list_and_prefix(self, repo, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        (state / "wiring_skip.yaml").write_text("library_exports:\n  - lonely_fn\n")
        result = wiring_gate.check_tree_wiring("HEAD", repo=repo, state_dir=state)
        assert [s["name"] for s in result["files"]["scripts/lib_a.py"]] == ["LonelyClass"]
        assert result["totals"]["skipped"] == 1
        other = wiring_gate.check_tree_wiring(
            "HEAD", prefix="nothing/", repo=repo, state_dir=state)
        assert other["totals"]["files"] == 0

    def test_unknown_ref_raises(self, repo, tmp_path):
        with pytest.raises(wiring_gate.WiringGateError):
            wiring_gate.check_tree_wiring("no-such-ref", repo=repo, state_dir=tmp_path)

    def test_cli_json(self, repo, tmp_path):
        proc = subprocess.run(
            [sys.executable, str(VNX_ROOT / "scripts/lib/wiring_gate.py"),
             "--tree", "--ref", "HEAD", "--json"],
            cwd=repo, capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "VNX_STATE_DIR": str(tmp_path / "s"),
                 "PYTHONPATH": str(VNX_ROOT / "scripts" / "lib")},
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["totals"]["unused"] == 2


PINNED_DIFF = textwrap.dedent("""\
    diff --git a/scripts/lib/pinned.py b/scripts/lib/pinned.py
    new file mode 100644
    --- /dev/null
    +++ b/scripts/lib/pinned.py
    @@ -0,0 +1,9 @@
    +def alpha():
    +    pass
    +
    +def _hidden():
    +    pass
    +
    +class Beta:
    +    pass
    +
    """)


def test_pr_mode_characterisation(tmp_path):
    with patch.object(wiring_gate, "_get_pr_diff", return_value=PINNED_DIFF), \
            patch.object(wiring_gate, "_grep_callers", return_value=0):
        result = wiring_gate.check_pr_wiring(1, state_dir=tmp_path)
    assert result.to_dict() == {
        "status": "advisory",
        "unwired": [
            {"name": "alpha", "file": "scripts/lib/pinned.py", "line": 1, "kind": "function"},
            {"name": "Beta", "file": "scripts/lib/pinned.py", "line": 7, "kind": "class"},
        ],
        "skipped": [],
        "total_checked": 2,
        "test_file_skipped": 0,
        "summary": "2 unwired symbol(s): alpha, Beta",
    }
