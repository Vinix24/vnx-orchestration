"""Tests for scripts/measure_process_tree_load.py (RS-1, rekenslot-per-machine)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "measure_process_tree_load.py"
PYTHON = sys.executable
FIELDS = {"ts", "root_pid", "n_xdist", "n_other", "xdist_cpu", "other_cpu",
          "rss", "swap_used", "compressor"}

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


def test_script_exists():
    assert SCRIPT.exists(), f"{SCRIPT} is missing"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("measure_process_tree_load", SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_ps_tree_and_sample(mod):
    procs = mod.parse_ps(PS_TEXT)
    assert [p["pid"] for p in procs] == [500, 501, 502, 503, 900]
    assert (procs[1]["ppid"], procs[1]["cpu"], procs[1]["rss_kb"]) == (500, 98.5, 102400)
    assert sorted(p["pid"] for p in mod.build_tree(procs, 500)) == [501, 502, 503]
    sample = mod.sample_tree(procs, 500)
    assert (sample["n_xdist"], sample["n_other"]) == (2, 1)  # pid 900 is outside the tree
    assert sample["xdist_cpu"] == pytest.approx(195.5)
    assert sample["other_cpu"] == pytest.approx(41.0)
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
    assert mod.parse_vm_stat(VM_STAT_TEXT) == 569832 * 16384
    assert mod.parse_vm_stat("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n") is None
    assert mod.parse_vm_stat("Pages occupied by compressor: 42.\n") is None


def test_missing_compressor_line_is_null_with_reason(mod, monkeypatch):
    texts = {mod.PS_CMD: PS_TEXT, mod.SWAP_CMD: SWAP_TEXT,
             mod.VM_STAT_CMD: "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"}
    monkeypatch.setattr(mod, "_run", lambda cmd, timeout=10: texts[cmd])
    sample, root_seen = mod.take_sample(500)
    assert root_seen is True
    assert sample["compressor"] is None
    assert any(reason.startswith("compressor:") for reason in sample["reasons"])
    assert sample["swap_used"] == int(3813.62 * 1024 ** 2)


def test_summary_reports_cores_p95_and_note(mod):
    samples = [
        {"n_xdist": 2, "n_other": 1, "xdist_cpu": 190.0, "other_cpu": 40.0,
         "rss": 100, "swap_used": 10, "compressor": 20},
        {"n_xdist": 2, "n_other": 1, "xdist_cpu": 195.0, "other_cpu": 45.0,
         "rss": 110, "swap_used": 10, "compressor": None},
    ]
    summary = mod.summarize(samples, 3.0)
    assert summary["samples"] == 2
    assert summary["cores_p95"] == pytest.approx(2.4)
    assert "not a budget" in summary["note"]
    assert summary["fields"]["compressor"]["n"] == 1
    assert summary["fields"]["compressor"]["max"] == 20


@pytest.mark.skipif(shutil.which("vm_stat") is None, reason="vm_stat missing (not macOS)")
def test_live_run_on_a_synthetic_tree(tmp_path):
    code = ("import subprocess, sys, time\n"
            "child = 'while True:\\n    sum(range(2000))\\n'\n"
            "kids = [subprocess.Popen([sys.executable, '-c', child]) for _ in range(2)]\n"
            "time.sleep(30)\n")
    parent = subprocess.Popen([PYTHON, "-c", code], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        time.sleep(0.5)
        out = tmp_path / "load.ndjson"
        result = subprocess.run([PYTHON, str(SCRIPT), "--root-pid", str(parent.pid),
                                 "--interval", "1", "--duration", "3", "--out", str(out),
                                 "--summary"], capture_output=True, text=True,
                                timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        lines = [json.loads(row) for row in out.read_text().splitlines() if row.strip()]
        assert len(lines) >= 2
        assert all(FIELDS <= set(line) and line["root_pid"] == parent.pid for line in lines)
        assert any(line["n_other"] >= 2 for line in lines)
        assert json.loads(result.stdout)["samples"] == len(lines)
    finally:
        try:
            os.killpg(os.getpgid(parent.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.wait(timeout=10)


def test_exit_codes_for_bad_arguments_and_missing_root(tmp_path):
    out = tmp_path / "load.ndjson"
    def run(*extra):
        return subprocess.run([PYTHON, str(SCRIPT), *extra, "--out", str(out)],
                              capture_output=True, text=True, timeout=30, check=False)
    assert run().returncode == 2
    assert run("--root-pid", "999999", "--duration", "1").returncode == 3
