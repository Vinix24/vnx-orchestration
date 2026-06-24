"""Door-flip D1 routing tests (ADR-024 routing-split / ADR-025 deprecation).

These exercise the REAL bash via subprocess (no reimplementation):

  * _d_valid_dispatch_id   — the centralized id-safety guard
  * _d_is_staged_form      — staged (door) vs raw (legacy) classifier
  * cmd_dispatch routing   — door-ON staged -> dispatch_cli (door); door-ON raw -> legacy
                             tmux/subprocess delivery (NOT the bridge) + a deprecation warning;
                             rollback/off -> legacy, no warning, staged-bundle hint on not-found
  * dispatch_deliver.sh    — provider propagation to the door bridge (G1) + default-OFF regression
  * dispatch_bridge.py     — claude_code domain string canonicalizes to the claude lane (O1)

D1 keeps the default OFF (the flip is D2), so these drive the door via an explicit
VNX_SINGLE_ENTRY_DISPATCH=1 opt-in.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DISPATCH_SH = REPO_ROOT / "scripts" / "commands" / "dispatch.sh"
DELIVER_SH = REPO_ROOT / "scripts" / "lib" / "dispatch_deliver.sh"


# --------------------------------------------------------------------------- #
# _d_is_staged_form / _d_valid_dispatch_id — direct predicate unit tests
# --------------------------------------------------------------------------- #

def _predicate_preamble(dispatch_dir: Path) -> str:
    return (
        "set -euo pipefail\n"
        "log() { :; }\n"
        "err() { :; }\n"
        f"export VNX_DISPATCH_DIR='{dispatch_dir}'\n"
        f"source '{DISPATCH_SH}'\n"
    )


def _run_predicate(dispatch_dir: Path, fn: str, *args: str) -> int:
    """Source dispatch.sh and return the exit code of `fn args` (0/1)."""
    quoted = " ".join(f"'{a}'" for a in args)
    script = _predicate_preamble(dispatch_dir) + f"{fn} {quoted}\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode


def _make_bundle(dispatch_dir: Path, pending_id: str) -> None:
    bundle = dispatch_dir / "pending" / pending_id
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "dispatch-spec.json").write_text("{}", encoding="utf-8")


@pytest.fixture()
def dispatch_dir(tmp_path: Path) -> Path:
    d = tmp_path / "dispatches"
    (d / "pending").mkdir(parents=True)
    return d


# ---- _d_valid_dispatch_id -------------------------------------------------- #

@pytest.mark.parametrize("arg,expected", [
    ("20260624-foo-A", 0),       # canonical id -> valid
    ("foo.md", 0),               # .md is allowed by the regex (bundle-check disambiguates later)
    ("a", 0),                    # single safe char
    ("../foo", 1),               # path traversal -> rejected (slash)
    ("..", 1),                   # bare .. -> rejected
    ("foo/bar", 1),              # slash -> rejected
    ("foo..bar", 1),             # embedded .. -> rejected
    (".hidden", 1),              # leading '.' is not [A-Za-z0-9]
    ("", 1),                     # empty -> rejected
])
def test_valid_dispatch_id(dispatch_dir, arg, expected):
    assert _run_predicate(dispatch_dir, "_d_valid_dispatch_id", arg) == expected


# ---- _d_is_staged_form (0 = door/staged, 1 = legacy/raw) ------------------- #

def test_staged_spec_file_is_door(dispatch_dir):
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "--spec-file", "/abs/x.json") == 0


def test_staged_force_release_lock_is_door(dispatch_dir):
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "--force-release-lock") == 0


def test_help_is_door(dispatch_dir):
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "--help") == 0
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "-h") == 0


def test_no_positional_is_door(dispatch_dir):
    # No file given -> door owns the "requires" messaging.
    assert _run_predicate(dispatch_dir, "_d_is_staged_form") == 0
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "--dry-run") == 0


def test_pending_id_with_bundle_is_door(dispatch_dir):
    _make_bundle(dispatch_dir, "20260624-demo-A")
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "20260624-demo-A") == 0


def test_pending_id_ending_md_with_bundle_is_door(dispatch_dir):
    # A pending id may legally end in .md; the bundle check wins over the .md heuristic.
    _make_bundle(dispatch_dir, "weird.md")
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "weird.md") == 0


def test_raw_md_path_is_legacy(dispatch_dir):
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "/path/to/x.md") == 1
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "x.md") == 1


def test_bare_slug_without_bundle_is_legacy(dispatch_dir):
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "no-such-bundle") == 1


def test_collision_bundle_wins_under_door(dispatch_dir):
    # Both a bundle dir `foo` and a raw file `foo.md`: the bare slug `foo` has a bundle -> door.
    _make_bundle(dispatch_dir, "foo")
    (dispatch_dir / "pending" / "foo.md").write_text("[[TARGET:T1]]\n", encoding="utf-8")
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "foo") == 0


def test_overrides_on_pending_id_still_door(dispatch_dir):
    # value-taking flags are skipped; the bundle slug is still found -> door (then the door
    # itself rejects the override with a clear error — covered by the integration test).
    _make_bundle(dispatch_dir, "20260624-demo-A")
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "20260624-demo-A", "--terminal", "T2") == 0


def test_end_of_options_marks_next_positional(dispatch_dir):
    # `-- foo.md` -> foo.md is positional -> .md -> legacy.
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "--", "foo.md") == 1


def test_value_flag_missing_value_does_not_trip_set_u(dispatch_dir):
    # `--terminal` with no value at the end must not crash under set -u; no positional -> door.
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "--terminal") == 0


def test_traversal_token_is_legacy(dispatch_dir):
    # `..` / `../x` never reach a bundle path-join; classify as legacy.
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "..") == 1
    assert _run_predicate(dispatch_dir, "_d_is_staged_form", "../x") == 1


# --------------------------------------------------------------------------- #
# cmd_dispatch routing — door vs legacy delivery (stub-home pattern)
# --------------------------------------------------------------------------- #

_DOOR_STUB = (
    "import os, sys\n"
    'open(os.environ["VNX_DOOR_MARKER"], "w").write("\\0".join(sys.argv[1:]))\n'
)
_LANE_STUB = """#!/usr/bin/env python3
import os, sys, json
with open(os.environ["VNX_LANE_MARKER"], "w") as f:
    json.dump({{"lane": "{lane}", "argv": sys.argv[1:]}}, f)
sys.exit(0)
"""
_BRIDGE_STUB = (
    "#!/usr/bin/env python3\n"
    "import os, sys\n"
    'open(os.environ["VNX_BRIDGE_MARKER"], "w").write("hit")\n'
    "sys.exit(0)\n"
)


def _make_home(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    lib = home / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "dispatch_cli.py").write_text(_DOOR_STUB, encoding="utf-8")
    (lib / "tmux_interactive_dispatch.py").write_text(_LANE_STUB.format(lane="tmux"), encoding="utf-8")
    (lib / "subprocess_dispatch.py").write_text(_LANE_STUB.format(lane="subprocess"), encoding="utf-8")
    (lib / "dispatch_bridge.py").write_text(_BRIDGE_STUB, encoding="utf-8")
    dispatch_dir = tmp_path / "dispatches"
    (dispatch_dir / "pending").mkdir(parents=True)
    return {
        "home": home,
        "dispatch_dir": dispatch_dir,
        "door_marker": tmp_path / "door.marker",
        "lane_marker": tmp_path / "lane.marker",
        "bridge_marker": tmp_path / "bridge.marker",
    }


def _run_cmd(env_paths: dict, *args: str, flag: str | None, legacy: str | None = None) -> subprocess.CompletedProcess:
    quoted = " ".join(f"'{a}'" for a in args)
    flag_line = f"export VNX_SINGLE_ENTRY_DISPATCH='{flag}'\n" if flag is not None else "unset VNX_SINGLE_ENTRY_DISPATCH\n"
    legacy_line = f"export VNX_DISPATCH_LEGACY='{legacy}'\n" if legacy is not None else ""
    script = f"""
set -u
log() {{ printf '%s\\n' "$*" >&2; }}
err() {{ printf 'ERR %s\\n' "$*" >&2; }}
export VNX_HOME='{env_paths["home"]}'
export VNX_DATA_DIR='{env_paths["home"]}/data'
export VNX_STATE_DIR='{env_paths["home"]}/data/state'
export VNX_DISPATCH_DIR='{env_paths["dispatch_dir"]}'
export VNX_DOOR_MARKER='{env_paths["door_marker"]}'
export VNX_LANE_MARKER='{env_paths["lane_marker"]}'
export VNX_BRIDGE_MARKER='{env_paths["bridge_marker"]}'
{flag_line}{legacy_line}source '{DISPATCH_SH}'
cmd_dispatch {quoted}
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


def _write_raw(env_paths: dict, name: str = "demo.md") -> Path:
    f = env_paths["dispatch_dir"] / "pending" / name
    f.write_text("[[TARGET:T1]]\nRole: backend-developer\nGate: G1\n\nDo it.\n", encoding="utf-8")
    return f


def test_door_on_staged_pending_id_hits_door(tmp_path):
    e = _make_home(tmp_path)
    _make_bundle_home = e["dispatch_dir"] / "pending" / "20260624-demo-A"
    _make_bundle_home.mkdir(parents=True)
    (_make_bundle_home / "dispatch-spec.json").write_text("{}", encoding="utf-8")
    r = _run_cmd(e, "20260624-demo-A", flag="1")
    assert r.returncode == 0, r.stderr
    assert e["door_marker"].exists(), "door (dispatch_cli.py) not invoked for a staged pending-id"
    assert "--spec-file" in e["door_marker"].read_text().split("\0")
    assert not e["lane_marker"].exists()
    assert not e["bridge_marker"].exists()


def test_door_on_raw_md_goes_legacy_not_bridge(tmp_path):
    e = _make_home(tmp_path)
    raw = _write_raw(e)
    r = _run_cmd(e, str(raw), flag="1")
    assert r.returncode == 0, r.stderr
    # POSITIVE: the legacy tmux lane IS invoked; the door AND the bridge are NOT.
    assert e["lane_marker"].exists(), "legacy delivery lane not invoked for raw .md under door-ON"
    assert json.loads(e["lane_marker"].read_text())["lane"] == "tmux"
    assert not e["door_marker"].exists(), "door wrongly invoked for raw .md"
    assert not e["bridge_marker"].exists(), "bridge wrongly invoked for raw .md (Option X1 violated)"
    assert "DEPRECATED" in r.stderr, "deprecation warning not emitted under door-ON + raw"


def test_door_on_raw_md_adapter_subprocess_honored(tmp_path):
    e = _make_home(tmp_path)
    raw = _write_raw(e)
    r = _run_cmd(e, str(raw), "--adapter", "subprocess", flag="1")
    assert r.returncode == 0, r.stderr
    # Lane precedence preserved on the raw form: --adapter subprocess -> subprocess lane.
    assert json.loads(e["lane_marker"].read_text())["lane"] == "subprocess"
    assert not e["bridge_marker"].exists()


def test_rollback_raw_md_legacy_no_warning(tmp_path):
    # Door off via explicit rollback (post-flip the default is ON, so we opt out explicitly):
    # raw .md -> legacy, and NO deprecation warning (the raw form is the sanctioned path here).
    e = _make_home(tmp_path)
    raw = _write_raw(e)
    r = _run_cmd(e, str(raw), flag=None, legacy="1")
    assert r.returncode == 0, r.stderr
    assert e["lane_marker"].exists()
    assert "DEPRECATED" not in r.stderr, "deprecation warning must not fire when the door is off"
    assert not e["door_marker"].exists()


def test_default_on_raw_md_legacy_with_warning(tmp_path):
    # Post-flip: unset VNX_SINGLE_ENTRY_DISPATCH resolves to the door (default ON). A raw .md still
    # falls through to legacy delivery, now WITH the deprecation warning.
    e = _make_home(tmp_path)
    raw = _write_raw(e)
    r = _run_cmd(e, str(raw), flag=None)  # unset -> default ON (post-flip)
    assert r.returncode == 0, r.stderr
    assert e["lane_marker"].exists()
    assert not e["door_marker"].exists()
    assert not e["bridge_marker"].exists()
    assert "DEPRECATED" in r.stderr, "deprecation warning must fire under the door default + raw"


def test_rollback_staged_pending_id_legacy_with_hint(tmp_path):
    e = _make_home(tmp_path)
    bundle = e["dispatch_dir"] / "pending" / "20260624-demo-A"
    bundle.mkdir(parents=True)
    (bundle / "dispatch-spec.json").write_text("{}", encoding="utf-8")
    # Door enabled but rollback wins -> legacy parse; pending-id isn't a legacy file -> not found + hint.
    r = _run_cmd(e, "20260624-demo-A", flag="1", legacy="1")
    assert r.returncode != 0
    assert not e["door_marker"].exists(), "door invoked despite VNX_DISPATCH_LEGACY=1"
    assert "staged dispatch bundle" in r.stderr, "staged-bundle hint missing on rollback + pending-id"
    assert "DEPRECATED" not in r.stderr, "no deprecation warning under rollback"


def test_rollback_spec_file_legacy_no_door(tmp_path):
    e = _make_home(tmp_path)
    # The second canonical staged form under rollback: --spec-file is a door concept; legacy can't
    # serve it -> the door is not invoked and it errors on the legacy lane.
    r = _run_cmd(e, "--spec-file", "/tmp/x.json", flag="1", legacy="1")
    assert not e["door_marker"].exists(), "door invoked despite rollback for --spec-file"


def test_door_on_help_renders_without_command_substitution(tmp_path):
    # Regression (kimi-gate): the door --help here-doc (cat <<HELP, unquoted) must not contain
    # backticks/$(...) — bash would command-substitute them when --help is shown, breaking the
    # help and opening an injection surface. Assert a clean render under door-ON.
    e = _make_home(tmp_path)
    r = _run_cmd(e, "--help", flag="1")
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert "Single-entry gate" in out
    assert "DEPRECATED" in out
    for bad in ("command not found", "syntax error", "No such file"):
        assert bad not in out.lower(), f"help triggered shell evaluation: {bad!r} in output"


def test_door_on_pending_id_with_override_clear_error(tmp_path):
    e = _make_home(tmp_path)
    bundle = e["dispatch_dir"] / "pending" / "20260624-demo-A"
    bundle.mkdir(parents=True)
    (bundle / "dispatch-spec.json").write_text("{}", encoding="utf-8")
    r = _run_cmd(e, "20260624-demo-A", "--terminal", "T2", flag="1")
    assert r.returncode != 0
    assert "legacy raw-file override" in r.stderr, "clear-error for staged-id + override missing"
    assert not e["door_marker"].exists()


# --------------------------------------------------------------------------- #
# dispatch_deliver.sh — provider propagation (G1) + default-OFF regression
# --------------------------------------------------------------------------- #

def _run_deliver(tmp_path: Path, *call_args: str, flag: str | None) -> subprocess.CompletedProcess:
    """Stub python3 to capture argv; source dispatch_deliver.sh; call _ddt_subprocess_delivery."""
    capture = tmp_path / "argv.txt"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    real_py = shutil.which("python3") or "/usr/bin/python3"
    py = stub_dir / "python3"
    # Delegate the flag predicate's `python3 -c ...` to the REAL interpreter so
    # vnx_single_entry_enabled resolves correctly; capture argv only for the delivery call.
    py.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = "-c" ]; then exec "{real_py}" "$@"; fi\n'
        f'printf "%s\\n" "$@" > "{capture}"\nexit 0\n'
    )
    py.chmod(0o755)
    (tmp_path / "dispatch.md").write_text("[[TARGET:T2]]\n", encoding="utf-8")
    flag_line = f'export VNX_SINGLE_ENTRY_DISPATCH="{flag}"\n' if flag is not None else "unset VNX_SINGLE_ENTRY_DISPATCH\n"
    quoted = " ".join(f'"{a}"' for a in call_args)
    script = (
        f'export PATH="{stub_dir}:$PATH"\n'
        f'export VNX_DIR="{REPO_ROOT}"\n'
        "log() { :; }\n"
        "log_structured_failure() { :; }\n"
        "rc_release_on_failure() { :; }\n"
        "release_terminal_claim() { :; }\n"
        f"{flag_line}"
        f'source "{DELIVER_SH}"\n'
        f"_ddt_subprocess_delivery {quoted}\n"
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ})
    proc_argv = capture.read_text().splitlines() if capture.exists() else []
    return proc, proc_argv


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_deliver_door_on_forwards_provider_to_bridge(tmp_path):
    proc, argv = _run_deliver(
        tmp_path, "T2", "d-1", "PROMPT", "sonnet", f"{tmp_path}/dispatch.md", "test-engineer", "codex_cli",
        flag="1",
    )
    assert proc.returncode == 0, proc.stderr
    assert any("dispatch_bridge.py" in a for a in argv), f"bridge not invoked under door-ON: {argv}"
    assert "--provider" in argv, f"--provider not forwarded to the bridge: {argv}"
    assert argv[argv.index("--provider") + 1] == "codex_cli"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_deliver_optout_legacy_unbroken_by_7th_arg(tmp_path):
    # Explicit opt-out (VNX_SINGLE_ENTRY_DISPATCH=0, post-flip the default is ON): the 7-arg call
    # still routes to subprocess_dispatch.py (legacy), proving the signature change is backward-safe.
    proc, argv = _run_deliver(
        tmp_path, "T2", "d-2", "PROMPT", "sonnet", f"{tmp_path}/dispatch.md", "test-engineer", "claude_code",
        flag="0",
    )
    assert proc.returncode == 0, proc.stderr
    assert any("subprocess_dispatch.py" in a for a in argv), f"legacy lane broken by 7th arg: {argv}"
    assert not any("dispatch_bridge.py" in a for a in argv)


# --------------------------------------------------------------------------- #
# dispatch_bridge.py — claude_code domain string canonicalizes (O1)
# --------------------------------------------------------------------------- #

def test_claude_code_canonicalizes_to_claude():
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    import dispatch_bridge
    # The real emitted domain strings from get_terminal_provider / vnx_init.
    assert dispatch_bridge._canonical_provider("claude_code").value == "claude"
    assert dispatch_bridge._canonical_provider("codex_cli").value == "codex"
    assert dispatch_bridge._canonical_provider("gemini_cli").value == "gemini"


# --------------------------------------------------------------------------- #
# Receipt-lane: the staged spec's provider is the lane determinant
# (dispatch_cli.py: is_claude_lane = spec.provider == Provider.CLAUDE -> tmux-spawn; else provider lane)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("emitted,canonical", [
    ("claude_code", "claude"),   # -> claude_tmux_subscription lane
    ("codex_cli", "codex"),      # -> provider lane (NOT claude tmux)
    ("gemini_cli", "gemini"),    # -> provider lane
])
def test_staged_spec_carries_canonical_provider_lane_determinant(tmp_path, emitted, canonical):
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    import dispatch_bridge
    spec_file = dispatch_bridge.stage_spec_bundle(
        instruction_text="do it",
        dispatch_id="20260624-lane-A",
        role="backend-developer",
        target_slot="T1",
        provider=emitted,
        data_dir=tmp_path,
    )
    payload = json.loads(spec_file.read_text(encoding="utf-8"))
    assert payload["provider"] == canonical, (
        f"provider '{emitted}' must canonicalize to '{canonical}' in the spec (the lane determinant)"
    )


def test_deliver_via_door_routes_to_bridge_under_default_on(tmp_path, monkeypatch):
    # Post-flip default ON: deliver_via_door routes the in-process callers (incl.
    # vnx_cli/commands/dispatch_agent.py) through the bridge, not the legacy callable.
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    import dispatch_bridge
    monkeypatch.delenv("VNX_SINGLE_ENTRY_DISPATCH", raising=False)  # unset -> default ON
    monkeypatch.delenv("VNX_DISPATCH_LEGACY", raising=False)
    calls = {"bridge": 0, "legacy": 0}
    monkeypatch.setattr(dispatch_bridge, "bridge_dispatch", lambda **kw: calls.__setitem__("bridge", calls["bridge"] + 1) or 0)
    dispatch_bridge.deliver_via_door(
        lambda: calls.__setitem__("legacy", calls["legacy"] + 1) or True,
        instruction_text="x", dispatch_id="20260624-agent-A", target_slot="T1", role="agent",
    )
    assert calls["bridge"] == 1 and calls["legacy"] == 0, f"default-ON must route via the bridge: {calls}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
