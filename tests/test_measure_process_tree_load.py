"""Tests for scripts/measure_process_tree_load.py (RS-1b, rekenslot-per-machine).

The pure parsers, the tree walk, the classifier and the summary live in
``scripts/lib/process_tree_load.py`` and are tested in
``tests/test_process_tree_load.py``. Only the sampler, the exclusive NDJSON
writer and the CLI are exercised here.
"""

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


def test_help_in_a_fresh_interpreter_finds_the_library(mod):
    """-I drops the script directory from sys.path; the CLI must add scripts/lib itself."""
    result = subprocess.run([PYTHON, "-I", "scripts/measure_process_tree_load.py", "--help"],
                            cwd=REPO_ROOT, capture_output=True, text=True,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_missing_compressor_line_is_null_with_reason(mod, monkeypatch):
    texts = {mod.PS_CMD: (PS_TEXT, None), mod.SWAP_CMD: (SWAP_TEXT, None),
             mod.VM_STAT_CMD: ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n", None)}
    monkeypatch.setattr(mod, "_run", lambda cmd, timeout=10: texts[cmd])
    sample, outcome = mod.take_sample(500)
    assert outcome == "sample"
    assert sample["compressor"] is None
    assert any(reason.startswith("compressor:") for reason in sample["reasons"])
    assert sample["swap_used"] == int(3813.62 * 1024 ** 2)


def test_unreadable_swap_is_null_with_reason_not_zero(mod, monkeypatch):
    texts = {mod.PS_CMD: (PS_TEXT, None), mod.SWAP_CMD: (None, "exit 1"),
             mod.VM_STAT_CMD: (VM_STAT_TEXT, None)}
    monkeypatch.setattr(mod, "_run", lambda cmd, timeout=10: texts[cmd])
    sample, outcome = mod.take_sample(500)
    assert outcome == "sample"
    assert sample["swap_used"] is None
    assert any(reason.startswith("swap_used:") for reason in sample["reasons"])
    assert sample["compressor"] == 569832 * 16384


def test_run_reports_a_missing_binary(mod):
    text, error = mod._run(("vnx-no-such-binary-7f3a",))
    assert text is None and error


def test_snapshot_without_the_root_is_root_gone(mod, monkeypatch):
    texts = {mod.PS_CMD: (PS_TEXT, None), mod.SWAP_CMD: (SWAP_TEXT, None),
             mod.VM_STAT_CMD: (VM_STAT_TEXT, None)}
    monkeypatch.setattr(mod, "_run", lambda cmd, timeout=10: texts[cmd])
    _, outcome = mod.take_sample(777)
    assert outcome == "root_gone"


@pytest.mark.parametrize("ps_result", [
    (None, "exit 1"), (None, "timed out"), (None, "empty output"),
    ("not a ps line\n", None),
])
def test_snapshot_failure_is_not_a_vanished_root(mod, monkeypatch, ps_result):
    texts = {mod.PS_CMD: ps_result, mod.SWAP_CMD: (SWAP_TEXT, None),
             mod.VM_STAT_CMD: (VM_STAT_TEXT, None)}
    monkeypatch.setattr(mod, "_run", lambda cmd, timeout=10: texts[cmd])
    sample, outcome = mod.take_sample(500)
    assert outcome == "snapshot_failed"
    assert all(sample[f] is None for f in ("n_xdist", "n_other", "xdist_cpu",
                                           "other_cpu", "rss"))
    assert any(reason.startswith("ps_failed: ") for reason in sample["reasons"])
    assert sample["swap_used"] == int(3813.62 * 1024 ** 2)
    assert sample["compressor"] == 569832 * 16384


def test_failed_snapshot_continues_and_summary_skips_it(mod, tmp_path, monkeypatch):
    good = {"n_xdist": 2, "n_other": 1, "xdist_cpu": 190.0, "other_cpu": 40.0,
            "rss": 100, "swap_used": 10, "compressor": 20}
    failed = {**dict.fromkeys(mod.SAMPLE_FIELDS), "reasons": ["ps_failed: exit 1"]}
    steps = [("sample", good), ("sample", good), ("snapshot_failed", failed),
             ("sample", good), ("root_gone", {})]

    def fake_take_sample(root_pid):
        outcome, body = steps.pop(0)
        return {"ts": "2026-01-01T00:00:00+00:00", "root_pid": root_pid, **body}, outcome

    monkeypatch.setattr(mod, "take_sample", fake_take_sample)
    out = tmp_path / "sequence.ndjson"
    summary, code = mod.run_measurement(123, 0, None, out)
    lines = [json.loads(row) for row in out.read_text().splitlines()]
    assert code == 0 and len(lines) == 4
    failed_lines = [ln for ln in lines if any(r.startswith("ps_failed:") for r in ln.get("reasons", []))]
    assert len(failed_lines) == 1
    assert all(failed_lines[0][f] is None for f in ("n_xdist", "n_other", "xdist_cpu",
                                                    "other_cpu", "rss"))
    assert summary["samples"] == 3 and summary["failed_snapshots"] == 1
    assert summary["fields"]["xdist_cpu"]["n"] == 3
    assert summary["fields"]["xdist_cpu"]["mean"] == pytest.approx(190.0)


def test_every_snapshot_failed_exits_one(mod, tmp_path, monkeypatch):
    failed = {**dict.fromkeys(mod.SAMPLE_FIELDS), "reasons": ["ps_failed: exit 1"]}
    monkeypatch.setattr(mod, "take_sample", lambda pid: (dict(failed), "snapshot_failed"))
    out = tmp_path / "never.ndjson"
    summary, code = mod.run_measurement(123, 0.01, 0.05, out)
    assert code == 1
    assert summary["samples"] == 0 and summary["failed_snapshots"] >= 1
    assert not out.exists()


@pytest.mark.skipif(shutil.which("vm_stat") is None, reason="vm_stat missing (not macOS, so CI skips this live run)")
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


def _ps_row(pid, ppid, command):
    return f"{pid:5d} {ppid:5d} {pid:5d}   1.0   4096 {command}\n"


def _match_run(mod, tmp_path, monkeypatch, ps_text, name, ps_error=None):
    """Drive `--match` through the CLI with the `ps` and own-pid seams."""
    outputs = {mod.PS_CMD: (ps_text, ps_error), mod.SWAP_CMD: (SWAP_TEXT, None),
               mod.VM_STAT_CMD: (VM_STAT_TEXT, None)}
    monkeypatch.setattr(mod, "_run", lambda cmd, timeout=10: outputs.get(cmd, (None, "exit 1")))
    monkeypatch.setattr(mod, "_own_pid", lambda: 4242, raising=False)
    out = tmp_path / f"{name}.ndjson"
    code = mod.main(["--match", "local_ci_gate", "--out", str(out), "--duration", "0"])
    return code, out


def test_match_never_selects_the_sampler_itself(mod, tmp_path, monkeypatch):
    ps_text = _ps_row(4242, 1, "python3 measure_process_tree_load.py --match local_ci_gate")
    code, out = _match_run(mod, tmp_path, monkeypatch, ps_text, "self")
    assert code == 3 and not out.exists()


def test_match_never_selects_the_sampler_ancestor_wrapper(mod, tmp_path, monkeypatch):
    ps_text = (_ps_row(4300, 1, "zsh -c 'python3 measure_process_tree_load.py --match local_ci_gate'")
               + _ps_row(4242, 4300, "python3 -c sampler"))
    code, out = _match_run(mod, tmp_path, monkeypatch, ps_text, "wrapper")
    assert code == 3 and not out.exists()


def test_match_selects_a_real_target_beside_the_sampler(mod, tmp_path, monkeypatch):
    ps_text = (_ps_row(4300, 1, "zsh -c 'python3 measure_process_tree_load.py --match local_ci_gate'")
               + _ps_row(4242, 4300, "python3 measure_process_tree_load.py --match local_ci_gate")
               + _ps_row(5100, 1, "/bin/bash scripts/local_ci_gate.sh --full"))
    code, out = _match_run(mod, tmp_path, monkeypatch, ps_text, "target")
    assert code == 0
    assert json.loads(out.read_text().splitlines()[0])["root_pid"] == 5100


@pytest.mark.parametrize("ps_text, ps_error", [("", "exit 1"), ("not a ps line\n", None)])
def test_match_unreadable_snapshot_stays_blocking(mod, tmp_path, monkeypatch, ps_text, ps_error):
    code, out = _match_run(mod, tmp_path, monkeypatch, ps_text, "unreadable", ps_error)
    assert code == 3 and not out.exists()


def test_exit_codes_for_bad_arguments_and_missing_root(tmp_path):
    out = tmp_path / "load.ndjson"
    def run(*extra):
        return subprocess.run([PYTHON, str(SCRIPT), *extra, "--out", str(out)],
                              capture_output=True, text=True, timeout=30, check=False)
    assert run().returncode == 2
    assert run("--root-pid", "999999", "--duration", "1").returncode == 3
    assert not out.exists()
    out.write_bytes(b"earlier measurement\n")
    existing = run("--root-pid", str(os.getpid()), "--duration", "0")
    assert existing.returncode == 2
    assert str(out) in existing.stderr and len(existing.stderr.strip().splitlines()) == 1
    assert out.read_bytes() == b"earlier measurement\n"
