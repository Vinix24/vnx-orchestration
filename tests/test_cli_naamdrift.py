"""tests/test_cli_naamdrift.py — pin cross-entrance name-drift fixes (OI-1084, OI-1060).

The two VNX entrances (`bin/vnx` in the fabric repo and the pip-installed
`vnx_cli`) disagreed on the planning layer's name: `vnx horizon` worked in
the pip CLI but was refused by the fabric mode gate, while `vnx objective`
was the only spelling the gate let through. A dispatch instruction that
prescribed `vnx horizon reconcile` failed in the fabric repo on the mode
port, and the error read like a permission problem rather than a naming one.

These tests pin the three fixes:

1. ``horizon`` is in the shared operator/starter tier, so the fabric mode
   gate lets the canonical spelling through (``objective`` stays as an
   alias). Both entrances accept ``horizon``.
2. A refused name that exists as a sub-verb or alias now names the working
   form in the error, in both entrances:
   - ``plan-gate`` -> ``vnx horizon plan-gate``
   - ``dispatch`` <-> ``dispatch-agent`` (cross-entrance alias)
3. The pip-CLI invalid-choice message carries the same hint.

Red-against-main measurement: every test fails on main (horizon is absent
from every tier; the suggest helpers and the pip-CLI suggestion parser do
not exist) and passes on this branch. The new-symbol imports are done
inside each test so the collection itself does not abort — on main the
tests fail individually with ImportError/AttributeError or assertion
failure, which is the red signal.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
for _p in (str(REPO_ROOT), str(_LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vnx_mode  # noqa: E402


def _pip_main():
    """Import the pip-CLI main lazily so a missing helper surfaces as a
    per-test failure, not a collection error."""
    from vnx_cli.main import main as vnx_main
    return vnx_main


def _pip_suggest(command: str) -> str:
    from vnx_cli.main import _suggest_working_form
    return _suggest_working_form(command)


# ---------------------------------------------------------------------------
# 1. horizon is in the shared tier (both entrances)
# ---------------------------------------------------------------------------

def test_horizon_is_in_starter_operator_tier():
    """The canonical spelling must be gate-allowed in both modes, not just
    operator. On main `horizon` is absent from every tier and refused."""
    assert "horizon" in vnx_mode.TIER_STARTER_OPERATOR


def test_horizon_passes_mode_gate_in_both_modes():
    for mode in (vnx_mode.VNXMode.STARTER, vnx_mode.VNXMode.OPERATOR):
        # Must not raise — horizon is gate-allowed.
        vnx_mode.check_command_allowed("horizon", mode)


def test_objective_alias_still_gate_allowed():
    """The backward-compat alias must not have been broken by adding horizon."""
    assert "objective" in vnx_mode.TIER_STARTER_OPERATOR
    for mode in (vnx_mode.VNXMode.STARTER, vnx_mode.VNXMode.OPERATOR):
        vnx_mode.check_command_allowed("objective", mode)


# ---------------------------------------------------------------------------
# 2. ModeGateError names the working form for alias/sub-verb names (fabric)
# ---------------------------------------------------------------------------

def test_mode_gate_plan_gate_names_working_form():
    """`plan-gate` is a sub-verb of `vnx horizon`, not a mode problem. The
    error must point at `vnx horizon plan-gate` rather than only saying
    'requires a different mode'."""
    with pytest.raises(vnx_mode.ModeGateError) as exc:
        vnx_mode.check_command_allowed("plan-gate", vnx_mode.VNXMode.OPERATOR)
    msg = str(exc.value)
    assert "plan-gate" in msg
    assert "vnx horizon plan-gate" in msg


def test_mode_gate_dispatch_names_cross_entrance_alias():
    """`dispatch` (fabric) is `dispatch-agent` (pip). The suggestion helper
    must name the other spelling."""
    suggestion = vnx_mode._suggest_working_form("dispatch")
    assert "dispatch-agent" in suggestion


def test_mode_gate_dispatch_agent_names_cross_entrance_alias():
    suggestion = vnx_mode._suggest_working_form("dispatch-agent")
    assert "dispatch" in suggestion


def test_mode_gate_unknown_name_gives_no_hint():
    """A genuinely unknown name gets no false suggestion."""
    assert vnx_mode._suggest_working_form("totallybogus") == ""


# ---------------------------------------------------------------------------
# 3. pip-CLI invalid-choice message carries the same hint
# ---------------------------------------------------------------------------

def _run_pip(monkeypatch, capsys, argv):
    vnx_main = _pip_main()
    monkeypatch.setattr(sys, "argv", ["vnx", *argv])
    with pytest.raises(SystemExit) as exc:
        vnx_main()
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


def test_pip_plan_gate_error_names_working_form(monkeypatch, capsys):
    rc, _, err = _run_pip(monkeypatch, capsys, ["plan-gate"])
    assert rc == 2
    assert "invalid choice" in err
    assert "vnx horizon plan-gate" in err


def test_pip_dispatch_error_names_cross_entrance_alias(monkeypatch, capsys):
    rc, _, err = _run_pip(monkeypatch, capsys, ["dispatch"])
    assert rc == 2
    assert "invalid choice" in err
    assert "dispatch-agent" in err


def test_pip_bogus_command_no_false_hint(monkeypatch, capsys):
    rc, _, err = _run_pip(monkeypatch, capsys, ["totallybogus"])
    assert rc == 2
    assert "invalid choice" in err
    # No drift hint attached for an unknown name.
    assert "sub-verb of" not in err
    assert "spelled" not in err


def test_pip_suggest_matches_mode_suggest():
    """Both entrances compute the same hint for the same name (SSOT parity)."""
    for name in ("plan-gate", "dispatch", "dispatch-agent"):
        assert _pip_suggest(name) == vnx_mode._suggest_working_form(name)


def test_pip_horizon_and_objective_both_accepted(monkeypatch, capsys):
    """Both spellings must parse in the pip CLI (horizon canonical, objective
    alias). Parity assertion guards regressions in either direction."""
    for cmd in ("horizon", "objective"):
        rc, out, _ = _run_pip(monkeypatch, capsys, [cmd, "--help"])
        assert rc == 0
        assert "list" in out  # the verb surface is rendered


# ---------------------------------------------------------------------------
# 4. bin/vnx delegates `horizon` to the planning engine (fabric entrance)
# ---------------------------------------------------------------------------

def test_bin_vnx_horizon_help_works():
    """`./bin/vnx horizon --help` must return a working help text, not the
    mode-gate refusal. This is the exact faalscenario from OI-1084: a
    dispatch prescribing `vnx horizon ...` failed in the fabric repo."""
    result = subprocess.run(
        [str(REPO_ROOT / "bin" / "vnx"), "horizon", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    # The refusal message must be gone.
    assert "requires a different mode" not in result.stdout
    assert "requires a different mode" not in result.stderr
    # A real planning verb is rendered.
    assert "list" in result.stdout


def test_bin_vnx_objective_alias_still_works():
    """The `objective` alias must still work through bin/vnx after adding the
    horizon branch."""
    result = subprocess.run(
        [str(REPO_ROOT / "bin" / "vnx"), "objective", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "list" in result.stdout


def test_bin_vnx_plan_gate_error_names_working_form():
    """`./bin/vnx plan-gate` (no sub-verb) is refused and the refusal must
    name `vnx horizon plan-gate` (OI-1060: a name that exists as a sub-verb
    but not as a top-level command). Covers both refusal paths: the mode gate
    (operator mode) and the unknown-command fallback (pre-init)."""
    result = subprocess.run(
        [str(REPO_ROOT / "bin" / "vnx"), "plan-gate"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "plan-gate" in combined
    assert "vnx horizon plan-gate" in combined
