"""vnx doctor — validate prerequisites and project structure."""

import argparse
import json
import os
import shutil
import sys


_BINARY_CHECKS = [
    ("python3", "Python 3 runtime"),
    ("git", "Git version control"),
    ("jq", "jq JSON processor"),
]

_DIR_CHECKS = [
    (".vnx", "VNX config directory"),
    (".vnx-data", "VNX runtime data directory"),
]


def vnx_doctor(args: argparse.Namespace) -> int:
    project_dir = os.path.abspath(args.project_dir)
    json_mode = getattr(args, "json_output", False)

    results = []

    # Binary checks
    for binary, label in _BINARY_CHECKS:
        found = shutil.which(binary) is not None
        results.append({
            "check": label,
            "key": binary,
            "status": "PASS" if found else "FAIL",
            "detail": shutil.which(binary) if found else f"{binary!r} not found in PATH",
        })

    # Directory checks
    for rel_dir, label in _DIR_CHECKS:
        full = os.path.join(project_dir, rel_dir)
        exists = os.path.isdir(full)
        results.append({
            "check": label,
            "key": rel_dir,
            "status": "PASS" if exists else "FAIL",
            "detail": full if exists else f"Directory missing: {full}",
        })

    # At least one agent dir
    agents_dir = os.path.join(project_dir, "agents")
    has_agents = os.path.isdir(agents_dir) and bool(os.listdir(agents_dir))
    results.append({
        "check": "Agent directory populated",
        "key": "agents_dir",
        "status": "PASS" if has_agents else "WARN",
        "detail": agents_dir if has_agents else f"agents/ missing or empty: {agents_dir}",
    })

    any_fail = any(r["status"] == "FAIL" for r in results)

    if json_mode:
        print(json.dumps({"checks": results, "ok": not any_fail}, indent=2))
    else:
        _print_human(results)

    return 1 if any_fail else 0


def _print_human(results: list) -> None:
    width = max(len(r["check"]) for r in results) + 2
    for r in results:
        status = r["status"]
        label = r["check"].ljust(width)
        detail = r["detail"]
        if status == "PASS":
            marker = "[PASS]"
        elif status == "WARN":
            marker = "[WARN]"
        else:
            marker = "[FAIL]"
        print(f"  {marker}  {label}  {detail}")

    any_fail = any(r["status"] == "FAIL" for r in results)
    print()
    if any_fail:
        print("doctor: one or more checks FAILED. Run `vnx init` to scaffold missing structure.")
    else:
        print("doctor: all checks passed.")
