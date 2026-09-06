"""tests/test_dispatch_track_producer.py — OI-1639: nul van 1114 gestagede specs draagt
een track_id: bouw de producent.

Three surfaces measured broken (05/06-09-2026) and fixed here:

1. The door (dispatch_cli.py) had no ``--track`` flag: a track_id could only ever reach
   a spec by being staged into the JSON already (dispatch_bridge.stage_spec_bundle's own
   ``track_id`` param, OI-1632). ``--track`` now stamps ``spec.track_id`` BEFORE
   ``validate()``/``build_runtime_snapshot()``, so the pre-existing TL-D1 machinery
   (``_check_track_link_verdict``, ``_persist_track_id``) sees it exactly as if the
   staged spec had carried the value — no second code path, no second column.
2. There was no CLI to STAGE ONLY (write a dispatch-spec.json and stop) — an operator
   had to call ``dispatch_bridge.stage_spec_bundle`` from Python by hand.
   ``dispatch_bridge.py stage ...`` (and ``vnx dispatch stage ...``) now does exactly
   that: stage, print the spec path, never fire.
3. ``scripts/commands/dispatch.sh``'s single-entry wrapper rejected any door flag it did
   not itself enumerate — measured: ``--override-stop-conditions``, already known to
   ``dispatch_cli.py``, failed through the wrapper with "unknown flag". Unknown
   ``--flag``s are now collected and forwarded verbatim; the door is the only place
   that still decides whether it recognizes them.

RED on origin/main:
  (1) ``--track`` is an unrecognized argparse flag on ``dispatch_cli.main`` (SystemExit(2));
  (2) ``dispatch_bridge.main(["stage", ...])`` has no "stage" first-token handling — it
      falls into the legacy stage+fire parser, which treats "stage" as a value/flag
      soup and errors (missing required ``--dispatch-id``/``--terminal``, or SystemExit);
  (3) ``dispatch.sh``'s ``_d_single_entry_dispatch`` hits its ``-*)`` case and errors
      ``"unknown flag: --override-stop-conditions"``.
GREEN on the fix: all three behave as described above.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_bridge  # noqa: E402
from dispatch_cli import main as dispatch_cli_main  # noqa: E402


def _make_tracks_db(state_dir: Path, *, tracks: "dict[str, str] | None" = None) -> Path:
    """Minimal runtime_coordination.db with just a `tracks` stand-in for
    _check_track_link_verdict/_lookup_track_phase — mirrors the identically-named,
    independently-kept helper in test_dispatch_cli.py / test_oi1632_track_overdracht.py
    (same rationale there: no cross-file fixture import)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tracks (
            track_id TEXT NOT NULL PRIMARY KEY,
            phase TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        )
        """
    )
    for tid, phase in (tracks or {}).items():
        conn.execute(
            "INSERT INTO tracks (track_id, phase, project_id) VALUES (?, ?, 'vnx-dev')",
            (tid, phase),
        )
    conn.commit()
    conn.close()
    return db_path


def _stage_bundle(data_dir: Path, dispatch_id: str, *, extra: "dict | None" = None) -> Path:
    """Write a minimal staged dispatch-spec.json at the layout the door's
    _authority_from_spec_path recognizes (<data_dir>/dispatches/pending/<id>/...)."""
    bundle_dir = data_dir / "dispatches" / "pending" / dispatch_id
    bundle_dir.mkdir(parents=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec_dict = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": dispatch_id,
        "instruction_file": str(instruction),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
        "track_id": None,
        # Door tests assert route/track state, not lane behavior; tmp_path is not a real
        # git repo, so pin the tmux lane exactly like test_dispatch_cli.py's own e2e tests.
        "force_tmux": True,
        "force_tmux_reason": "test fixture pins the tmux lane",
    }
    spec_dict.update(extra or {})
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec_dict), encoding="utf-8")
    return spec_file


# ---------------------------------------------------------------------------
# 1. The door: --track stamps spec.track_id before validate()/the TL-D1 check.
# ---------------------------------------------------------------------------

class TestDoorTrackFlag:
    def test_track_flag_stamps_spec_and_shows_in_dry_run(self, tmp_path, monkeypatch, capsys):
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        _make_tracks_db(data_dir / "state", tracks={"oi1639-track": "active"})
        spec_file = _stage_bundle(data_dir, "20260906-door-track-dry")

        rc = dispatch_cli_main(
            ["--spec-file", str(spec_file), "--dry-run", "--track", "oi1639-track"]
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "track_id:     oi1639-track" in out, (
            f"--track must land on the spec and print in dry-run output; got:\n{out}"
        )
        # A dry-run never reaches _persist_track_id (real-fire only) — the bundle on
        # disk is untouched; --track is a per-fire override, not a rewrite of the spec.
        on_disk = json.loads(spec_file.read_text(encoding="utf-8"))
        assert on_disk["track_id"] is None

    def test_track_flag_nonexistent_track_rejects_naming_the_track(
        self, tmp_path, monkeypatch, capsys
    ):
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        _make_tracks_db(data_dir / "state", tracks={"some-other-track": "active"})
        spec_file = _stage_bundle(data_dir, "20260906-door-track-bad")

        with patch("dispatch_cli._execute_claude") as mock_execute:
            rc = dispatch_cli_main(
                ["--spec-file", str(spec_file), "--track", "does-not-exist-oi1639"]
            )

        assert rc == 1
        mock_execute.assert_not_called()
        err = capsys.readouterr().err
        assert "bad-track-link" in err
        assert "does-not-exist-oi1639" in err


# ---------------------------------------------------------------------------
# 2. dispatch_bridge.py's new `stage` subcommand — stage only, never fire.
# ---------------------------------------------------------------------------

class TestStagingCli:
    def test_stage_subcommand_writes_track_id(self, tmp_path, capsys):
        instruction_file = tmp_path / "instruction.md"
        instruction_file.write_text("Do the thing.", encoding="utf-8")
        data_dir = tmp_path / "vnx-data"

        rc = dispatch_bridge.main([
            "stage",
            "--instruction", str(instruction_file),
            "--dispatch-id", "20260906-stage-cli-track",
            "--role", "backend-developer",
            "--slot", "T1",
            "--track", "oi1639-track",
            "--data-dir", str(data_dir),
        ])

        assert rc == 0
        printed_path = capsys.readouterr().out.strip()
        payload = json.loads(Path(printed_path).read_text(encoding="utf-8"))
        assert payload["track_id"] == "oi1639-track"

    def test_stage_subcommand_without_track_is_null(self, tmp_path, capsys):
        instruction_file = tmp_path / "instruction.md"
        instruction_file.write_text("Do the thing.", encoding="utf-8")
        data_dir = tmp_path / "vnx-data"

        rc = dispatch_bridge.main([
            "stage",
            "--instruction", str(instruction_file),
            "--dispatch-id", "20260906-stage-cli-notrack",
            "--role", "backend-developer",
            "--slot", "T1",
            "--data-dir", str(data_dir),
        ])

        assert rc == 0
        printed_path = capsys.readouterr().out.strip()
        payload = json.loads(Path(printed_path).read_text(encoding="utf-8"))
        assert payload["track_id"] is None

    def test_stage_subcommand_never_fires_the_dispatch(self, tmp_path, capsys):
        """stage only writes the bundle — it must never reach run_dispatch."""
        instruction_file = tmp_path / "instruction.md"
        instruction_file.write_text("Do the thing.", encoding="utf-8")
        data_dir = tmp_path / "vnx-data"

        with patch("dispatch_cli.run_dispatch") as mock_run_dispatch:
            rc = dispatch_bridge.main([
                "stage",
                "--instruction", str(instruction_file),
                "--dispatch-id", "20260906-stage-cli-nofire",
                "--role", "backend-developer",
                "--slot", "T1",
                "--data-dir", str(data_dir),
            ])

        assert rc == 0
        mock_run_dispatch.assert_not_called()

    def test_legacy_bridge_cli_unaffected_by_stage_dispatch(self, tmp_path):
        """dispatch_bridge.main with no "stage" first token keeps the original
        stage+fire behavior byte-identical (no existing caller passes it)."""
        with pytest.raises(SystemExit):
            # No --dispatch-id/--terminal: the legacy parser's required args are
            # missing, so argparse exits 2 — proving routing fell through to
            # _main_legacy, not _main_stage (which would complain about
            # --instruction instead).
            dispatch_bridge.main([])


# ---------------------------------------------------------------------------
# 3. dispatch.sh's single-entry wrapper forwards unknown door flags verbatim.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DISPATCH_SH = _REPO_ROOT / "scripts" / "commands" / "dispatch.sh"

# Stub door: records argv to VNX_TEST_ARGV_MARKER and exits 0 (no real dispatch).
_DOOR_STUB = (
    "import os, sys\n"
    'open(os.environ["VNX_TEST_ARGV_MARKER"], "w").write("\\0".join(sys.argv[1:]))\n'
)
_BRIDGE_STUB = (
    "import os, sys\n"
    'open(os.environ["VNX_TEST_BRIDGE_ARGV_MARKER"], "w").write("\\0".join(sys.argv[1:]))\n'
)


def _make_stub_home(tmp_path: Path, *, with_bridge_stub: bool = False) -> Path:
    home = tmp_path / "stubhome"
    (home / "scripts" / "lib").mkdir(parents=True)
    (home / "scripts" / "lib" / "dispatch_cli.py").write_text(_DOOR_STUB, encoding="utf-8")
    if with_bridge_stub:
        (home / "scripts" / "lib" / "dispatch_bridge.py").write_text(
            _BRIDGE_STUB, encoding="utf-8"
        )
    return home


def _preamble(stub_home: Path, data_dir: Path, dispatch_dir: Path, marker: Path) -> str:
    return f"""
set -e
VNX_HOME='{stub_home}'
VNX_DATA_DIR='{data_dir}'
VNX_DISPATCH_DIR='{dispatch_dir}'
VNX_STATE_DIR='{data_dir}/state'
export VNX_TEST_ARGV_MARKER='{marker}'
log() {{ echo "[LOG] $*"; }}
err() {{ echo "[ERR] $*" >&2; }}
source '{_DISPATCH_SH}'
"""


def _run(bash_cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)


def _promote_spec(dispatch_dir: Path, dispatch_id: str) -> None:
    pend = dispatch_dir / "pending" / dispatch_id
    pend.mkdir(parents=True)
    (pend / "dispatch-spec.json").write_text("{}", encoding="utf-8")


class TestDispatchShUnknownFlagPassthrough:
    def test_override_stop_conditions_and_track_forwarded(self, tmp_path):
        stub_home = _make_stub_home(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _promote_spec(dispatch_dir, "20260906-passthrough")
        marker = tmp_path / "argv.marker"
        cmd = _preamble(stub_home, tmp_path, dispatch_dir, marker) + """
VNX_SINGLE_ENTRY_DISPATCH=1
cmd_dispatch '20260906-passthrough' --dry-run --override-stop-conditions "operator says go" --track oi1639-track
"""
        r = _run(cmd)
        assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
        assert "unknown flag" not in (r.stdout + r.stderr).lower()
        assert marker.exists(), "door stub was not invoked"
        argv = marker.read_text().split("\0")
        assert "--override-stop-conditions" in argv
        assert argv[argv.index("--override-stop-conditions") + 1] == "operator says go"
        assert "--track" in argv
        assert argv[argv.index("--track") + 1] == "oi1639-track"

    def test_genuinely_unknown_short_flag_still_rejected(self, tmp_path):
        """Control: a short flag (-x) is NOT a door long-flag shape — the wrapper's
        own unknown-flag reject must still fire, proving the passthrough didn't turn
        into an accept-everything bypass."""
        stub_home = _make_stub_home(tmp_path)
        dispatch_dir = tmp_path / "dispatches"
        _promote_spec(dispatch_dir, "20260906-shortflag")
        marker = tmp_path / "argv.marker"
        cmd = _preamble(stub_home, tmp_path, dispatch_dir, marker) + """
VNX_SINGLE_ENTRY_DISPATCH=1
cmd_dispatch '20260906-shortflag' -x
"""
        r = _run(cmd)
        assert r.returncode == 1
        assert "unknown flag" in (r.stdout + r.stderr).lower()
        assert not marker.exists()

    def test_stage_subcommand_routes_to_bridge_not_door(self, tmp_path):
        """`vnx dispatch stage ...` must call dispatch_bridge.py, never dispatch_cli.py."""
        stub_home = _make_stub_home(tmp_path, with_bridge_stub=True)
        dispatch_dir = tmp_path / "dispatches"
        door_marker = tmp_path / "argv.marker"
        bridge_marker = tmp_path / "bridge_argv.marker"
        cmd = _preamble(stub_home, tmp_path, dispatch_dir, door_marker) + f"""
export VNX_TEST_BRIDGE_ARGV_MARKER='{bridge_marker}'
cmd_dispatch stage --instruction /tmp/x.md --dispatch-id d1 --role backend-developer --slot T1
"""
        r = _run(cmd)
        assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
        assert bridge_marker.exists(), "dispatch_bridge.py stage was NOT invoked"
        assert not door_marker.exists(), "vnx dispatch stage must not touch the door"
        argv = bridge_marker.read_text().split("\0")
        assert argv[0] == "stage"
        assert "--dispatch-id" in argv
