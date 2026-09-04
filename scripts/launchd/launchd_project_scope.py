#!/usr/bin/env python3
"""launchd_project_scope.py — per-project launchd label guard (OI-1509/OI-1510).

Measured live on this machine while building this module (2026-09-04,
``launchctl list | grep vnx``): ``com.vnx.gate-obligation-runner`` (bare, no
project suffix — mission-control's job) and
``com.vnx.gate-obligation-runner.vnx-dev`` (a hand-installed workaround, never
produced by any script in this repo) were BOTH loaded at once. That bare
label is the collision-enabling condition OI-1509/OI-1510 describe: launchd
itself refuses to run two DIFFERENT jobs under one Label at the same time, so
a genuine collision never shows up as two simultaneous entries in
``launchctl list`` — it shows up as the SECOND install's ``launchctl unload``
silently tearing down the FIRST project's job before overwriting the shared
destination file. The bare label is what makes that possible; a per-project
suffix is what makes it impossible. This module checks two properties, one
static and one live:

  1. ``check_template_contract`` — every daemon family this repo requires to
     be per-project (``REQUIRED_PER_PROJECT_FAMILIES`` below) must ship a
     template under ``scripts/launchd/`` whose ``Label`` carries the
     ``${VNX_PROJECT_ID}`` placeholder. Deterministic, no host dependency.

  2. ``check_installed_state`` — given a ``launchctl list`` snapshot (always
     injected by the caller, never read directly by the functions below) and
     this project's id, every required family must have a loaded instance
     named ``<family>.<project_id>``, and no loaded label for that family may
     be bare or carry a malformed (non-project-id-shaped) suffix.

``main()`` is the live, read-only operator/CI entry point: it resolves this
project's id (``vnx_paths.resolve_project_id()``), shells out to the real
``launchctl list`` (never ``load``/``unload``/``bootstrap`` — this module
installs nothing), and reports violations with a non-zero exit code. It never
writes to ``~/Library/LaunchAgents`` or any state store.
"""
from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from job_exit_capture import _parse_launchctl_list  # noqa: E402
import vnx_paths  # noqa: E402

# Mirrors PROJECT_ID_RE / _vnx_valid_project_id (scripts/lib/vnx_paths.py,
# scripts/lib/vnx_paths.sh) — kept as a small, deliberate duplicate here,
# same lockstep-mirror pattern vnx_paths.sh itself documents for its own
# py/sh duplication.
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

# The two daemon families OI-1509/OI-1510 name explicitly. Not derived
# dynamically from whatever templates happen to exist under scripts/launchd/:
# most templates in that directory (conversation-analyzer, dashboard-adjacent
# jobs, etc.) are deliberately single-instance-per-machine, not per-project,
# and must NOT be flagged just for lacking a ${VNX_PROJECT_ID} placeholder.
REQUIRED_PER_PROJECT_FAMILIES: tuple = (
    "com.vnx.gate-obligation-runner",
    "com.vnx.receipt-processor",
)

_PLACEHOLDER = "${VNX_PROJECT_ID}"


def _read_template_label(plist_path: Path) -> Optional[str]:
    """Read a template's raw (pre-substitution) Label. None on any read/parse
    failure — the caller turns that into its own, more specific violation."""
    try:
        with open(plist_path, "rb") as fh:
            data = plistlib.load(fh)
    except (plistlib.InvalidFileException, OSError, ValueError):
        # vnx-silent-except: unreadable/malformed template surfaces as a
        # template_unreadable violation in check_template_contract, not a crash
        return None
    label = data.get("Label")
    return label if isinstance(label, str) else None


def check_template_contract(
    templates_dir: Path,
    families: Sequence[str] = REQUIRED_PER_PROJECT_FAMILIES,
) -> Dict[str, Any]:
    """Static contract check: no host, no launchctl, no environment — every
    required family's template must exist under ``templates_dir`` and its
    ``Label`` must contain the ``${VNX_PROJECT_ID}`` placeholder.
    """
    templates_dir = Path(templates_dir)
    violations: List[Dict[str, Any]] = []
    checked: List[Dict[str, Any]] = []

    for family in families:
        template_path = templates_dir / f"{family}.plist"
        if not template_path.is_file():
            violations.append(
                {
                    "family": family,
                    "kind": "template_missing",
                    "detail": f"no template at {template_path}",
                }
            )
            checked.append({"family": family, "template": str(template_path), "label": None})
            continue

        label = _read_template_label(template_path)
        checked.append({"family": family, "template": str(template_path), "label": label})

        if label is None:
            violations.append(
                {
                    "family": family,
                    "kind": "template_unreadable",
                    "detail": f"could not read a Label out of {template_path}",
                }
            )
            continue

        if _PLACEHOLDER not in label:
            violations.append(
                {
                    "family": family,
                    "kind": "label_not_project_scoped",
                    "detail": (
                        f"{template_path.name} Label={label!r} does not contain "
                        f"{_PLACEHOLDER}; two projects installing this template "
                        "would resolve to the SAME launchd Label and collide "
                        "(OI-1509/OI-1510)"
                    ),
                }
            )

    return {"ok": not violations, "violations": violations, "checked": checked}


def check_installed_state(
    launchctl_output: str,
    project_id: str,
    families: Sequence[str] = REQUIRED_PER_PROJECT_FAMILIES,
) -> Dict[str, Any]:
    """Behavioral check against an injected ``launchctl list`` snapshot.

    A live collision (two projects fighting over one Label) cannot be
    observed as two simultaneous entries — launchd itself prevents that, the
    second ``load`` unloads the first. What IS observable, and is exactly the
    precondition that makes a future collision possible, is a bare
    (non-project-scoped) or malformed label still loaded for a family this
    project requires to be project-scoped. That, plus this project's own
    instance being absent, are the two failure modes checked here.
    """
    if not PROJECT_ID_RE.match(project_id):
        return {
            "ok": False,
            "violations": [
                {
                    "family": None,
                    "kind": "invalid_project_id",
                    "detail": f"project_id {project_id!r} does not match {PROJECT_ID_RE.pattern}",
                }
            ],
            "loaded_labels": [],
        }

    jobs = _parse_launchctl_list(launchctl_output)
    loaded_labels = [job["label"] for job in jobs]
    loaded_set = set(loaded_labels)

    violations: List[Dict[str, Any]] = []
    for family in families:
        expected_label = f"{family}.{project_id}"
        if expected_label not in loaded_set:
            violations.append(
                {
                    "family": family,
                    "kind": "missing_instance",
                    "detail": (
                        f"no loaded launchd job named {expected_label!r} — "
                        f"project {project_id!r} has no instance of this daemon"
                    ),
                }
            )

        for label in loaded_labels:
            if label == family:
                violations.append(
                    {
                        "family": family,
                        "kind": "non_per_project_label",
                        "detail": (
                            f"{label!r} is loaded bare (no project suffix) — "
                            f"collision risk for {family}"
                        ),
                    }
                )
                continue
            if label.startswith(family + "."):
                suffix = label[len(family) + 1 :]
                if not PROJECT_ID_RE.match(suffix):
                    violations.append(
                        {
                            "family": family,
                            "kind": "malformed_label",
                            "detail": f"{label!r} suffix {suffix!r} is not a valid project id",
                        }
                    )

    return {"ok": not violations, "violations": violations, "loaded_labels": loaded_labels}


def check_project_scope(
    templates_dir: Path,
    project_id: str,
    launchctl_output: str,
    families: Sequence[str] = REQUIRED_PER_PROJECT_FAMILIES,
) -> Dict[str, Any]:
    """Combine the static template contract and the live installed-state
    checks. Both an injected ``launchctl_output`` and an explicit
    ``project_id`` are required — this function never reads the host's real
    launchd state or environment itself (that boundary lives in ``main()``
    only), so it is fully deterministic under test.
    """
    template_check = check_template_contract(templates_dir, families)
    installed_check = check_installed_state(launchctl_output, project_id, families)
    violations = template_check["violations"] + installed_check["violations"]
    return {
        "ok": template_check["ok"] and installed_check["ok"],
        "violations": violations,
        "template_check": template_check,
        "installed_check": installed_check,
    }


class LaunchctlListFailedError(RuntimeError):
    """``launchctl list`` ran but exited non-zero. Distinct from
    ``OSError``/``subprocess.SubprocessError`` (binary missing, timeout):
    this means launchctl itself refused or errored on this specific
    invocation. Caught separately in ``main()`` so a non-zero exit is never
    silently treated as "queried successfully, nothing loaded" -- an
    absence must be MEASURED, never inferred from a failed measurement
    attempt (the exact class of bug the coordinator flagged in review: a
    prior version of this function returned ``result.stdout`` unconditionally,
    discarding ``result.returncode`` entirely)."""


def _run_real_launchctl_list() -> str:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise LaunchctlListFailedError(
            f"launchctl list exited {result.returncode}" + (f": {stderr}" if stderr else "")
        )
    return result.stdout or ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed guard: exits non-zero when a per-project launchd "
            "daemon (gate-obligation-runner, receipt-processor) is missing "
            "this project's instance, or when a bare/malformed label is "
            "loaded for it (OI-1509/OI-1510). Read-only: never installs, "
            "loads, or unloads a launchd job."
        )
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Override the resolved project id (default: vnx_paths.resolve_project_id())",
    )
    parser.add_argument(
        "--templates-dir",
        default=str(Path(__file__).resolve().parent),
        help="Directory containing the *.plist templates (default: this file's directory)",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        print(f"launchd_project_scope: skipped — platform {sys.platform!r} is not darwin (launchd is macOS-only)")
        return 0

    project_id = args.project_id or vnx_paths.resolve_project_id()
    if not project_id:
        print(
            "launchd_project_scope: cannot resolve a project id (no --project-id, "
            "no VNX_PROJECT_ID, no .vnx-project-id marker) — refusing to guess",
            file=sys.stderr,
        )
        return 2

    try:
        launchctl_output = _run_real_launchctl_list()
    except (OSError, subprocess.SubprocessError, LaunchctlListFailedError) as exc:
        print(f"launchd_project_scope: launchctl unavailable ({exc}) — cannot verify installed state", file=sys.stderr)
        return 2

    result = check_project_scope(Path(args.templates_dir), project_id, launchctl_output)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        status = "OK" if result["ok"] else "VIOLATIONS"
        print(f"launchd_project_scope[{project_id}]: {status}")
        for violation in result["violations"]:
            print(f"  - [{violation['kind']}] {violation.get('family')}: {violation['detail']}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
