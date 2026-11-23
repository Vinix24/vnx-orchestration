"""OI-3059: a T0 session without CLAUDE_ROLE=orchestrator says so at the top of the digest.

Runs the real hooks/sessionstart.sh against a tmp state dir. The check runs in the hook process
(scripts/lib/sessionstart_t0_probe.py -> actor_class.derive()), never in the detached builder."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "sessionstart.sh"
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))
import vnx_paths  # noqa: E402

_CLEAR = ("VNX_DATA_DIR", "VNX_STATE_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_PROJECT_ID", "VNX_HOME", "VNX_CANONICAL_ROOT",
          "VNX_PROJECT_ROOT", "CLAUDE_ROLE", "VNX_TERMINAL", "VNX_TERMINAL_ID", "VNX_DISPATCH_ID",
          "VNX_CURRENT_DISPATCH_ID")


def _project(tmp_path: Path) -> Path:
    (tmp_path / "project" / ".vnx").mkdir(parents=True)
    t0 = tmp_path / "project" / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n", encoding="utf-8")
    return t0


def _ctx(hook: Path, cwd: Path, env: dict) -> str:
    r = subprocess.run(["bash", str(hook)], capture_output=True, text=True, cwd=str(cwd), env=env,
                       stdin=subprocess.DEVNULL, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def _env(tmp_path: Path, monkeypatch, **extra: str) -> dict:
    for var in _CLEAR:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "home"))
    Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"]).mkdir(parents=True, exist_ok=True)
    return {**os.environ, **extra}


def test_orchestrator_role_gets_no_warning(tmp_path, monkeypatch):
    ctx = _ctx(HOOK, _project(tmp_path), _env(tmp_path, monkeypatch, CLAUDE_ROLE="orchestrator"))
    assert "T0 ROLE" not in ctx


def test_without_any_role_marker_the_digest_warns_with_the_class(tmp_path, monkeypatch):
    ctx = _ctx(HOOK, _project(tmp_path), _env(tmp_path, monkeypatch))
    assert "T0 ROLE MISSING" in ctx and "classified 'unknown'" in ctx
    assert "CLAUDE_ROLE=orchestrator" in ctx and "claude --resume" in ctx
    lines = ctx.split("\n")
    assert lines[0].startswith("T0 Master Orchestrator Active")
    # The playbook line stays inside the first kilobyte (R4), so the role line follows it.
    assert lines[1].startswith(("Playbook file", "T0 PLAYBOOK SOURCE MISSING"))
    assert lines[2].startswith("T0 ROLE MISSING")


def test_with_the_import_alarm_and_a_missing_role_the_order_is_alarm_header_role(tmp_path, monkeypatch):
    t0 = _project(tmp_path)
    (t0 / "CLAUDE.md").unlink()  # no import: the import alarm applies
    ctx = _ctx(HOOK, t0, _env(tmp_path, monkeypatch))
    alarm, header, role = (ctx.index(m) for m in ("ROLE NOT LOADED", "T0 Master Orchestrator Active", "T0 ROLE MISSING"))
    assert ctx.startswith("ROLE NOT LOADED") and alarm < header < role
    assert ctx[header:].split("\n")[2].startswith("T0 ROLE MISSING")


def test_a_worker_marker_is_warned_with_its_class(tmp_path, monkeypatch):
    ctx = _ctx(HOOK, _project(tmp_path), _env(tmp_path, monkeypatch, CLAUDE_ROLE="backend-developer", VNX_DISPATCH_ID="d1"))
    assert "T0 ROLE MISSING" in ctx and "classified 'worker'" in ctx


def _broken_copy(tmp_path: Path, actor_class_source: str) -> Path:
    """A hook copy whose hook-relative scripts root carries a broken actor_class.py."""
    root = tmp_path / "fabric"
    (root / "hooks").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    hook = root / "hooks" / "sessionstart.sh"
    hook.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    for src in LIB.glob("*.py"):
        if src.name == "sessionstart_t0_probe.py":  # a copy: a symlink would resolve back to the real lib dir
            (root / "scripts" / "lib" / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        elif src.name != "actor_class.py":
            (root / "scripts" / "lib" / src.name).symlink_to(src)
    (root / "scripts" / "lib" / "actor_class.py").write_text(actor_class_source, encoding="utf-8")
    return hook


@pytest.mark.parametrize("source", [
    "def derive():\n    raise RuntimeError('derive exploded')\n",
    "raise ImportError('actor_class is broken')\n",
])
def test_a_failing_derive_says_the_role_could_not_be_checked(tmp_path, monkeypatch, source):
    hook = _broken_copy(tmp_path, source)
    ctx = _ctx(hook, _project(tmp_path), _env(tmp_path, monkeypatch, CLAUDE_ROLE="orchestrator"))
    assert "T0 ROLE UNCHECKED" in ctx and "could not be determined" in ctx
    assert "T0 ROLE MISSING" not in ctx


def test_a_probe_that_cannot_run_says_unchecked_not_nothing(tmp_path, monkeypatch):
    hook = _broken_copy(tmp_path, "T0 = 't0'\ndef derive():\n    return 't0'\n")
    (hook.parent.parent / "scripts" / "lib" / "sessionstart_t0_probe.py").unlink()
    ctx = _ctx(hook, _project(tmp_path), _env(tmp_path, monkeypatch, CLAUDE_ROLE="orchestrator"))
    assert "T0 ROLE UNCHECKED" in ctx
    assert "operator inbox: could not be read" in ctx and "NOT the same as no open questions" in ctx
