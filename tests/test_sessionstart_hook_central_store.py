#!/usr/bin/env python3
"""tests/test_sessionstart_hook_central_store.py — D4: sessionstart.sh reads
the CENTRAL store (ADR-026), not only a repo-local ".vnx-data/state".

Defect: hooks/sessionstart.sh:64-73 (pre-fix) resolved the T0 state dir by
walking up from $PWD checking only ".vnx-data/state" and
".claude/vnx-data/state" — both repo-local candidates. ADR-026
(docs/governance/decisions/ADR-026-per-project-store-with-governance-federation.md)
makes the per-project CENTRAL store (~/.vnx-data/<project_id>/state) the
canonical location. A checkout with no repo-local ".vnx-data" (the normal
case since the central-store cutover) silently found nothing, and the hook
injected a plausible-looking "No open items data" / "No terminal state data"
briefing instead of surfacing that recent_receipts/tracks/pr_queue/
strategic_state were all actually unreachable — a fail-open.

These tests exercise the real hook (bash + the real vnx_paths resolver)
against a project directory that deliberately carries NO repo-local
".vnx-data" anywhere in its tree — the exact defect scenario — and assert:

1. The hook still finds and reads the CENTRAL store (same resolver the
   runtime uses: vnx_paths.resolve_paths()), same OI-852/OI-859 class fix as
   scripts/hooks/path_parity_check.sh and scripts/hooks/build_t0_state_hook.sh.
2. When the store genuinely cannot be resolved/found, the hook says so
   LOUDLY ("VNX STATE STORE NOT FOUND") instead of falling through to the
   ordinary "No open items data" / "No terminal state data" defaults — a
   third state, not a silent zero (niet-gemeten is een derde tak).

Isolation: real per-project central store lookups go through VNX_DATA_HOME
(redirected at a tmp_path) exactly like tests/test_path_parity_hook.py — the
whole point is exercising the same "no repo-local state, no explicit
VNX_STATE_DIR" code path production hits.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "sessionstart.sh"
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import vnx_paths  # noqa: E402

_VNX_ENV_VARS = (
    "VNX_DATA_DIR",
    "VNX_STATE_DIR",
    "VNX_DATA_DIR_EXPLICIT",
    "VNX_DATA_HOME",
    "VNX_PROJECT_ID",
    "VNX_HOME",
    "VNX_BIN",
    "VNX_EXECUTABLE",
    "VNX_CANONICAL_ROOT",
    "VNX_PROJECT_ROOT",
)


def _clean_env(monkeypatch) -> None:
    for var in _VNX_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _make_terminal_project(tmp_path: Path, name: str = "project") -> Path:
    """A T0 terminal dir with NO repo-local .vnx-data anywhere in its tree —
    the exact pre-fix defect scenario."""
    root = tmp_path / name
    (root / ".vnx").mkdir(parents=True)
    t0_dir = root / ".claude" / "terminals" / "T0"
    t0_dir.mkdir(parents=True)
    return t0_dir


def _run_hook(cwd: Path, env: dict) -> dict:
    r = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, cwd=str(cwd), env=env, timeout=10,
    )
    assert r.returncode == 0, f"hook must exit 0: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return json.loads(r.stdout)


class TestCentralStoreResolution:
    def test_finds_central_store_without_repo_local_vnx_data(self, tmp_path, monkeypatch):
        """The exact defect (Klaar-wanneer bullet 3): a project dir with NO
        repo-local .vnx-data/state must still resolve to the central
        per-project store (ADR-026) and read real terminal-state / open-items
        content from it."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)

        isolated_home = tmp_path / "vnx-data-home"
        monkeypatch.setenv("VNX_DATA_HOME", str(isolated_home))

        env = dict(os.environ)
        expected_state_dir = Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"])
        repo_local_fallback = REPO / ".vnx-data" / "state"
        assert expected_state_dir != repo_local_fallback, (
            "central resolver coincides with the repo-local fallback on this "
            "machine -- this test cannot distinguish the fix from the old bug"
        )

        expected_state_dir.mkdir(parents=True, exist_ok=True)
        # Golf 3A fix-forward: this fixture used to write the singular-terminal
        # filename `terminal_state_T1.json` with a top-level `current_task`
        # field. Measured against the real writer
        # (scripts/lib/terminal_state_shadow.py:TERMINAL_STATE_FILENAME) while
        # fixing hooks/sessionstart.sh's own dead read of that same filename:
        # the writer has only ever produced the SINGULAR `terminal_state.json`
        # with a `.terminals.<id>` map (no `current_task` field — the hook
        # reads `.status` / `.last_activity` per terminal). This test's own
        # purpose is central-store PATH resolution (D4), not the terminal-state
        # schema, so the fixture is corrected to match reality rather than the
        # dead-code path both the hook and this fixture used to share.
        (expected_state_dir / "terminal_state.json").write_text(
            json.dumps({"terminals": {"T1": {"status": "busy", "last_activity": "UNIQUE-TASK-MARKER-9f2a"}}}),
            encoding="utf-8",
        )
        (expected_state_dir / "open_items.json").write_text(
            json.dumps({"items": [
                {"status": "open", "severity": "blocker", "id": "OI-1", "title": "UNIQUE-OI-MARKER-7b3c"},
            ]}),
            encoding="utf-8",
        )

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "UNIQUE-TASK-MARKER-9f2a" in ctx, ctx
        assert "UNIQUE-OI-MARKER-7b3c" in ctx, ctx
        assert "VNX STATE STORE NOT FOUND" not in ctx

        # Regression guard: the old repo-local candidates must never have
        # been consulted or created as a side effect of this run.
        assert not (t0_dir.parent.parent.parent / ".vnx-data").exists()

    def test_loud_warning_when_store_not_found(self, tmp_path, monkeypatch):
        """Not-found is a third state, not a silent zero: when the resolved
        central path does not exist on disk (no dispatch/receipt-processor
        has ever run for this project), the hook must say so LOUDLY, never
        fall through to the ordinary "No open items data" / "No terminal
        state data" defaults — those must read as "measured: empty", not
        "unmeasured"."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)

        isolated_home = tmp_path / "vnx-data-home-empty"
        monkeypatch.setenv("VNX_DATA_HOME", str(isolated_home))
        env = dict(os.environ)

        expected_state_dir = Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"])
        assert not expected_state_dir.exists(), "precondition: central dir must not pre-exist"

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "VNX STATE STORE NOT FOUND" in ctx, ctx
        assert "UNMEASURED" in ctx, ctx
        assert "No open items data" not in ctx
        assert "No terminal state data" not in ctx

    def test_hook_is_valid_bash(self):
        result = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
