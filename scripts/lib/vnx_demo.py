#!/usr/bin/env python3
"""VNX Demo Mode — orchestrator over existing demo infrastructure.

Wraps the existing ``demo/`` directory (dry-run replays, setup_demo.sh,
evidence files) with a clean Python entrypoint. Demo mode uses a temp
directory for runtime state so it never touches the real project.

Existing demo assets:
  demo/setup_demo.sh                       — full LeadFlow project + VNX bootstrap
  demo/dry-run/replay.sh                   — governance pipeline replay (no LLM)
  demo/dry-run/evidence/                   — real receipts, dispatches, reports
  demo/dry-run-context-rotation/replay.sh  — context rotation replay
  demo/dry-run-context-rotation/evidence/  — rotation receipts + handover

Contracts:
  G-R2:  Demo mode emits receipts (to temp directory).
  A-R1:  Shares the same canonical runtime model.
  Productization §2.3: Demo mode = replay/dry-run, temp state, no persistent changes.
  Productization §7.2: Receipt completeness in all modes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vnx_mode import VNXMode, check_mode_feature_enabled, _atomic_write_json
from vnx_paths import resolve_paths


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AVAILABLE_SCENARIOS = {
    "governance-pipeline": {
        "subdir": "dry-run",
        "script": "replay.sh",
        "description": "Full governance lifecycle: dispatch, receipt, quality advisory, open items (6 PRs, 3 tracks)",
    },
    "context-rotation": {
        "subdir": "dry-run-context-rotation",
        "script": "replay.sh",
        "description": "Context window exhaustion, handover, and session resumption",
    },
}

DEFAULT_SCENARIO = "governance-pipeline"


# ---------------------------------------------------------------------------
# Demo environment
# ---------------------------------------------------------------------------

class DemoEnvironment:
    """Manages a temp directory that mirrors .vnx-data for demo mode."""

    def __init__(self, demo_dir: Path, vnx_home: Path, project_root: Path):
        self.demo_dir = demo_dir
        self.vnx_home = vnx_home
        self.project_root = project_root
        self.data_dir = demo_dir / ".vnx-data"
        self.state_dir = self.data_dir / "state"

    @classmethod
    def create(cls, vnx_home: Optional[str] = None) -> "DemoEnvironment":
        """Create a fresh temp-backed demo environment."""
        if not check_mode_feature_enabled(VNXMode.DEMO):
            raise RuntimeError(
                "Demo mode is disabled (VNX_DEMO_MODE_ENABLED=0). "
                "Set VNX_DEMO_MODE_ENABLED=1 to enable."
            )

        paths = resolve_paths()
        home = Path(vnx_home or paths["VNX_HOME"])
        project_root = Path(paths["PROJECT_ROOT"])
        demo_dir = Path(tempfile.mkdtemp(prefix="vnx-demo-"))

        env = cls(demo_dir, home, project_root)
        env._init_layout()
        return env

    def _init_layout(self) -> None:
        """Create minimal .vnx-data layout in the temp directory."""
        for sub in [
            self.state_dir,
            self.data_dir / "dispatches" / "pending",
            self.data_dir / "dispatches" / "active",
            self.data_dir / "dispatches" / "completed",
            self.data_dir / "receipts",
            self.data_dir / "logs",
        ]:
            sub.mkdir(parents=True, exist_ok=True)

        # Write mode.json
        _atomic_write_json(self.data_dir / "mode.json", {
            "mode": "demo",
            "set_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": 1,
            "temp_dir": str(self.demo_dir),
        })

    def demo_dir_path(self) -> Path:
        """Return the VNX_HOME/demo directory with replay scripts."""
        return self.vnx_home / "demo"

    def cleanup(self) -> None:
        """Remove the temp directory."""
        if self.demo_dir.exists():
            shutil.rmtree(self.demo_dir, ignore_errors=True)

    def seed_evidence(self, scenario: str) -> bool:
        """Copy evidence files from a scenario into the demo state dir.

        This allows the dashboard to project real-looking state from
        the demo evidence files.
        """
        info = AVAILABLE_SCENARIOS.get(scenario)
        if not info:
            return False

        evidence_dir = self.demo_dir_path() / info["subdir"] / "evidence"
        if not evidence_dir.is_dir():
            return False

        # Copy receipts
        for ndjson in evidence_dir.glob("*.ndjson"):
            shutil.copy2(str(ndjson), str(self.data_dir / "receipts" / ndjson.name))

        # Copy state-like files
        for state_file in ["pr_queue_state.json", "open_items_digest.json",
                           "last_quality_summary.json"]:
            src = evidence_dir / state_file
            if src.exists():
                shutil.copy2(str(src), str(self.state_dir / state_file))

        # Copy dispatch audit
        audit_src = evidence_dir / "dispatch_audit.jsonl"
        if audit_src.exists():
            shutil.copy2(str(audit_src), str(self.state_dir / "dispatch_audit.jsonl"))

        return True


# ---------------------------------------------------------------------------
# Scenario runners
# ---------------------------------------------------------------------------

def run_replay(scenario: str = DEFAULT_SCENARIO, fast: bool = False,
               vnx_home: Optional[str] = None) -> int:
    """Run a dry-run replay scenario.

    Returns the subprocess exit code.
    """
    paths = resolve_paths()
    home = Path(vnx_home or paths["VNX_HOME"])
    demo_base = home / "demo"

    info = AVAILABLE_SCENARIOS.get(scenario)
    if not info:
        print(f"Unknown scenario: {scenario}")
        print(f"Available: {', '.join(AVAILABLE_SCENARIOS.keys())}")
        return 1

    script = demo_base / info["subdir"] / info["script"]
    if not script.exists():
        print(f"Replay script not found: {script}")
        return 1

    args = ["bash", str(script)]
    if fast:
        args.append("--fast")

    result = subprocess.run(args, cwd=str(script.parent))
    return result.returncode


def run_setup_demo(target_dir: Optional[str] = None,
                   vnx_home: Optional[str] = None) -> int:
    """Run the full demo project setup (LeadFlow + VNX bootstrap).

    Returns the subprocess exit code.
    """
    paths = resolve_paths()
    home = Path(vnx_home or paths["VNX_HOME"])
    setup_script = home / "demo" / "setup_demo.sh"

    if not setup_script.exists():
        print(f"Setup script not found: {setup_script}")
        return 1

    args = ["bash", str(setup_script)]
    if target_dir:
        args.append(target_dir)

    result = subprocess.run(args)
    return result.returncode


def run_dashboard_demo(vnx_home: Optional[str] = None, port: int = 8111) -> int:
    """Launch the dashboard with demo evidence data.

    Seeds a temp environment with evidence from the governance-pipeline
    scenario, then starts serve_dashboard.py pointing at the demo state.

    Returns the subprocess exit code.
    """
    env = DemoEnvironment.create(vnx_home)
    try:
        env.seed_evidence("governance-pipeline")

        dashboard_script = env.vnx_home / "dashboard" / "serve_dashboard.py"
        if not dashboard_script.exists():
            print(f"Dashboard server not found: {dashboard_script}")
            return 1

        print(f"Starting demo dashboard on port {port}...")
        print(f"Demo state: {env.data_dir}")
        print("Press Ctrl+C to stop.\n")

        result = subprocess.run(
            [sys.executable, str(dashboard_script), "--port", str(port)],
            env={
                **os.environ,
                "VNX_DATA_DIR": str(env.data_dir),
                "VNX_STATE_DIR": str(env.state_dir),
                "VNX_HOME": str(env.vnx_home),
            },
        )
        return result.returncode
    except KeyboardInterrupt:
        print("\nDemo dashboard stopped.")
        return 0
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# List scenarios
# ---------------------------------------------------------------------------

def list_scenarios() -> List[Dict[str, str]]:
    """Return available demo scenarios with descriptions."""
    paths = resolve_paths()
    demo_base = Path(paths["VNX_HOME"]) / "demo"
    result = []
    for name, info in AVAILABLE_SCENARIOS.items():
        script = demo_base / info["subdir"] / info["script"]
        result.append({
            "name": name,
            "description": info["description"],
            "available": script.exists(),
        })
    return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="VNX Demo Mode — showcase VNX without API keys or project setup"
    )
    sub = parser.add_subparsers(dest="action", help="Demo action")

    # vnx demo replay
    replay_p = sub.add_parser("replay", help="Replay a recorded governance flow")
    replay_p.add_argument("scenario", nargs="?", default=DEFAULT_SCENARIO,
                          choices=list(AVAILABLE_SCENARIOS.keys()),
                          help=f"Scenario to replay (default: {DEFAULT_SCENARIO})")
    replay_p.add_argument("--fast", action="store_true", help="Fast playback (0.5s delay)")

    # vnx demo dashboard
    dash_p = sub.add_parser("dashboard", help="Launch dashboard with sample data")
    dash_p.add_argument("--port", type=int, default=8111, help="Dashboard port (default: 8111)")

    # vnx demo setup
    setup_p = sub.add_parser("setup", help="Create a full demo project (LeadFlow + VNX)")
    setup_p.add_argument("--target", help="Target directory (default: ~/Development/vnx_demo)")

    # vnx demo list
    sub.add_parser("list", help="List available demo scenarios")

    args = parser.parse_args()

    if args.action == "replay":
        return run_replay(scenario=args.scenario, fast=args.fast)
    elif args.action == "dashboard":
        return run_dashboard_demo(port=args.port)
    elif args.action == "setup":
        return run_setup_demo(target_dir=args.target)
    elif args.action == "list":
        scenarios = list_scenarios()
        for s in scenarios:
            status = "available" if s["available"] else "missing"
            print(f"  {s['name']:30s} [{status}]  {s['description']}")
        return 0
    else:
        # Default: run governance-pipeline replay
        return run_replay()


if __name__ == "__main__":
    sys.exit(main())
