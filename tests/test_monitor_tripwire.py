"""Tests for the dumb monitor tripwire (hooks/monitor_tripwire.sh).

RED against origin/main (the script does not exist), GREEN on this branch.

The tripwire's independence contract — no shared code, no DB, no Python
interpreter with the monitor it watches — is asserted as a test, so a future
"helpful" refactor that imports shared tooling fails CI.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRIPWIRE = REPO_ROOT / "hooks" / "monitor_tripwire.sh"


def _run_tripwire(env_overrides: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(TRIPWIRE)],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=10,
    )


def _write_heartbeat(path: Path, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"component": "producer_freshness_monitor", "status": "ok"}), encoding="utf-8")
    ts = time.time() - age_seconds
    os.utime(path, (ts, ts))


def test_tripwire_shares_no_code_db_or_interpreter_with_monitor() -> None:
    """The independence contract IS the feature (an OI-852-class break must
    not kill monitor and alarm together). Keep the tripwire dumb."""
    text = TRIPWIRE.read_text(encoding="utf-8")
    # Strip comment lines: the contract is about what the script EXECUTES.
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).lower()
    assert "python" not in code, "tripwire must not use the Python interpreter"
    assert "sqlite" not in code, "tripwire must not open a DB connection"
    assert "scripts/lib" not in code, "tripwire must not import shared monitor code"
    assert "find " in code, "tripwire checks the heartbeat age with find -mmin"


def test_tripwire_quiet_on_fresh_heartbeat(tmp_path: Path) -> None:
    hb = tmp_path / "health" / "producer_freshness_monitor.json"
    _write_heartbeat(hb, age_seconds=300)
    proc = _run_tripwire({"VNX_TRIPWIRE_HEARTBEAT_GLOB": str(hb)})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_tripwire_fires_on_stale_heartbeat(tmp_path: Path) -> None:
    hb = tmp_path / "health" / "producer_freshness_monitor.json"
    _write_heartbeat(hb, age_seconds=2 * 86400)
    proc = _run_tripwire({"VNX_TRIPWIRE_HEARTBEAT_GLOB": str(hb)})
    assert proc.returncode == 0  # never blocks a session
    assert "tripwire" in proc.stdout
    assert "older than" in proc.stdout
    assert "additionalContext" in proc.stdout


def test_tripwire_fires_when_heartbeat_missing(tmp_path: Path) -> None:
    proc = _run_tripwire({"VNX_TRIPWIRE_HEARTBEAT_GLOB": str(tmp_path / "nope" / "*.json")})
    assert proc.returncode == 0
    assert "NO heartbeat" in proc.stdout


def test_tripwire_threshold_is_configurable(tmp_path: Path) -> None:
    hb = tmp_path / "health" / "producer_freshness_monitor.json"
    _write_heartbeat(hb, age_seconds=3600)  # 1h old
    # Default threshold (26h): quiet.
    assert _run_tripwire({"VNX_TRIPWIRE_HEARTBEAT_GLOB": str(hb)}).stdout.strip() == ""
    # Tight threshold (30 min): fires.
    proc = _run_tripwire({"VNX_TRIPWIRE_HEARTBEAT_GLOB": str(hb), "VNX_TRIPWIRE_MAX_AGE_MIN": "30"})
    assert "older than 30m" in proc.stdout
