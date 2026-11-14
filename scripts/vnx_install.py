#!/usr/bin/env python3
"""VNX Install Validator — prerequisite checking and install-time validation.

Validates that the host system meets VNX requirements before or after
installation. Provides actionable error messages for every failure.

Usage:
  python3 vnx_install.py --check          # Prereq check only (dry-run)
  python3 vnx_install.py --validate       # Validate existing installation
  python3 vnx_install.py --json           # JSON output for CI

Design:
  - All checks are read-only and idempotent.
  - Each check returns PASS/WARN/FAIL with remediation.
  - Supports both .vnx/ and .claude/vnx-system/ layouts.

Governance: G-R4 (public docs must match actual runtime behavior).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    category: str  # prereq | path | layout | install
    status: str  # pass | warn | fail
    message: str
    remediation: str = ""
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "message": self.message,
        }
        if self.remediation:
            d["remediation"] = self.remediation
        if self.details:
            d["details"] = self.details
        return d


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"
BOLD = "\033[1m"

STATUS_ICON = {
    PASS: f"{GREEN}PASS{RESET}",
    WARN: f"{YELLOW}WARN{RESET}",
    FAIL: f"{RED}FAIL{RESET}",
}


def _log(check: CheckResult) -> None:
    icon = STATUS_ICON.get(check.status, check.status)
    print(f"  [{icon}] {check.name}: {check.message}")
    for d in check.details:
        print(f"         {d}")
    if check.remediation and check.status != PASS:
        print(f"         {CYAN}Fix: {check.remediation}{RESET}")


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------

REQUIRED_TOOLS: List[Tuple[str, str, str]] = [
    ("python3", "3.9", "Python 3.9+ is required for VNX runtime"),
    ("bash", "3.2", "Bash 3.2+ is required for shell commands"),
    ("git", "2.20", "Git 2.20+ is required for worktree support"),
]

# Tools where a newer version is recommended but not required
UPGRADED_TOOLS: List[Tuple[str, str, str]] = [
    ("bash", "4.0", "Bash 4.0+ recommended for associative arrays and modern features"),
]

RECOMMENDED_TOOLS: List[Tuple[str, str]] = [
    ("jq", "JSON processing (used by some scripts)"),
    ("rg", "Fast code search (ripgrep)"),
    ("tmux", "Required for operator mode (multi-terminal grid)"),
    ("sqlite3", "Quality intelligence database CLI"),
]

OPTIONAL_PROVIDERS: List[Tuple[str, str]] = [
    ("claude", "Claude Code CLI (primary AI provider)"),
    ("codex", "Codex CLI (alternative provider for T1)"),
    ("gemini", "Gemini CLI (alternative provider for T1/T2)"),
]


def _get_tool_version(tool: str) -> Optional[str]:
    """Get version string from a tool, returns None if unavailable."""
    try:
        if tool == "python3":
            result = subprocess.run(
                ["python3", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            # "Python 3.11.5" -> "3.11.5"
            return result.stdout.strip().split()[-1] if result.returncode == 0 else None
        elif tool == "bash":
            result = subprocess.run(
                ["bash", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            # First line: "GNU bash, version 5.2.15..." -> extract version
            if result.returncode == 0:
                line = result.stdout.split("\n")[0]
                for part in line.split():
                    if part[0].isdigit():
                        return part.split("(")[0]
            return None
        elif tool == "git":
            result = subprocess.run(
                ["git", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            # "git version 2.39.0" -> "2.39.0"
            return result.stdout.strip().split()[-1] if result.returncode == 0 else None
        else:
            return "installed" if shutil.which(tool) else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _version_ge(actual: str, minimum: str) -> bool:
    """Check if actual version >= minimum version (simple numeric comparison)."""
    try:
        actual_parts = [int(x) for x in actual.split(".")[:3]]
        min_parts = [int(x) for x in minimum.split(".")[:3]]
        # Pad to equal length
        while len(actual_parts) < len(min_parts):
            actual_parts.append(0)
        while len(min_parts) < len(actual_parts):
            min_parts.append(0)
        return actual_parts >= min_parts
    except (ValueError, IndexError):
        return True  # If we can't parse, assume OK


def check_prerequisites() -> List[CheckResult]:
    """Check required and recommended tools."""
    results: List[CheckResult] = []

    # Required tools
    for tool, min_version, description in REQUIRED_TOOLS:
        path = shutil.which(tool)
        if not path:
            results.append(CheckResult(
                tool, "prereq", FAIL,
                f"Required tool not found: {tool}",
                f"Install {tool} ({description})",
            ))
            continue

        version = _get_tool_version(tool)
        if version and version != "installed" and not _version_ge(version, min_version):
            results.append(CheckResult(
                tool, "prereq", FAIL,
                f"{tool} {version} found, but {min_version}+ required",
                f"Upgrade {tool} to {min_version}+",
            ))
        else:
            version_str = f" ({version})" if version and version != "installed" else ""
            results.append(CheckResult(
                tool, "prereq", PASS,
                f"{tool}{version_str}: {path}",
            ))

    # Upgrade recommendations (warn, not fail)
    for tool, rec_version, description in UPGRADED_TOOLS:
        version = _get_tool_version(tool)
        if version and version != "installed" and not _version_ge(version, rec_version):
            results.append(CheckResult(
                f"{tool}-upgrade", "prereq", WARN,
                f"{tool} {version} works, but {rec_version}+ recommended",
                description,
            ))

    # Recommended tools
    for tool, description in RECOMMENDED_TOOLS:
        if shutil.which(tool):
            results.append(CheckResult(
                tool, "prereq", PASS,
                f"Optional: {tool} ({description})",
            ))
        else:
            results.append(CheckResult(
                tool, "prereq", WARN,
                f"Optional not found: {tool}",
                f"Install {tool} — {description}",
            ))

    # AI providers
    provider_found = False
    for tool, description in OPTIONAL_PROVIDERS:
        if shutil.which(tool):
            results.append(CheckResult(
                tool, "prereq", PASS,
                f"Provider: {tool} ({description})",
            ))
            provider_found = True

    if not provider_found:
        results.append(CheckResult(
            "ai-provider", "prereq", WARN,
            "No AI provider CLI found",
            "Install at least one: claude (Claude Code), codex (Codex CLI), or gemini (Gemini CLI)",
        ))

    return results


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------

def detect_layout(project_root: Path) -> Tuple[Optional[str], Optional[Path]]:
    """Detect VNX layout in a project directory.

    Returns (layout_name, vnx_home_path) or (None, None) if not found.
    """
    # Primary layout: .vnx/
    vnx_dir = project_root / ".vnx"
    if (vnx_dir / "bin" / "vnx").is_file():
        return "vnx", vnx_dir

    # Legacy layout: .claude/vnx-system/
    claude_vnx = project_root / ".claude" / "vnx-system"
    if (claude_vnx / "bin" / "vnx").is_file():
        return "claude", claude_vnx

    return None, None


def check_layout(project_root: Path) -> List[CheckResult]:
    """Check VNX layout detection and validity."""
    results: List[CheckResult] = []

    layout, vnx_home = detect_layout(project_root)
    if layout is None:
        results.append(CheckResult(
            "layout", "layout", FAIL,
            f"No VNX installation found in {project_root}",
            "Run install.sh to install VNX, or check the project path",
        ))
        return results

    results.append(CheckResult(
        "layout", "layout", PASS,
        f"Layout: {layout} ({vnx_home})",
    ))

    # Check layout marker
    layout_file = vnx_home / ".layout"
    if layout_file.exists():
        stored_layout = layout_file.read_text().strip()
        if stored_layout == layout:
            results.append(CheckResult(
                "layout-marker", "layout", PASS,
                f"Layout marker matches: {layout}",
            ))
        else:
            results.append(CheckResult(
                "layout-marker", "layout", WARN,
                f"Layout marker says '{stored_layout}' but detected '{layout}'",
            ))
    else:
        results.append(CheckResult(
            "layout-marker", "layout", WARN,
            "No .layout marker file (older installation?)",
        ))

    return results


# ---------------------------------------------------------------------------
# Installation validation
# ---------------------------------------------------------------------------

def validate_installation(project_root: Path) -> List[CheckResult]:
    """Validate an existing VNX installation."""
    results: List[CheckResult] = []

    layout, vnx_home = detect_layout(project_root)
    if layout is None or vnx_home is None:
        results.append(CheckResult(
            "install", "install", FAIL,
            "No VNX installation to validate",
            "Run install.sh first",
        ))
        return results

    # Check critical directories
    critical_dirs = [
        ("bin", "CLI entrypoint"),
        ("scripts", "Runtime scripts"),
        ("scripts/lib", "Shared libraries"),
        ("templates", "Terminal templates"),
        ("templates/terminals", "Terminal CLAUDE.md templates"),
    ]
    for rel_path, description in critical_dirs:
        dir_path = vnx_home / rel_path
        if dir_path.is_dir():
            results.append(CheckResult(
                f"dir-{rel_path}", "install", PASS,
                f"{description}: {rel_path}",
            ))
        else:
            results.append(CheckResult(
                f"dir-{rel_path}", "install", FAIL,
                f"Missing: {rel_path} ({description})",
                "Re-run install.sh to repair",
            ))

    # Check critical files
    critical_files = [
        ("bin/vnx", "CLI entrypoint script"),
        ("scripts/vnx_init.py", "Python init orchestrator"),
        ("scripts/vnx_doctor.py", "Python doctor"),
        ("scripts/lib/vnx_paths.py", "Path resolver"),
        ("scripts/lib/vnx_paths.sh", "Shell path resolver"),
        ("scripts/lib/vnx_mode.py", "Mode management"),
    ]
    for rel_path, description in critical_files:
        file_path = vnx_home / rel_path
        if file_path.is_file():
            results.append(CheckResult(
                f"file-{rel_path}", "install", PASS,
                f"{description}",
            ))
        else:
            results.append(CheckResult(
                f"file-{rel_path}", "install", FAIL,
                f"Missing: {rel_path} ({description})",
                "Re-run install.sh to repair",
            ))

    # Check bin/vnx is executable
    vnx_bin = vnx_home / "bin" / "vnx"
    if vnx_bin.exists() and os.access(str(vnx_bin), os.X_OK):
        results.append(CheckResult(
            "executable", "install", PASS,
            "bin/vnx is executable",
        ))
    elif vnx_bin.exists():
        results.append(CheckResult(
            "executable", "install", FAIL,
            "bin/vnx exists but is not executable",
            f"Run: chmod +x {vnx_bin}",
        ))

    # Check terminal templates
    for tid in ["T0", "T1", "T2", "T3"]:
        tmpl = vnx_home / "templates" / "terminals" / f"{tid}.md"
        if tmpl.is_file():
            results.append(CheckResult(
                f"template-{tid}", "install", PASS,
                f"Terminal template: {tid}",
            ))
        else:
            results.append(CheckResult(
                f"template-{tid}", "install", FAIL,
                f"Missing template: templates/terminals/{tid}.md",
                "Re-run install.sh to repair",
            ))

    # Check if runtime data directory exists (post-init)
    data_dir = project_root / ".vnx-data"
    if data_dir.is_dir():
        results.append(CheckResult(
            "runtime-data", "install", PASS,
            f"Runtime data: {data_dir}",
        ))
    else:
        results.append(CheckResult(
            "runtime-data", "install", WARN,
            "No .vnx-data/ (not yet initialized)",
            "Run: vnx init (or vnx setup)",
        ))

    # Check mode.json
    mode_file = data_dir / "mode.json"
    if mode_file.is_file():
        try:
            with open(mode_file) as f:
                mode_data = json.load(f)
            mode = mode_data.get("mode", "unknown")
            results.append(CheckResult(
                "mode", "install", PASS,
                f"Mode: {mode}",
            ))
        except (json.JSONDecodeError, OSError):
            results.append(CheckResult(
                "mode", "install", WARN,
                "mode.json exists but is unreadable",
            ))
    elif data_dir.is_dir():
        results.append(CheckResult(
            "mode", "install", WARN,
            "No mode.json (run 'vnx init --starter' or 'vnx init --operator')",
        ))

    return results


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

def check_path_sanity(project_root: Path) -> List[CheckResult]:
    """Check for common path problems."""
    results: List[CheckResult] = []

    # Check for spaces in path
    path_str = str(project_root)
    if " " in path_str:
        results.append(CheckResult(
            "path-spaces", "path", WARN,
            f"Project path contains spaces: {path_str}",
            "Some tools may have issues with spaces in paths",
        ))

    # Check path length
    if len(path_str) > 200:
        results.append(CheckResult(
            "path-length", "path", WARN,
            f"Project path is very long ({len(path_str)} chars)",
            "Deeply nested paths may cause issues with some tools",
        ))

    # Check write access
    test_file = project_root / ".vnx-install-test"
    try:
        test_file.write_text("test")
        test_file.unlink()
        results.append(CheckResult(
            "write-access", "path", PASS,
            f"Write access: {project_root}",
        ))
    except OSError:
        results.append(CheckResult(
            "write-access", "path", FAIL,
            f"Cannot write to: {project_root}",
            f"Check permissions on {project_root}",
        ))

    # Check git repo
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            results.append(CheckResult(
                "git-repo", "path", PASS,
                "Inside a git repository",
            ))
        else:
            results.append(CheckResult(
                "git-repo", "path", WARN,
                "Not a git repository",
                "VNX works best inside a git repository (worktrees, provenance)",
            ))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        results.append(CheckResult(
            "git-repo", "path", WARN,
            "Could not check git status",
        ))

    if not results:
        results.append(CheckResult(
            "path", "path", PASS,
            f"Path OK: {project_root}",
        ))

    return results


# ---------------------------------------------------------------------------
# Invocation mode documentation
# ---------------------------------------------------------------------------

INVOCATION_MODES = """
VNX Invocation Modes
=====================

1. Direct path (any shell):
   .vnx/bin/vnx <command>
   .claude/vnx-system/bin/vnx <command>

2. Shell helper function (recommended):
   vnx install-shell-helper     # one-time setup
   vnx <command>                # auto-resolves from any subdirectory

3. Shell alias (manual):
   alias vnx='/path/to/project/.vnx/bin/vnx'

4. PATH addition (manual):
   export PATH="/path/to/project/.vnx/bin:$PATH"

Install Modes
=============

1. Default (.vnx/ layout):
   bash install.sh /path/to/project

2. Hidden (.claude/vnx-system/ layout):
   bash install.sh /path/to/project --layout claude

Post-Install
============

   vnx setup               # One-command: init + doctor + register
   vnx setup --starter     # Starter mode (no tmux)
   vnx setup --operator    # Operator mode (full grid)
"""


def print_invocation_modes() -> None:
    """Print supported invocation modes."""
    print(INVOCATION_MODES)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_checks(
    project_root: Optional[Path] = None,
    prereqs_only: bool = False,
    validate: bool = False,
) -> List[CheckResult]:
    """Run all install checks."""
    results: List[CheckResult] = []

    results.extend(check_prerequisites())

    if prereqs_only:
        return results

    if project_root is None:
        # Try to detect from environment or CWD
        env_root = os.environ.get("PROJECT_ROOT")
        if env_root:
            project_root = Path(env_root)
        else:
            project_root = Path.cwd()

    results.extend(check_path_sanity(project_root))
    results.extend(check_layout(project_root))

    if validate:
        results.extend(validate_installation(project_root))

    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="VNX Install Validator — prerequisite and installation checks",
    )
    parser.add_argument("--check", action="store_true",
                        help="Check prerequisites only (dry-run, no project needed)")
    parser.add_argument("--validate", action="store_true",
                        help="Validate an existing VNX installation")
    parser.add_argument("--project-root", type=str,
                        help="Project root directory (default: CWD or $PROJECT_ROOT)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    parser.add_argument("--invocation-modes", action="store_true",
                        help="Print supported invocation modes and exit")
    args = parser.parse_args()

    if args.invocation_modes:
        print_invocation_modes()
        return 0

    project_root = Path(args.project_root) if args.project_root else None
    results = run_checks(
        project_root=project_root,
        prereqs_only=args.check,
        validate=args.validate,
    )

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print(f"\n{BOLD}VNX Install Validator{RESET}")
        print(f"{'─' * 50}")

        # Group by category
        categories = {}
        for r in results:
            categories.setdefault(r.category, []).append(r)

        category_labels = {
            "prereq": "Prerequisites",
            "path": "Path Validation",
            "layout": "Layout Detection",
            "install": "Installation",
        }

        for cat in ["prereq", "path", "layout", "install"]:
            checks = categories.get(cat, [])
            if not checks:
                continue
            print(f"\n{BOLD}{category_labels.get(cat, cat)}{RESET}")
            for r in checks:
                _log(r)

        fails = sum(1 for r in results if r.status == FAIL)
        warns = sum(1 for r in results if r.status == WARN)
        passes = sum(1 for r in results if r.status == PASS)

        print(f"\n{'─' * 50}")
        if fails:
            print(f"{RED}FAILED{RESET} — {passes} passed, {warns} warnings, {fails} failures")
            if not args.validate:
                print(f"\nFix the failures above before installing VNX.")
        elif warns:
            print(f"{YELLOW}READY with warnings{RESET} — {passes} passed, {warns} warnings")
        else:
            print(f"{GREEN}READY{RESET} — {passes} checks OK")

    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
