#!/usr/bin/env python3
"""VNX Setup — one-command project setup orchestrator.

Replaces the manual multi-step post-install flow:
  install.sh → vnx init → vnx doctor → vnx register

with a single:
  vnx setup [--starter|--operator]

Design:
  - Chains: prereq check → init → doctor → register → next-steps.
  - Each step reports status; stops on critical failure.
  - Suggests shell-helper installation when appropriate.
  - Supports --starter (default for new users) and --operator modes.

Governance: G-R5 (every simplification needs QA evidence).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env, resolve_paths

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


# ---------------------------------------------------------------------------
# Step model
# ---------------------------------------------------------------------------

PASS = "pass"
SKIP = "skip"
FAIL = "fail"


@dataclass
class SetupStep:
    name: str
    status: str  # pass | skip | fail
    message: str
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


STATUS_ICON = {
    PASS: f"{GREEN}OK{RESET}",
    SKIP: f"{YELLOW}SKIP{RESET}",
    FAIL: f"{RED}FAIL{RESET}",
}


def _log(step: SetupStep) -> None:
    icon = STATUS_ICON.get(step.status, step.status)
    print(f"  [{icon}] {step.name}: {step.message}")
    for d in step.details:
        print(f"         {d}")


# ---------------------------------------------------------------------------
# Step: prereq check
# ---------------------------------------------------------------------------

def step_prereq_check(paths: Dict[str, str]) -> SetupStep:
    """Run install validator in prereq-check mode."""
    install_validator = SCRIPT_DIR / "vnx_install.py"
    if not install_validator.exists():
        return SetupStep("prereq-check", SKIP, "Install validator not found (skipping)")

    try:
        result = subprocess.run(
            [sys.executable, str(install_validator), "--check", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            checks = json.loads(result.stdout)
            fails = sum(1 for c in checks if c["status"] == "fail")
            warns = sum(1 for c in checks if c["status"] == "warn")
            if fails:
                details = [
                    f"  {c['name']}: {c['message']}"
                    for c in checks if c["status"] == "fail"
                ]
                return SetupStep("prereq-check", FAIL,
                                 f"{fails} prerequisite(s) missing", details)
            if warns:
                return SetupStep("prereq-check", PASS,
                                 f"Prerequisites OK ({warns} optional warnings)")
            return SetupStep("prereq-check", PASS, "All prerequisites met")
        else:
            return SetupStep("prereq-check", FAIL,
                             "Prerequisite check failed",
                             [result.stdout[:500] if result.stdout else ""])
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return SetupStep("prereq-check", FAIL, f"Check error: {e}")


# ---------------------------------------------------------------------------
# Step: init
# ---------------------------------------------------------------------------

def step_init(paths: Dict[str, str], mode: str) -> SetupStep:
    """Run vnx init with appropriate mode flag."""
    vnx_home = Path(paths["VNX_HOME"])
    init_script = vnx_home / "scripts" / "vnx_init.py"

    if not init_script.exists():
        return SetupStep("init", FAIL, f"Init script not found: {init_script}")

    args = [sys.executable, str(init_script), f"--{mode}", "--json"]
    try:
        result = subprocess.run(
            args,
            capture_output=True, text=True, timeout=60,
            env={**os.environ, **{k: v for k, v in paths.items()},
                 "PYTHONPATH": str(vnx_home / "scripts" / "lib")},
        )

        if result.returncode == 0:
            steps = json.loads(result.stdout)
            fails = sum(1 for s in steps if s["status"] == "fail")
            if fails:
                details = [
                    f"  {s['name']}: {s['message']}"
                    for s in steps if s["status"] == "fail"
                ]
                return SetupStep("init", FAIL,
                                 f"Init completed with {fails} failure(s)", details)
            return SetupStep("init", PASS,
                             f"Initialized in {mode} mode")
        else:
            stderr = result.stderr[:300] if result.stderr else ""
            return SetupStep("init", FAIL,
                             f"Init failed (exit {result.returncode})",
                             [stderr] if stderr else [])
    except subprocess.TimeoutExpired:
        return SetupStep("init", FAIL, "Init timed out (60s)")


# ---------------------------------------------------------------------------
# Step: write mode
# ---------------------------------------------------------------------------

def step_write_mode(paths: Dict[str, str], mode: str) -> SetupStep:
    """Write mode.json for the selected mode."""
    try:
        from vnx_mode import VNXMode, write_mode
        mode_enum = VNXMode(mode)
        path = write_mode(mode_enum, paths.get("VNX_DATA_DIR"))
        return SetupStep("mode", PASS, f"Mode set: {mode} ({path})")
    except Exception as e:
        return SetupStep("mode", FAIL, f"Failed to write mode: {e}")


# ---------------------------------------------------------------------------
# Step: doctor (quick)
# ---------------------------------------------------------------------------

def step_doctor(paths: Dict[str, str]) -> SetupStep:
    """Run vnx doctor to validate installation health."""
    vnx_home = Path(paths["VNX_HOME"])
    doctor_script = vnx_home / "scripts" / "vnx_doctor.py"

    if not doctor_script.exists():
        return SetupStep("doctor", SKIP, "Doctor script not found")

    try:
        result = subprocess.run(
            [sys.executable, str(doctor_script), "--json"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, **{k: v for k, v in paths.items()},
                 "PYTHONPATH": str(vnx_home / "scripts" / "lib")},
        )

        checks = json.loads(result.stdout)
        fails = sum(1 for c in checks if c["status"] == "fail")
        warns = sum(1 for c in checks if c["status"] == "warn")
        passes = sum(1 for c in checks if c["status"] == "pass")

        if fails:
            details = [
                f"  {c['name']}: {c['message']}"
                for c in checks if c["status"] == "fail"
            ][:5]
            return SetupStep("doctor", FAIL,
                             f"{fails} check(s) failed, {warns} warnings",
                             details)
        if warns:
            return SetupStep("doctor", PASS,
                             f"{passes} passed, {warns} warnings")
        return SetupStep("doctor", PASS,
                         f"All {passes} checks passed")
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return SetupStep("doctor", FAIL, f"Doctor error: {e}")


# ---------------------------------------------------------------------------
# Step: register
# ---------------------------------------------------------------------------

def step_register(paths: Dict[str, str]) -> SetupStep:
    """Register project in global registry."""
    vnx_home = Path(paths["VNX_HOME"])
    vnx_bin = vnx_home / "bin" / "vnx"

    if not vnx_bin.exists():
        return SetupStep("register", SKIP, "VNX binary not found")

    try:
        result = subprocess.run(
            [str(vnx_bin), "register"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "PROJECT_ROOT": paths["PROJECT_ROOT"],
                 "VNX_HOME": paths["VNX_HOME"]},
        )
        if result.returncode == 0:
            return SetupStep("register", PASS,
                             "Project registered in ~/.vnx/projects.json")
        return SetupStep("register", SKIP,
                         "Registration skipped (non-critical)")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return SetupStep("register", SKIP,
                         "Registration skipped (non-critical)")


# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------

def get_next_steps(mode: str, paths: Dict[str, str]) -> List[str]:
    """Generate context-aware next steps."""
    steps = []

    # Check shell helper
    try:
        # Check if vnx() function exists in current shell
        result = subprocess.run(
            ["bash", "-c", "type vnx 2>/dev/null"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            steps.append("Install shell helper:  vnx install-shell-helper")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        steps.append("Install shell helper:  vnx install-shell-helper")

    if mode == "starter":
        steps.extend([
            "Check health:         vnx doctor",
            "View status:          vnx status",
            "Upgrade to operator:  vnx init --operator",
        ])
    elif mode == "operator":
        steps.extend([
            "Check health:         vnx doctor",
            "Launch tmux grid:     vnx start",
            "View status:          vnx status",
        ])

    return steps


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_setup(mode: str = "starter",
              skip_register: bool = False,
              skip_doctor: bool = False) -> List[SetupStep]:
    """Execute the full setup sequence."""
    paths = ensure_env()
    results: List[SetupStep] = []

    # Step 1: Prerequisites
    prereq = step_prereq_check(paths)
    results.append(prereq)
    if prereq.status == FAIL:
        return results  # Stop early on critical prereq failure

    # Step 2: Init
    init_result = step_init(paths, mode)
    results.append(init_result)
    if init_result.status == FAIL:
        return results

    # Step 3: Write mode
    mode_result = step_write_mode(paths, mode)
    results.append(mode_result)

    # Step 4: Doctor (optional)
    if not skip_doctor:
        doctor_result = step_doctor(paths)
        results.append(doctor_result)

    # Step 5: Register (optional)
    if not skip_register:
        register_result = step_register(paths)
        results.append(register_result)

    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="VNX Setup — one-command project setup",
    )
    parser.add_argument("--starter", action="store_true",
                        help="Set up in starter mode (default, no tmux required)")
    parser.add_argument("--operator", action="store_true",
                        help="Set up in operator mode (full tmux grid)")
    parser.add_argument("--skip-register", action="store_true",
                        help="Skip project registration")
    parser.add_argument("--skip-doctor", action="store_true",
                        help="Skip doctor health check")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    # Determine mode
    if args.operator:
        mode = "operator"
    else:
        mode = "starter"

    if not args.json:
        print(f"\n{BOLD}VNX Setup{RESET}")
        print(f"{'─' * 50}")
        print(f"  Mode: {BOLD}{mode}{RESET}")
        print(f"{'─' * 50}")

    results = run_setup(
        mode=mode,
        skip_register=args.skip_register,
        skip_doctor=args.skip_doctor,
    )

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            _log(r)

        fails = sum(1 for r in results if r.status == FAIL)

        print(f"\n{'─' * 50}")
        if fails:
            print(f"{RED}Setup failed{RESET} — fix the issues above and re-run 'vnx setup'")
        else:
            print(f"{GREEN}Setup complete{RESET}")
            print(f"\n{BOLD}Next steps:{RESET}")
            paths = resolve_paths()
            for step in get_next_steps(mode, paths):
                print(f"  {step}")
            print()

    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
