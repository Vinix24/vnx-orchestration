"""test_dispatch_flags.py — single-source routing predicate (item E helper).

Locks the truth table and proves the bash binding (vnx_dispatch_flags.sh) agrees with the
python source (dispatch_flags.py) for every case, so the default + VNX_DISPATCH_LEGACY rollback
never drift between languages.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import dispatch_flags

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FLAGS_SH = _REPO_ROOT / "scripts" / "lib" / "vnx_dispatch_flags.sh"

# (env-overlay, expected_enabled). Default is OFF pre-flip (dispatch_flags._DEFAULT_ENABLED).
CASES = [
    ({}, False),                                        # unset -> default OFF
    ({"VNX_SINGLE_ENTRY_DISPATCH": ""}, False),          # empty -> default OFF
    ({"VNX_SINGLE_ENTRY_DISPATCH": "0"}, False),         # explicit 0 -> OFF
    ({"VNX_SINGLE_ENTRY_DISPATCH": "1"}, True),          # 1 -> ON
    ({"VNX_SINGLE_ENTRY_DISPATCH": "2"}, True),          # widened: any non-0 truthy -> ON
    ({"VNX_SINGLE_ENTRY_DISPATCH": "1", "VNX_DISPATCH_LEGACY": "1"}, False),  # rollback wins
    ({"VNX_SINGLE_ENTRY_DISPATCH": "1", "VNX_DISPATCH_LEGACY": "0"}, True),
    ({"VNX_DISPATCH_LEGACY": "1"}, False),               # rollback wins over default
    ({"VNX_SINGLE_ENTRY_DISPATCH": "0", "VNX_DISPATCH_LEGACY": "1"}, False),
]


def test_default_is_off_pre_flip():
    assert dispatch_flags.default_enabled() is False
    assert dispatch_flags.single_entry_enabled({}) is False


def test_python_truth_table():
    for env, expected in CASES:
        assert dispatch_flags.single_entry_enabled(env) is expected, f"py mismatch for {env}"


def _bash_enabled(env_overlay: dict) -> bool:
    # Run only the bash binding with a controlled environment; exit 0 == enabled.
    script = f'source "{_FLAGS_SH}"; vnx_single_entry_enabled'
    full_env = {"PATH": __import__("os").environ.get("PATH", ""),
                "VNX_HOME": str(_REPO_ROOT)}
    full_env.update({k: v for k, v in env_overlay.items()})
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full_env)
    return r.returncode == 0


def test_bash_binding_matches_python():
    for env, expected in CASES:
        assert _bash_enabled(env) is expected, f"bash mismatch for {env}"
