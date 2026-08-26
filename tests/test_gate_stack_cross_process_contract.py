"""test_gate_stack_cross_process_contract.py — OI-1462: the gate-eis and the
gate-vervulling run in different processes and can resolve the SAME config
flag differently.

Root cause under test: two independent read-sites --
``review_gate_manager._build_default_review_stack()`` (the eiser, runs at
MODULE-IMPORT time) and ``gate_request_handler._ci_gate_available()`` (the
vervuller, runs later, possibly in a different process) -- both call
``config_runtime.get_bool("VNX_CI_GATE_REQUIRED")``. PR #1684 fixed the
LEESPAD (``config_runtime._resolve_state_dir`` now falls back to
``vnx_paths.resolve_paths()`` when ``$VNX_STATE_DIR`` is unset), but nothing
detects it when the two sides still disagree -- e.g. because one process can
reach the operator's config store and the other cannot.

A same-process test with monkeypatch is NOT a substitute here: the whole bug
is that the environment differs PER PROCESS. Every test below spawns two REAL
subprocesses (``subprocess.run([sys.executable, "-c", ...])``) with
deliberately different ``env=``.

Measured 2026-08-26 (T0, on main @7f93f681): three real processes differing
ONLY in environment (state-dir set / unset, cwd inside/outside the repo) all
read True for VNX_CI_GATE_REQUIRED, because (a) #1684's resolver fallback
finds this repo's real central store regardless of cwd, and (b) the registry
default flipped "0" -> "1" (OI-1385) since this flag was PARK on 21-08. A
test that seeds the DB with the SAME value as the registry default is green
for the wrong reason: a process that cannot reach the DB at all would still
read the matching default and look identical to one that read the real
override. Every test below therefore seeds an operator override that
DIVERGES from the registry default, and asserts on WHERE the value came from
(``autowired``: True = read from the DB, False = env/registry fallback) --
not only on what the value was. Two processes that coincidentally name the
same number from different sources is not the contract being pinned here.

SECOND round of the same lesson (T0 review, 2026-08-26): the first version of
this file isolated the vervuller with a fake, empty $HOME and a cwd outside
the repo -- and was ITSELF environment-dependent, exactly the defect class it
exists to catch. ``vnx_paths._resolve_vnx_home()`` derives ``VNX_HOME`` from
its OWN ``__file__`` location, not from cwd or $HOME. When the scripts a
subprocess imports live inside a REAL, long-lived dev checkout,
``_resolve_state_root``'s "existing dev checkout: keep resolving to
``<project_root>/.vnx-data``" branch (step 4) finds whatever real
``.vnx-data`` has accumulated there from actual day-to-day use -- BEFORE the
fake-$HOME central-store check or the fresh-install fallback are ever
reached. Measured: the real ``~/Development/vnx-orchestration`` checkout
carries an 835 KB ``runtime_coordination.db`` from 2026-08-15. A subprocess
whose scripts live there resolves ``autowired=True`` regardless of $HOME,
and the "GREEN" test would have written an operator override into that real,
shared store (with a nonsense ``project_id=".vnx-data"``, since it derived
the id from ``resolved_state_dir.parent.name``).

The fix: every subprocess whose resolution must be test-controlled runs
against an ISOLATED COPY of ``scripts/`` placed under ``tmp_path``
(:func:`_isolated_scripts_copy`), never against this test file's own
checkout. A copy with no ``.git`` and no ``.vnx-project-id`` marker makes
``vnx_paths._resolve_state_root`` fall through to its explicit
collision-safe branch ("no resolvable project_id -> stay project-local"),
which computes a path entirely under ``tmp_path`` -- deterministically, on
any machine, regardless of what real state sits next to whatever checkout
hosts this test file. The GREEN case additionally plants an ISOLATED
``.vnx-project-id`` marker (also under ``tmp_path``) so the vervuller can
resolve a project_id purely through the file-based marker mechanism -- the
same mechanism a real project checkout uses -- without ever touching a real
identity registry or a real state store.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(LIB_DIR))

import config_registry as cr  # noqa: E402
import config_store_db as cs  # noqa: E402

FLAG = "VNX_CI_GATE_REQUIRED"
PID = "gatecontracttest"

# Process A driver -- the EISER: imports review_gate_manager, which resolves
# DEFAULT_REVIEW_STACK at MODULE-IMPORT time via config_runtime.get_bool.
# A always uses the REAL scripts/ (never the isolated copy): it addresses
# its store via an explicit VNX_STATE_DIR, so it never calls
# vnx_paths.resolve_paths() at all -- there is nothing for it to isolate.
_PROC_A_CODE = """
import json, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
import config_runtime
autowired = config_runtime.autowire()
import review_gate_manager as rgm
print(json.dumps({
    "stack": rgm.DEFAULT_REVIEW_STACK,
    "flag_value": config_runtime.get_bool("VNX_CI_GATE_REQUIRED"),
    "autowired": autowired,
}))
"""

# Process B driver -- the VERVULLER: calls the real fulfilment-decision
# method. shutil.which is pinned to a fixed path so the gh binary's local
# presence can never be the variable that differs between A and B -- only
# the config-flag resolution may differ. Always run against an ISOLATED
# scripts copy (see module docstring) so its vnx_paths resolution can never
# land next to a real, already-populated checkout.
_PROC_B_CODE = """
import json, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
import config_runtime
autowired = config_runtime.autowire()
import gate_request_handler as grh
grh.shutil.which = lambda name: "/usr/bin/gh"
print(json.dumps({
    "flag_value": config_runtime.get_bool("VNX_CI_GATE_REQUIRED"),
    "ci_gate_available": grh.GateRequestHandlerMixin._ci_gate_available(None),
    "autowired": autowired,
}))
"""

# Probe driver -- asks, under a GIVEN env, what state dir vnx_paths'
# canonical resolver (the #1684 fallback) would compute, so a test can seed a
# store at EXACTLY that path. Always run against the isolated scripts copy,
# for the same reason as process B.
_PROBE_CODE = """
import json, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
import vnx_paths
print(json.dumps(vnx_paths.resolve_paths()))
"""


def _isolated_scripts_copy(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the real scripts/ tree under ``tmp_path`` so a subprocess
    importing vnx_paths from the copy can never resolve ``VNX_HOME`` (derived
    from vnx_paths.py's own ``__file__``) back to THIS test file's checkout.

    Deliberately carries no ``.git`` and no ``.vnx-project-id`` marker: that
    forces ``vnx_paths._resolve_state_project_id`` to come up with no
    resolvable project_id (no env, no marker, no git remote), which routes
    ``_resolve_state_root`` through its explicit collision-safe fallback
    ("no resolvable project_id -> stay project-local") -- a path computed
    entirely under ``tmp_path``, regardless of what real ``.vnx-data`` sits
    next to whatever checkout actually hosts this test file (measured: a
    real dev checkout carries one from actual use; a disposable dispatch
    worktree does not -- the isolation must not depend on which is true).

    Excludes ``__pycache__``/``*.pyc`` -- copying the real tree (~16 MB) with
    those still takes well under a second locally; without them it is
    faster still and never leaks stale bytecode into the copy.
    """
    dest = tmp_path / "isolated_vnx_repo" / "scripts"
    shutil.copytree(SCRIPTS_DIR, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest, dest / "lib"


def _seed_state_dir(tmp_path: Path, value: str, *, project_id: str = PID) -> Path:
    """Build a REAL runtime_coordination.db carrying an explicit operator
    override for VNX_CI_GATE_REQUIRED, via the same config_store_db API the
    dashboard uses -- never hand-written SQL."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_dir / "runtime_coordination.db")
    try:
        cs.set_config(conn, project_id, FLAG, value, actor="test", approval_id="test-approval")
    finally:
        conn.close()
    return state_dir


def _run(code: str, env_overrides: dict, cwd: Path, *, scripts_dir: Path, lib_dir: Path) -> dict:
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", code, str(scripts_dir), str(lib_dir)],
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise AssertionError(
            f"subprocess produced non-JSON output: stdout={result.stdout!r} stderr={result.stderr!r}"
        ) from exc


def _probe_canonical_state_dir(fake_home: Path, cwd: Path, *, scripts_dir: Path, lib_dir: Path) -> Path:
    data = _run(_PROBE_CODE, {"HOME": str(fake_home)}, cwd, scripts_dir=scripts_dir, lib_dir=lib_dir)
    return Path(data["VNX_STATE_DIR"])


# ---------------------------------------------------------------------------
# RED: the eiser and the vervuller resolve VNX_CI_GATE_REQUIRED differently,
# because they run in different environments/processes.
# ---------------------------------------------------------------------------


def test_cross_process_flag_divergence_when_vervuller_cannot_see_the_db(tmp_path):
    """Two REAL subprocesses, deliberately different env= AND, for B, an
    isolated copy of scripts/ so its vnx_paths resolution is independent of
    whatever real checkout hosts this test file (see module docstring).

    Seeds an operator override of "0" in a fresh DB -- the OPPOSITE of the
    current registry default "1" (see module docstring for why the default
    must be diverged from, not matched). Process A can reach that DB
    (explicit VNX_STATE_DIR + VNX_PROJECT_ID, real scripts/) and must read
    the override. Process B gets neither env var, plus a FAKE, empty $HOME
    AND the isolated scripts copy -- carrying no ``.git``/marker -- so the
    #1684 canonical-resolver fallback genuinely cannot find ANY
    project_config: not this test's store, not this machine's own real
    ``~/.vnx-data/<project>`` store, and not whatever ``.vnx-data`` may have
    accumulated next to the real checkout this test file itself lives in.
    """
    assert cr.CONFIG_REGISTRY[FLAG].default == "1", (
        "this test's whole premise is an override that diverges from the "
        "registry default -- if the default ever flips back to '0' this "
        "seed must flip too, or the test goes back to being green for the "
        "wrong reason (a store-blind process would then coincidentally "
        "read the same value as the store-connected one)"
    )
    state_dir = _seed_state_dir(tmp_path, "0")

    a_result = _run(
        _PROC_A_CODE,
        {"VNX_STATE_DIR": str(state_dir), "VNX_PROJECT_ID": PID},
        tmp_path,
        scripts_dir=SCRIPTS_DIR, lib_dir=LIB_DIR,
    )

    iso_scripts, iso_lib = _isolated_scripts_copy(tmp_path)
    fake_home = tmp_path / "fake_home_unreachable"
    fake_home.mkdir()
    outside_cwd = tmp_path / "outside_repo_cwd"
    outside_cwd.mkdir()
    b_result = _run(
        _PROC_B_CODE, {"HOME": str(fake_home)}, outside_cwd,
        scripts_dir=iso_scripts, lib_dir=iso_lib,
    )

    # Provenance first: a value that merely LOOKS the same from two
    # different sources is not a passing contract, so assert where each
    # value came from before asserting what it was.
    assert a_result["autowired"] is True, (
        f"process A must have wired the DB and read the operator override; got {a_result}"
    )
    assert b_result["autowired"] is False, (
        f"process B must NOT have found any project_config -- it has no "
        f"VNX_STATE_DIR, a fake empty $HOME, and an isolated (git-less, "
        f"marker-less) scripts copy; got {b_result}"
    )
    assert a_result["flag_value"] is False, f"A must read the DB override (0): {a_result}"
    assert b_result["flag_value"] is True, f"B must fall back to the registry default (1): {b_result}"

    # The named, measurable divergence this dispatch exists to catch: same
    # flag, same instant, two different real values from two different
    # sources -- not a coincidence of matching numbers.
    assert a_result["flag_value"] != b_result["flag_value"], (
        f"expected a genuine cross-process divergence on {FLAG}, got A={a_result} B={b_result}"
    )
    assert "ci_gate" not in a_result["stack"], (
        f"A resolved the operator's off-override and must exclude ci_gate "
        f"from the default review stack: {a_result['stack']}"
    )
    assert b_result["ci_gate_available"] is True, (
        "B, unable to see the operator's off-override, considers ci_gate "
        f"available anyway -- the two processes disagree on whether the "
        f"operator required this gate at all: {b_result}"
    )


# ---------------------------------------------------------------------------
# GREEN: regression guard for PR #1684 -- once the vervuller CAN reach the
# SAME store the eiser read (here: purely via vnx_paths.resolve_paths()'s
# canonical-resolver fallback, the exact code path #1684 added, with no
# VNX_STATE_DIR/VNX_PROJECT_ID at all), both sides must agree.
# ---------------------------------------------------------------------------


def test_cross_process_flag_agreement_when_vervuller_finds_store_via_canonical_resolver(tmp_path):
    """Before #1684, a process with no VNX_STATE_DIR could never see an
    operator override at all, regardless of environment. This pins that it
    now can, via the SAME canonical-resolver path #1684 added -- and that
    when it does, the two processes' reads agree.

    Both the probe and process B run against an isolated scripts copy (see
    module docstring) with an ISOLATED ``.vnx-project-id`` marker planted
    under ``tmp_path`` -- so the resolved store lives entirely under
    ``tmp_path`` (asserted explicitly below) and this test can never write
    an operator override into a real, shared store.
    """
    iso_scripts, iso_lib = _isolated_scripts_copy(tmp_path)
    (tmp_path / ".vnx-project-id").write_text(PID + "\n", encoding="utf-8")
    fake_home = tmp_path / "fake_home_reachable"
    fake_home.mkdir()

    # Ask a subprocess -- under the exact env B will use -- what state dir
    # the canonical resolver computes, so the store gets seeded at that path.
    resolved_state_dir = _probe_canonical_state_dir(
        fake_home, tmp_path, scripts_dir=iso_scripts, lib_dir=iso_lib,
    )
    resolved_tmp_path = tmp_path.resolve()
    assert resolved_state_dir.resolve().is_relative_to(resolved_tmp_path), (
        f"the canonical resolver must land entirely under this test's own "
        f"tmp_path -- a test must never write an operator override into a "
        f"real, shared store; got {resolved_state_dir} (tmp_path={tmp_path})"
    )
    resolved_state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved_state_dir / "runtime_coordination.db")
    try:
        cs.set_config(conn, PID, FLAG, "0", actor="test", approval_id="test-approval")
    finally:
        conn.close()

    a_result = _run(
        _PROC_A_CODE,
        {"VNX_STATE_DIR": str(resolved_state_dir), "VNX_PROJECT_ID": PID},
        tmp_path,
        scripts_dir=SCRIPTS_DIR, lib_dir=LIB_DIR,
    )
    # Process B: no explicit VNX_STATE_DIR/VNX_PROJECT_ID at all -- must find
    # the SAME store purely via the canonical resolver fallback (state dir)
    # plus the isolated .vnx-project-id marker (project_id).
    b_result = _run(
        _PROC_B_CODE, {"HOME": str(fake_home)}, tmp_path,
        scripts_dir=iso_scripts, lib_dir=iso_lib,
    )

    assert a_result["autowired"] is True
    assert b_result["autowired"] is True, (
        f"the #1684 canonical-resolver fallback must find this store with "
        f"no VNX_STATE_DIR at all; got {b_result}"
    )
    assert a_result["flag_value"] is False
    assert b_result["flag_value"] is False, (
        f"once B can see the SAME operator override A saw, it must read "
        f"the same value: {b_result}"
    )
    assert "ci_gate" not in a_result["stack"]
    assert b_result["ci_gate_available"] is False, (
        f"the two sides must agree once the store is reachable from both: "
        f"A={a_result} B={b_result}"
    )
