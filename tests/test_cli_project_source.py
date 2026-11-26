"""AG-1g1: actual CLI processes refuse unowned stores and use authoritative tenants."""
import json
import os
from pathlib import Path
import pty
import sqlite3
import subprocess
import sys

import pytest

from test_seed_tracks_from_roadmap import _make_db

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
COMMANDS = ("planning_cli", "sign", "seed_tracks_from_roadmap", "crash_recovery_sweep",
            "orphan_sweep")


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("source", ["missing", "empty", "env", "marker", "layout", "explicit", "invalid", "invalid-explicit"])
def test_cli_project_source(tmp_path, command, source):
    root = tmp_path / ".vnx-data" / "other" if source == "layout" else tmp_path
    state = _make_db(root)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "VNX_STATE_DIR": str(state),
           "VNX_DATA_DIR": str(root), "VNX_DATA_DIR_EXPLICIT": "1", "VNX_PROJECT_ROOT": str(root)}
    if source in {"env", "explicit", "invalid"}:
        env["VNX_PROJECT_ID"] = "../bad" if source == "invalid" else "other"
    if source == "empty":
        env["VNX_PROJECT_ID"] = ""
    if source in {"marker", "explicit"}:
        (root / ".vnx-project-id").write_text("other\n")
    extra = []
    if source == "explicit":
        env["VNX_PROJECT_ID"] = "different"
        extra = ["--project-id", "other"]
    if source == "invalid-explicit":
        env["VNX_PROJECT_ID"] = "other"
        extra = ["--project-id", "/bad"]
    roadmap = root / "roadmap.yaml"
    roadmap.write_text("features: []\n")
    with sqlite3.connect(state / "runtime_coordination.db") as conn:
        conn.execute("CREATE TABLE terminal_leases (terminal_id TEXT, project_id TEXT, worker_pid INTEGER)")
        conn.executemany("INSERT INTO terminal_leases VALUES ('T1', ?, ?)",
                         [("other", os.getpid()), ("other-more", 2147483647), ("vnx-dev", 2147483647)])
        conn.execute("INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
                     ("provider-agnostic-permissions", "other", "other-project-track", "goal"))
    active = root / "dispatches" / "active" / "dlv-source-check"
    active.mkdir(parents=True)
    (active / "manifest.json").write_text(json.dumps({"terminal": "T1"}))
    args = {
        "planning_cli": ["objective", "list", "--state-dir", str(state)],
        "sign": ["plan-gate", "sign", "absent", "--key", str(root / "key"), "--identity", "operator"],
        "seed_tracks_from_roadmap": ["--state-dir", str(state), "--roadmap", str(roadmap)],
        "crash_recovery_sweep": ["--data-dir", str(root), "--dry-run", "--json"],
        "orphan_sweep": ["--data-dir", str(root), "--repo-root", str(root), "--dry-run", "--json"],
    }[command]
    script = "planning_cli" if command == "sign" else command
    master, slave = pty.openpty()
    with os.fdopen(master), os.fdopen(slave) as terminal:
        proc = subprocess.run([sys.executable, str(SCRIPTS / f"{script}.py"), *args, *extra],
                              stdin=terminal, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    output = proc.stdout + proc.stderr
    if source in {"missing", "empty", "invalid", "invalid-explicit"}:
        assert proc.returncode == 2, output
        assert ("vnx init" in output and "VNX_PROJECT_ID" in output) if source in {"missing", "empty"} else "invalid" in output
    elif command == "sign":
        assert proc.returncode == 3 and "('other')" in output, output
    elif command == "planning_cli":
        assert proc.returncode == 0 and "other-project-track" in output, output
    elif command == "seed_tracks_from_roadmap":
        assert proc.returncode == 0 and "project_id : other" in output, output
    else:
        assert proc.returncode in (0, 1), output  # tmux may be absent; the active pass still runs.
        key = "active_skipped_alive" if command == "orphan_sweep" else "skipped_alive"
        assert json.loads(proc.stdout)[key] == ["dlv-source-check"], output
