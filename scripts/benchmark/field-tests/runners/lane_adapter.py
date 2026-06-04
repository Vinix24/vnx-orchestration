"""lane_adapter.py — route a benchmark dispatch through the right VNX lane.

Each lane in `models.yaml` maps to an existing VNX dispatcher:

| provider          | dispatcher                              | notes                          |
|-------------------|-----------------------------------------|--------------------------------|
| claude            | subprocess_dispatch.py (T1/T2/T3 pin)   | --model accepts opus/sonnet/haiku + explicit ids |
| litellm:deepseek  | provider_dispatch.py                    | provider=litellm:deepseek      |
| litellm:moonshot  | provider_dispatch.py                    | provider=kimi (CLI OAuth)      |
| litellm:zai       | provider_dispatch.py                    | provider=litellm:zai           |
| local-gemma       | provider_dispatch.py                    | provider=local-gemma           |

Returns a DispatchResult dataclass with the receipt path + timing + raw stdout/stderr.
No mocking; if the dispatcher binary or credentials are missing the call fails loudly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[3]
SUBPROCESS_DISPATCH = REPO_ROOT / "scripts" / "lib" / "subprocess_dispatch.py"
TMUX_INTERACTIVE_DISPATCH = REPO_ROOT / "scripts" / "lib" / "tmux_interactive_dispatch.py"
PROVIDER_DISPATCH = REPO_ROOT / "scripts" / "lib" / "provider_dispatch.py"
CENTRAL_REPORT_DIR = Path.home() / ".vnx-data" / "vnx-dev" / "unified_reports"


@dataclass
class DispatchResult:
    lane_id: str
    task_id: str
    replication: int
    dispatch_id: str
    success: bool
    wallclock_seconds: float
    report_path: Optional[Path]
    stdout: str
    stderr: str
    error: Optional[str] = None


def _claude_subprocess(
    lane: dict, dispatch_id: str, instruction: str,
    dispatch_paths: str, deadline_seconds: int,
) -> tuple[int, str, str]:
    """Route a Claude lane via subprocess_dispatch.py on T1 (pinned)."""
    env = {
        **os.environ,
        "VNX_STATE_DIR": ".vnx-data/state",
        "VNX_DATA_DIR": ".vnx-data",
        "VNX_DISPATCH_DIR": ".vnx-data/dispatches",
    }
    cmd = [
        sys.executable, str(SUBPROCESS_DISPATCH),
        "--terminal-id", "T1",
        "--dispatch-id", dispatch_id,
        "--model", lane["model_arg"],
        "--role", "backend-developer",
        "--pr-id", f"BENCH-{lane['id']}",
        "--dispatch-paths", dispatch_paths,
        "--instruction", instruction,
        "--allow-unstaged",
        "--reason", f"benchmark run {dispatch_id}",
    ]
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True,
        timeout=deadline_seconds + 60, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _provider_dispatch(
    lane: dict, dispatch_id: str, instruction: str,
    dispatch_paths: str, deadline_seconds: int,
) -> tuple[int, str, str]:
    """Route a non-Claude provider via provider_dispatch.py."""
    env = {
        **os.environ,
        "VNX_STATE_DIR": ".vnx-data/state",
        "VNX_DATA_DIR": ".vnx-data",
        "VNX_DISPATCH_DIR": ".vnx-data/dispatches",
    }
    provider_map = {
        "litellm:deepseek": "litellm:deepseek",
        "litellm:moonshot": "kimi",
        "litellm:zai": "litellm:zai",
        "local-gemma": "local-gemma",
    }
    provider = provider_map.get(lane["provider"], lane["provider"])
    cmd = [
        sys.executable, str(PROVIDER_DISPATCH),
        "--provider", provider,
        "--terminal-id", "headless",
        "--dispatch-id", dispatch_id,
        "--model", lane["model_arg"],
        "--role", "backend-developer",
        "--dispatch-paths", dispatch_paths,
        "--instruction", instruction,
    ]
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True,
        timeout=deadline_seconds + 60, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def dispatch(
    lane: dict,
    task_id: str,
    replication: int,
    instruction: str,
    dispatch_paths: str,
    deadline_seconds: int,
) -> DispatchResult:
    """Run a single (lane, task, replication) dispatch and return result."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dispatch_id = f"bench-{lane['id']}-{task_id}-r{replication}-{ts}"
    start = time.monotonic()

    try:
        if lane["provider"] == "claude":
            rc, out, err = _claude_subprocess(
                lane, dispatch_id, instruction, dispatch_paths, deadline_seconds,
            )
        else:
            rc, out, err = _provider_dispatch(
                lane, dispatch_id, instruction, dispatch_paths, deadline_seconds,
            )
    except subprocess.TimeoutExpired as exc:
        wallclock = time.monotonic() - start
        return DispatchResult(
            lane_id=lane["id"], task_id=task_id, replication=replication,
            dispatch_id=dispatch_id, success=False, wallclock_seconds=wallclock,
            report_path=None, stdout="", stderr=str(exc),
            error=f"timeout after {deadline_seconds}s",
        )

    wallclock = time.monotonic() - start
    report_path = CENTRAL_REPORT_DIR / f"{dispatch_id}.md"
    if not report_path.exists():
        report_path = CENTRAL_REPORT_DIR / f"{dispatch_id}_report.md"
    success = rc == 0 and report_path.exists()

    return DispatchResult(
        lane_id=lane["id"], task_id=task_id, replication=replication,
        dispatch_id=dispatch_id, success=success, wallclock_seconds=wallclock,
        report_path=report_path if report_path.exists() else None,
        stdout=out[-2000:], stderr=err[-2000:],
        error=None if success else f"rc={rc} report_exists={report_path.exists()}",
    )


def load_lanes(models_yaml: Path, lane_ids: list[str]) -> list[dict]:
    """Load lane configs from models.yaml, filtered to requested ids."""
    import yaml
    data = yaml.safe_load(models_yaml.read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in data["models"]}
    missing = [lid for lid in lane_ids if lid not in by_id]
    if missing:
        raise ValueError(f"Lane(s) not in models.yaml: {missing}")
    return [by_id[lid] for lid in lane_ids]
