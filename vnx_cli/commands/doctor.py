#!/usr/bin/env python3
"""vnx doctor — validate prerequisites and project structure."""

import json
import shutil
import sys
from pathlib import Path
from typing import NamedTuple

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


class Check(NamedTuple):
    name: str
    status: str
    detail: str


def _check_tools() -> list[Check]:
    results = []
    for tool in ("python3", "git", "jq"):
        found = shutil.which(tool)
        results.append(Check(
            name=f"tool:{tool}",
            status=PASS if found else FAIL,
            detail=found or f"{tool} not found in PATH",
        ))
    return results


def _check_directories(project_dir: Path) -> list[Check]:
    results = []

    vnx_dir = project_dir / ".vnx"
    results.append(Check(
        name="dir:.vnx",
        status=PASS if vnx_dir.is_dir() else FAIL,
        detail=str(vnx_dir) if vnx_dir.is_dir() else ".vnx/ missing — run `vnx init`",
    ))

    vnx_data = project_dir / ".vnx-data"
    results.append(Check(
        name="dir:.vnx-data",
        status=PASS if vnx_data.is_dir() else FAIL,
        detail=str(vnx_data) if vnx_data.is_dir() else ".vnx-data/ missing — run `vnx init`",
    ))

    agents_dir = project_dir / "agents"
    if agents_dir.is_dir():
        agent_dirs = [d for d in agents_dir.iterdir() if d.is_dir()]
        if agent_dirs:
            results.append(Check(
                name="agents",
                status=PASS,
                detail=f"{len(agent_dirs)} agent dir(s) found",
            ))
        else:
            results.append(Check(
                name="agents",
                status=WARN,
                detail="agents/ exists but contains no subdirectories",
            ))
    else:
        results.append(Check(
            name="agents",
            status=WARN,
            detail="agents/ directory not found",
        ))

    return results


def vnx_doctor(args) -> int:
    project_dir = Path(args.project_dir).resolve()
    emit_json = getattr(args, "json", False)

    checks: list[Check] = []
    checks.extend(_check_tools())
    checks.extend(_check_directories(project_dir))

    if emit_json:
        output = {
            "project_dir": str(project_dir),
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail}
                for c in checks
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        for c in checks:
            marker = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[c.status]
            print(f"  {marker}  {c.name:<24}  {c.detail}")

    failed = any(c.status == FAIL for c in checks)
    return 1 if failed else 0
