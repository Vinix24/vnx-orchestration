"""A lib module that imports a same-named sibling binds the scripts/lib module, whatever sys.path says.

Eight names exist in scripts/ and in scripts/lib/. Each case runs in a subprocess with
scripts/ at sys.path[0] and scripts/lib after it, so nothing leaks into this process.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = str(ROOT / "scripts")
_LIB = str(ROOT / "scripts" / "lib")

PRELUDE = f"""
import os, sys
_front = [{_SCRIPTS!r}, {_LIB!r}, {str(ROOT)!r}]
sys.path[:] = _front + [p for p in sys.path if os.path.realpath(p or '.') not in map(os.path.realpath, _front)]
LIB = os.path.realpath({_LIB!r})
def in_lib(mod):
    return os.path.realpath(mod.__file__).startswith(LIB + os.sep)
import importlib.util
def load_file(name, path, execute=True):
    # The caller under test, loaded by path: the scripts/ wrapper of the same name must not stand in for it.
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    if execute:
        spec.loader.exec_module(mod)
    return mod
"""


def _run(body: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", PRELUDE + body],
        capture_output=True, text=True, cwd=tmp_path, timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def _ok(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == 0, proc.stdout + proc.stderr


SWEEP = """
import tempfile
from pathlib import Path
orphan_sweep = load_file("orphan_sweep", os.path.join(LIB, "orphan_sweep.py"))
d = Path(tempfile.mkdtemp())
(d / "dispatches" / "active").mkdir(parents=True)
orphan_sweep.sweep(project_id="vnx-dev", repo_root=d, data_dir=d, state_dir=d / "state", dry_run=True, list_sessions=lambda: [])
assert in_lib(sys.modules["crash_recovery_sweep"]), sys.modules["crash_recovery_sweep"].__file__
assert sys.modules["crash_recovery_sweep"].DEFAULT_MAX_ORPHANS
"""


def test_orphan_sweep_binds_lib_crash_recovery_sweep(tmp_path):
    _ok(_run(SWEEP, tmp_path))


def test_orphan_sweep_binds_lib_when_the_wrapper_was_imported_first(tmp_path):
    body = """
load_file("crash_recovery_sweep", os.path.join(%r, "crash_recovery_sweep.py"), execute=False)  # half executed: the state that broke main
""" % _SCRIPTS + SWEEP
    _ok(_run(body, tmp_path))


def test_patching_the_lib_module_steers_the_sweep_default(tmp_path):
    body = """
import tempfile
from pathlib import Path
lib = load_file("crash_recovery_sweep", os.path.join(LIB, "crash_recovery_sweep.py"))
orphan_sweep = load_file("orphan_sweep", os.path.join(LIB, "orphan_sweep.py"))
lib.is_pid_alive = lambda pid: True
d = Path(tempfile.mkdtemp())
(d / "dispatches" / "active").mkdir(parents=True)
orphan_sweep.sweep(project_id="vnx-dev", repo_root=d, data_dir=d, state_dir=d / "state", dry_run=True, list_sessions=lambda: [])
assert sys.modules["crash_recovery_sweep"] is lib
"""
    _ok(_run(body, tmp_path))


def test_qi_transfer_modules_bind_lib_pattern_reattribution(tmp_path):
    body = """
import qi_transfer_plan, qi_transfer_apply
lib = sys.modules["pattern_reattribution"]
assert in_lib(lib), lib.__file__
assert qi_transfer_plan.decide_row is lib.decide_row
assert qi_transfer_apply.decide_row is lib.decide_row
"""
    _ok(_run(body, tmp_path))


def test_qi_transfer_plan_binds_lib_when_the_wrapper_was_imported_first(tmp_path):
    body = f"""
load_file("pattern_reattribution", os.path.join({_SCRIPTS!r}, "pattern_reattribution.py"), execute=False)
import qi_transfer_plan
lib = sys.modules["pattern_reattribution"]
assert in_lib(lib), lib.__file__
assert qi_transfer_plan.decide_row is lib.decide_row
"""
    _ok(_run(body, tmp_path))


def test_cost_aware_lookup_reaches_the_lib_cost_tracker(tmp_path):
    body = """
import cost_tracker as scripts_version  # scripts/ is first: not the lib module
assert not in_lib(scripts_version)
import pool_scaling_policies
mod = sys.modules["pool_scaling_policies.cost_aware_v1"]
mod._estimate_hourly_cost([], [], 2)
lib = sys.modules["cost_tracker"]
assert in_lib(lib), lib.__file__
lib.recent_cost_per_hour = lambda size, mix: 123.0
assert mod._estimate_hourly_cost([], [], 2) == 123.0  # the $0.50/h fallback would give 1.0
"""
    _ok(_run(body, tmp_path))


def test_load_lib_module_refuses_a_name_without_a_lib_file(tmp_path):
    body = """
from lib_module import load_lib_module
for bad in ("no_such_lib_module_xyz", "../x", ""):
    try:
        load_lib_module(bad)
    except ImportError:
        continue
    raise SystemExit("accepted " + repr(bad))
"""
    _ok(_run(body, tmp_path))


def test_failed_load_restores_the_previous_entry(tmp_path):
    lib_dir = tmp_path / "libcopy"
    lib_dir.mkdir()
    (lib_dir / "lib_module.py").write_text((ROOT / "scripts/lib/lib_module.py").read_text())
    (lib_dir / "broken_mod.py").write_text("raise RuntimeError('boom')\n")
    body = f"""
import sys, types
sys.path.insert(0, {str(lib_dir)!r})
from lib_module import load_lib_module
prev = types.ModuleType("broken_mod")
sys.modules["broken_mod"] = prev
try:
    load_lib_module("broken_mod")
except RuntimeError:
    pass
else:
    raise SystemExit("failure swallowed")
assert sys.modules["broken_mod"] is prev
"""
    proc = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
