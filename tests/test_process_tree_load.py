"""Tests for scripts/lib/process_tree_load.py (RS-1, rekenslot-per-machine).

The library is pure: text in, data out. Split 2
(``scripts/measure_process_tree_load.py``) adds the sampler, the CLI and the
exit codes on top of it, and its tests live in
``tests/test_measure_process_tree_load.py``. Every test here is platform-free
and needs no live process.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "scripts" / "lib"

PS_TEXT = """\
  500     1   500   5.0  20480 /usr/bin/python3 -m pytest tests/test_everything.py -n 2 --dist load
  501   500   500  98.5 102400 /usr/bin/python3 -u -c "import sys;exec(eval(sys.stdin.readline()))"
  502   500   500  97.0 101888 /usr/bin/python3 -u -c "import sys;exec(eval(sys.stdin.readline()))"
  503   500   500  41.0 204800 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --type=renderer
  900     1   900  12.0   4096 /usr/sbin/unrelated-daemon gw9
"""
SWAP_TEXT = "vm.swapusage: total = 5120.00M  used = 3813.62M  free = 1306.38M  (encrypted)\n"
VM_STAT_TEXT = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                "Pages occupied by compressor:                 569832.\n")


@pytest.fixture(scope="module")
def mod():
    """Import the library the way a caller with only scripts/lib on sys.path does."""
    sys.path.insert(0, str(LIB))
    try:
        import process_tree_load
    finally:
        sys.path.remove(str(LIB))
    return process_tree_load


def test_parse_ps_tree_and_sample(mod):
    procs = mod.parse_ps(PS_TEXT)
    assert [p["pid"] for p in procs] == [500, 501, 502, 503, 900]
    assert (procs[1]["ppid"], procs[1]["cpu"], procs[1]["rss_kb"]) == (500, 98.5, 102400)
    assert sorted(p["pid"] for p in mod.build_tree(procs, 500)) == [501, 502, 503]
    sample = mod.sample_tree(procs, 500)
    assert (sample["n_xdist"], sample["n_other"]) == (2, 1)  # pid 900 is outside the tree
    assert sample["xdist_cpu"] == pytest.approx(195.5)
    assert sample["other_cpu"] == pytest.approx(46.0)  # chrome 41.0 + the root's own 5.0
    assert sample["rss"] == (20480 + 102400 + 101888 + 204800) * 1024
    assert len(mod.parse_ps("garbage\n" + PS_TEXT + "501 x y\n")) == 5


@pytest.mark.parametrize("command,kind", [
    ('/usr/bin/python3 -u -c "import sys;exec(eval(sys.stdin.readline()))"', "xdist"),
    ("pytest -n 4 --dist loadfile", "xdist"),
    ("pytest -n 4 --dist=load gw3", "xdist"),
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --type=renderer", "other"),
    ("/usr/bin/python3 -m pytest tests", "other"),
])
def test_classifier_splits_xdist_from_other(mod, command, kind):
    assert mod.classify_command(command) == kind


def test_swap_and_compressor_parsers(mod):
    assert mod.parse_swapusage(SWAP_TEXT) == int(3813.62 * 1024 ** 2)
    assert mod.parse_swapusage("vm.swapusage: total = 1.00G  used = 0.00M  free = 1.00G") == 0
    assert mod.parse_swapusage("") is None
    assert mod.parse_swapusage("vm.swapusage: total = 5120.00M  free = 1306.38M") is None
    # A number the regex admits but float rejects, and a unit outside K/M/G:
    # both are null, never 0.
    assert mod.parse_swapusage("vm.swapusage: used = 1.2.3M  free = 1.00G") is None
    assert mod.parse_swapusage("vm.swapusage: used = 12.00X  free = 1.00G") is None
    assert mod.parse_vm_stat(VM_STAT_TEXT) == 569832 * 16384
    # The compressor line is missing: null, never 0.
    assert mod.parse_vm_stat("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n") is None
    assert mod.parse_vm_stat("Pages occupied by compressor: 42.\n") is None


def test_sample_tree_counts_the_root_own_cpu_as_other(mod):
    procs = mod.parse_ps("  600     1   600  88.0  4096 /usr/bin/python3 -m pytest tests/test_root_only.py\n")
    sample = mod.sample_tree(procs, 600)
    assert sample["xdist_cpu"] == 0
    assert sample["other_cpu"] == pytest.approx(88.0)
    assert sample["rss"] == 4096 * 1024


def test_summary_reports_cores_p95_and_note(mod):
    samples = [
        {"n_xdist": 2, "n_other": 1, "xdist_cpu": 190.0, "other_cpu": 40.0,
         "rss": 100, "swap_used": 10, "compressor": 20},
        {"n_xdist": 2, "n_other": 1, "xdist_cpu": 195.0, "other_cpu": None,
         "rss": 110, "swap_used": 10, "compressor": None},
        {"n_xdist": 2, "n_other": 1, "xdist_cpu": 195.0, "other_cpu": 45.0,
         "rss": 110, "swap_used": 10, "compressor": None},
    ]
    summary = mod.summarize(samples, 3.0, failed=2)
    assert summary["samples"] == 3
    assert summary["failed_snapshots"] == 2
    # The null other_cpu line cannot contribute a core count; the two complete
    # lines carry the p95 of 240.0 (= 2.4 cores).
    assert summary["cores_p95"] == pytest.approx(2.4)
    assert "not a budget" in summary["note"]
    assert summary["fields"]["compressor"]["n"] == 1
    assert summary["fields"]["compressor"]["max"] == 20
    assert summary["fields"]["other_cpu"]["n"] == 2


def test_summary_of_no_samples_is_null_not_zero(mod):
    summary = mod.summarize([], 0.0)
    assert summary["samples"] == 0
    assert summary["failed_snapshots"] == 0
    assert summary["cores_p95"] is None
    assert summary["fields"]["xdist_cpu"] == {"n": 0, "mean": None, "p50": None,
                                              "p95": None, "max": None}


def test_imports_in_a_fresh_interpreter_with_only_scripts_lib():
    """Prove the path split 2's CLI uses: only scripts/lib on sys.path, -I."""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(LIB)!r})\n"
        "import process_tree_load\n"
        f"sample = process_tree_load.sample_tree(process_tree_load.parse_ps({PS_TEXT!r}), 500)\n"
        "print(sample['n_xdist'], sample['n_other'])\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["2", "1"]
