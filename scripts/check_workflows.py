#!/usr/bin/env python3
"""GitHub Actions workflow validation gate (OI-1222).

Lints every ``.github/workflows/*.yml`` (and ``*.yaml``) for the two failure
classes that must not slip through local CI onto GitHub:

1. YAML validity — the file parses at all.
2. GitHub-Actions-specific shape — workflow/job/step structure, ``on:`` keys,
   and expression/context references (``${{ ... }}``), via ``actionlint``.

A YAML-valid file that GitHub still refuses (unknown context property, invalid
``on:`` event) must fail locally too — parsing YAML alone is not enough. If
``actionlint`` is not installed, the gate FAILS with an install instruction
instead of silently skipping. A silently-skipped validation step is the exact
defect this gate closes: an invalid workflow ships, GitHub produces zero runs,
and every check that only looks for red reads that as green.

See: dispatch OI-1222.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ACTIONLINT_INSTALL_HINT = (
    "actionlint is not installed. Install it before running local-ci, e.g.:\n"
    "    brew install actionlint              # macOS (Homebrew)\n"
    "    go install github.com/rhysd/actionlint/cmd/actionlint@latest\n"
    "    or download a prebuilt binary from https://github.com/rhysd/actionlint/releases\n"
    "The workflow-validate gate refuses to run without it: a silent skip is\n"
    "exactly the failure mode this gate exists to close."
)


def workflow_files(root: Path) -> list[Path]:
    """Return the sorted workflow files under ``.github/workflows/``."""
    workflows_dir = root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []
    files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def validate_yaml(path: Path) -> list[str]:
    """Return YAML parse errors for a single workflow file (empty when valid)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path}: cannot read file: {exc}"]
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"{path}: invalid YAML: {exc}"]
    return []


def find_actionlint() -> Path | None:
    """Return the ``actionlint`` binary path, or None when not installed."""
    found = shutil.which("actionlint")
    return Path(found) if found else None


def _format_finding(obj: dict) -> str:
    """Render one actionlint JSON finding as a single, readable string."""
    loc = (
        f"{obj.get('filepath', '?')}:"
        f"{obj.get('line', '?')}:{obj.get('column', '?')}"
    )
    out = f"{loc}: {obj.get('message', '?')} [{obj.get('kind', '?')}]"
    snippet = obj.get("snippet", "")
    if snippet:
        out += "\n" + "\n".join("      " + s for s in snippet.splitlines())
    return out


def run_actionlint(
    files: list[Path], binary: Path, cwd: Path | None = None
) -> list[str]:
    """Run actionlint over ``files`` and return one entry per finding.

    ``files`` may be relative to ``cwd`` for shorter, repo-relative output.
    actionlint is asked for JSON (one object per finding) so the gate can
    report an accurate count instead of one line per snippet row.
    """
    try:
        result = subprocess.run(
            [str(binary), "-format", "{{json .}}", *[str(f) for f in files]],
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
        )
    except OSError as exc:
        return [f"actionlint failed to run ({binary}): {exc}"]
    except subprocess.TimeoutExpired:
        return ["actionlint timed out after 120s"]

    findings: list[str] = []
    raw = result.stdout.strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Non-JSON output (unexpected) — surface the raw lines verbatim.
            findings.extend(
                line.rstrip() for line in result.stdout.splitlines() if line.strip()
            )
        else:
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        findings.append(_format_finding(item))
                    else:
                        findings.append(str(item))
    if result.stderr.strip():
        findings.append(f"actionlint stderr: {result.stderr.strip()}")
    return findings


def check_workflows(root: Path, actionlint_binary: Path | None = None) -> list[str]:
    """Return all findings for the repo's workflows (empty when all green).

    Combines the YAML parse layer with the actionlint layer. When actionlint is
    unavailable this returns the install-instruction error so the caller
    (``local-ci.sh``) reports a red gate instead of a silent pass.
    """
    files = workflow_files(root)
    if not files:
        return []  # nothing to validate — no workflow files in this tree

    findings: list[str] = []
    for path in files:
        findings.extend(validate_yaml(path))

    binary = actionlint_binary if actionlint_binary is not None else find_actionlint()
    if binary is None:
        findings.append(f"[actionlint missing] {ACTIONLINT_INSTALL_HINT}")
        return findings

    findings.extend(run_actionlint(files, binary, cwd=root))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0]).resolve() if args else Path.cwd()

    if not workflow_files(root):
        print("[workflow-validate] skip — no workflow files under .github/workflows/")
        return 0

    findings = check_workflows(root)

    if not findings:
        print(
            "[workflow-validate] PASS — all workflow files are valid YAML "
            "and pass actionlint."
        )
        return 0

    print(f"[workflow-validate] FAIL — {len(findings)} finding(s):\n")
    for finding in findings:
        print(f"  {finding}")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
