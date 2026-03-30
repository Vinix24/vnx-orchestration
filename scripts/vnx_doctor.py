#!/usr/bin/env python3
"""VNX Doctor — Python-led installation and runtime health validator.

Consolidates static installation checks (tools, paths, directories,
settings) from doctor.sh and runtime checks from vnx_doctor_runtime.py
into a single deterministic entrypoint with structured output.

Design:
  - All checks are read-only and idempotent.
  - Each check returns PASS/WARN/FAIL with actionable remediation.
  - Worktree detection and validation is built-in.
  - JSON output for CI integration.

Governance: G-R5 (every simplification needs QA evidence).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: str  # pass | warn | fail
    message: str
    remediation: str = ""
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"name": self.name, "status": self.status, "message": self.message}
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
# Tool checks
# ---------------------------------------------------------------------------

REQUIRED_TOOLS = ["bash", "python3"]
RECOMMENDED_TOOLS = ["rg", "jq", "tmux", "codex", "gemini", "sqlite3"]


def check_tools() -> List[CheckResult]:
    results = []
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool):
            results.append(CheckResult("tool", PASS, f"Required: {tool}"))
        else:
            results.append(CheckResult("tool", FAIL, f"Missing required: {tool}",
                                       f"Install {tool}"))

    for tool in RECOMMENDED_TOOLS:
        if shutil.which(tool):
            results.append(CheckResult("tool", PASS, f"Optional: {tool}"))
        else:
            results.append(CheckResult("tool", WARN, f"Optional not found: {tool}"))

    return results


# ---------------------------------------------------------------------------
# Path resolution checks
# ---------------------------------------------------------------------------

def check_path_resolution(paths: Dict[str, str]) -> List[CheckResult]:
    results = []
    project_root = Path(paths["PROJECT_ROOT"])
    canonical_root = Path(paths.get("VNX_CANONICAL_ROOT", paths["VNX_HOME"]))
    intelligence_dir = Path(paths["VNX_INTELLIGENCE_DIR"])

    if project_root.is_dir():
        results.append(CheckResult("path", PASS, f"Runtime root: {project_root}"))
    else:
        results.append(CheckResult("path", FAIL, f"Runtime root missing: {project_root}"))

    if canonical_root.is_dir():
        results.append(CheckResult("path", PASS, f"Canonical root: {canonical_root}"))
    else:
        results.append(CheckResult("path", FAIL, f"Canonical root missing: {canonical_root}"))

    results.append(CheckResult("path", PASS, f"Intelligence dir: {intelligence_dir}"))

    # Node path (for MCP servers in tmux)
    node_path = _resolve_node_path()
    if node_path and (Path(node_path) / "node").exists():
        results.append(CheckResult("path", PASS, f"Node: {node_path}"))
    else:
        results.append(CheckResult("path", WARN,
                                   "Node path not resolved. MCP servers may fail in tmux.",
                                   "Set VNX_NODE_PATH or install nvm"))

    # Python venv
    venv_path = _resolve_venv_path(paths)
    if venv_path and Path(venv_path).exists():
        results.append(CheckResult("path", PASS, f"Venv: {venv_path}"))
    else:
        results.append(CheckResult("path", WARN,
                                   "Python venv not found. Quality services may use system python."))

    return results


def _resolve_node_path() -> Optional[str]:
    """Find node binary directory."""
    if os.environ.get("VNX_NODE_PATH"):
        return os.environ["VNX_NODE_PATH"]
    node = shutil.which("node")
    if node:
        return str(Path(node).parent)
    # Try nvm default
    nvm_dir = os.environ.get("NVM_DIR", str(Path.home() / ".nvm"))
    default = Path(nvm_dir) / "alias" / "default"
    if default.exists():
        version = default.read_text().strip()
        candidate = Path(nvm_dir) / "versions" / "node" / version / "bin"
        if candidate.is_dir():
            return str(candidate)
    return None


def _resolve_venv_path(paths: Dict[str, str]) -> Optional[str]:
    """Find Python venv activate script."""
    project_root = Path(paths["PROJECT_ROOT"])
    for venv_name in [".venv", "venv", ".vnx-venv"]:
        activate = project_root / venv_name / "bin" / "activate"
        if activate.exists():
            return str(activate)
    return None


# ---------------------------------------------------------------------------
# Directory checks
# ---------------------------------------------------------------------------

def check_directories(paths: Dict[str, str]) -> List[CheckResult]:
    results = []

    required_dirs = [
        ("VNX config", Path(paths["PROJECT_ROOT"]) / ".vnx"),
        ("Runtime data", Path(paths["VNX_DATA_DIR"])),
        ("State", Path(paths["VNX_STATE_DIR"])),
        ("Logs", Path(paths["VNX_LOGS_DIR"])),
        ("PIDs", Path(paths["VNX_PIDS_DIR"])),
        ("Locks", Path(paths["VNX_LOCKS_DIR"])),
        ("Dispatches", Path(paths["VNX_DISPATCH_DIR"])),
        ("Dispatches/pending", Path(paths["VNX_DISPATCH_DIR"]) / "pending"),
        ("Dispatches/active", Path(paths["VNX_DISPATCH_DIR"]) / "active"),
        ("Dispatches/completed", Path(paths["VNX_DISPATCH_DIR"]) / "completed"),
        ("Reports", Path(paths["VNX_REPORTS_DIR"])),
    ]

    for label, dir_path in required_dirs:
        if dir_path.is_dir():
            results.append(CheckResult("dir", PASS, f"{label}: {dir_path}"))
        else:
            results.append(CheckResult("dir", FAIL, f"Missing: {label} ({dir_path})",
                                       "Run: vnx init"))

    # Config file
    config_file = Path(paths["PROJECT_ROOT"]) / ".vnx" / "config.yml"
    if config_file.exists():
        results.append(CheckResult("file", PASS, f"Config: {config_file}"))
    else:
        results.append(CheckResult("file", FAIL, f"Missing config: {config_file}",
                                   "Run: vnx init"))

    return results


# ---------------------------------------------------------------------------
# Template checks
# ---------------------------------------------------------------------------

def check_templates(paths: Dict[str, str]) -> List[CheckResult]:
    results = []
    vnx_home = Path(paths["VNX_HOME"])
    templates_dir = vnx_home / "templates" / "terminals"

    for tid in ["T0", "T1", "T2", "T3"]:
        tmpl = templates_dir / f"{tid}.md"
        if tmpl.exists():
            results.append(CheckResult("template", PASS, f"Terminal template: {tid}"))
        else:
            results.append(CheckResult("template", FAIL, f"Missing template: {tmpl}"))

    skills_yaml = Path(paths.get("VNX_SKILLS_DIR", vnx_home / "skills")) / "skills.yaml"
    if skills_yaml.exists():
        results.append(CheckResult("template", PASS, "Skills registry present"))
    else:
        results.append(CheckResult("template", FAIL, f"Missing: {skills_yaml}",
                                   "Run: vnx bootstrap-skills"))

    return results


# ---------------------------------------------------------------------------
# Settings checks
# ---------------------------------------------------------------------------

def check_settings(paths: Dict[str, str]) -> List[CheckResult]:
    results = []
    project_root = Path(paths["PROJECT_ROOT"])
    settings_file = project_root / ".claude" / "settings.json"

    if not settings_file.exists():
        return [CheckResult("settings", FAIL,
                            "Missing .claude/settings.json",
                            "Run: vnx regen-settings --full")]

    # JSON validity
    try:
        with open(settings_file) as f:
            settings = json.load(f)
        results.append(CheckResult("settings", PASS, "Valid JSON"))
    except json.JSONDecodeError as e:
        return [CheckResult("settings", FAIL, f"Invalid JSON: {e}",
                            "Fix syntax in .claude/settings.json")]

    # Structure validation via merge engine
    vnx_home = Path(paths["VNX_HOME"])
    validate_script = vnx_home / "scripts" / "vnx_settings_merge.py"
    if validate_script.exists():
        try:
            subprocess.run(
                [sys.executable, str(validate_script), "--validate",
                 "--project-root", str(project_root),
                 "--vnx-home", str(vnx_home)],
                capture_output=True, timeout=10, check=True,
            )
            results.append(CheckResult("settings", PASS, "Structure valid"))
        except subprocess.CalledProcessError:
            results.append(CheckResult("settings", FAIL,
                                       "Structure validation failed",
                                       "Run: vnx regen-settings --full"))

    # Hooks section
    if "hooks" in settings:
        results.append(CheckResult("settings", PASS, "Hooks section present"))
    else:
        results.append(CheckResult("settings", FAIL, "Missing hooks section",
                                   "Run: vnx bootstrap-hooks"))

    # Permissions section
    if "permissions" in settings:
        results.append(CheckResult("settings", PASS, "Permissions section present"))
    else:
        results.append(CheckResult("settings", FAIL, "Missing permissions section",
                                   "Run: vnx regen-settings --full"))

    return results


# ---------------------------------------------------------------------------
# Hooks check
# ---------------------------------------------------------------------------

def check_hooks(paths: Dict[str, str]) -> List[CheckResult]:
    hook_file = Path(paths["PROJECT_ROOT"]) / ".claude" / "hooks" / "sessionstart.sh"
    if hook_file.exists():
        return [CheckResult("hooks", PASS, f"SessionStart hook: {hook_file}")]
    return [CheckResult("hooks", FAIL, f"Missing hook: {hook_file}",
                        "Run: vnx bootstrap-hooks")]


# ---------------------------------------------------------------------------
# Database check
# ---------------------------------------------------------------------------

def check_database(paths: Dict[str, str]) -> List[CheckResult]:
    db_path = Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"

    if not db_path.exists():
        return [CheckResult("database", WARN,
                            "Quality intelligence DB not found",
                            "Run: vnx init-db")]

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        )
        table_count = cursor.fetchone()[0]
        conn.close()

        if table_count >= 10:
            return [CheckResult("database", PASS,
                                f"Quality DB: {table_count} tables")]
        return [CheckResult("database", FAIL,
                            f"Quality DB incomplete: {table_count} tables (expected >= 10)",
                            "Run: vnx init-db")]
    except sqlite3.Error as e:
        return [CheckResult("database", FAIL, f"Cannot read DB: {e}")]


# ---------------------------------------------------------------------------
# Write access check
# ---------------------------------------------------------------------------

def check_write_access(paths: Dict[str, str]) -> CheckResult:
    state_dir = Path(paths["VNX_STATE_DIR"])
    test_file = state_dir / ".vnx_doctor_write_test"
    try:
        test_file.write_text("test")
        test_file.unlink()
        return CheckResult("access", PASS, f"Write access: {state_dir}")
    except OSError:
        return CheckResult("access", FAIL,
                           f"Cannot write to: {state_dir}",
                           f"Check permissions on {state_dir}")


# ---------------------------------------------------------------------------
# Worktree checks
# ---------------------------------------------------------------------------

def detect_worktree() -> Tuple[bool, Optional[str], Optional[str]]:
    """Detect if CWD is a git worktree. Returns (is_worktree, wt_root, main_root)."""
    try:
        common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        if common_dir and toplevel and common_dir != toplevel:
            # Normalize: git-common-dir ends with /.git, strip it
            main_root = common_dir[:-5] if common_dir.endswith("/.git") else common_dir
            return True, toplevel, main_root
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False, None, None


def check_worktree(paths: Dict[str, str]) -> List[CheckResult]:
    is_wt, wt_root, main_root = detect_worktree()
    if not is_wt:
        return []

    results = [CheckResult("worktree", PASS, f"Running in worktree: {wt_root}")]
    wt_data = Path(wt_root) / ".vnx-data"

    if wt_data.is_symlink():
        results.append(CheckResult("worktree", WARN,
                                   ".vnx-data is a SYMLINK (old model)",
                                   "Run: vnx worktree-start"))
    elif wt_data.is_dir():
        snapshot_meta = wt_data / ".snapshot_meta"
        if snapshot_meta.exists():
            results.append(CheckResult("worktree", PASS, "Isolated .vnx-data with snapshot"))

            # Check snapshot freshness
            try:
                meta = {}
                for line in snapshot_meta.read_text().splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        meta[k.strip()] = v.strip()
                snap_date = meta.get("snapshot_date", "")
                if snap_date:
                    snap_dt = datetime.fromisoformat(snap_date.replace("Z", "+00:00"))
                    days_old = (datetime.now(timezone.utc) - snap_dt).days
                    if days_old >= 14:
                        results.append(CheckResult("worktree", WARN,
                                                   f"Snapshot is {days_old} days old",
                                                   "Run: vnx worktree-refresh"))
            except (ValueError, KeyError):
                pass

            # Check .env_override
            if not (wt_data / ".env_override").exists():
                results.append(CheckResult("worktree", WARN,
                                           "Missing .env_override in worktree",
                                           "Run: vnx worktree-start"))
            else:
                results.append(CheckResult("worktree", PASS, ".env_override present"))
        else:
            results.append(CheckResult("worktree", WARN,
                                       ".vnx-data exists but no snapshot metadata",
                                       "Run: vnx worktree-start"))
    else:
        results.append(CheckResult("worktree", WARN,
                                   "No .vnx-data in worktree",
                                   "Run: vnx worktree-start"))

    return results


# ---------------------------------------------------------------------------
# Version check
# ---------------------------------------------------------------------------

def check_version(paths: Dict[str, str]) -> List[CheckResult]:
    vnx_home = Path(paths["VNX_HOME"])
    origin_file = vnx_home / ".vnx-origin"

    if not origin_file.exists():
        return []

    version_lock = vnx_home / "version.lock"
    if version_lock.exists():
        version = version_lock.read_text().strip().split("\n")[0].strip()
        return [CheckResult("version", PASS, f"Pinned to {version}")]
    return [CheckResult("version", PASS, "No version lock (unpinned)",
                        "Run: vnx update --pin <tag>")]


# ---------------------------------------------------------------------------
# Path hygiene check
# ---------------------------------------------------------------------------

def check_path_hygiene(paths: Dict[str, str]) -> List[CheckResult]:
    vnx_home = Path(paths["VNX_HOME"])
    check_script = vnx_home / "scripts" / "vnx_doctor.sh"

    if not check_script.exists():
        return [CheckResult("hygiene", WARN, "Path hygiene script missing")]

    try:
        subprocess.run(
            ["bash", str(check_script)],
            capture_output=True, timeout=10, check=True,
            env={**os.environ, **{k: v for k, v in paths.items()}},
        )
        return [CheckResult("hygiene", PASS, "Path hygiene OK")]
    except subprocess.CalledProcessError:
        return [CheckResult("hygiene", FAIL, "Path hygiene check failed")]


# ---------------------------------------------------------------------------
# Runtime checks (delegates to vnx_doctor_runtime.py)
# ---------------------------------------------------------------------------

def check_runtime(paths: Dict[str, str], verbose: bool = False,
                  json_output: bool = False, preflight: bool = False) -> List[CheckResult]:
    runtime_script = Path(paths["VNX_HOME"]) / "scripts" / "lib" / "vnx_doctor_runtime.py"
    if not runtime_script.exists():
        return [CheckResult("runtime", WARN, "Runtime check script not found")]

    args_list = [sys.executable, str(runtime_script),
                 "--state-dir", paths["VNX_STATE_DIR"]]
    if json_output:
        args_list.append("--json")
    elif preflight:
        args_list.append("--preflight-only")
    elif verbose:
        args_list.append("--verbose")

    try:
        result = subprocess.run(
            args_list,
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONPATH": str(SCRIPT_DIR / "lib")},
        )

        if result.returncode == 0:
            return [CheckResult("runtime", PASS, "Runtime checks passed",
                                details=result.stdout.strip().split("\n")[:10] if verbose else [])]
        return [CheckResult("runtime", FAIL, "Runtime checks failed",
                            details=result.stdout.strip().split("\n")[:10])]
    except subprocess.TimeoutExpired:
        return [CheckResult("runtime", FAIL, "Runtime checks timed out")]


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_doctor(paths: Dict[str, str], *,
               package_check: bool = False,
               runtime: bool = False,
               runtime_verbose: bool = False,
               runtime_json: bool = False,
               runtime_preflight: bool = False) -> List[CheckResult]:
    """Run all doctor checks and return structured results."""
    results: List[CheckResult] = []

    results.extend(check_tools())
    results.extend(check_path_resolution(paths))
    results.extend(check_directories(paths))
    results.extend(check_templates(paths))
    results.extend(check_hooks(paths))
    results.extend(check_settings(paths))
    results.extend(check_database(paths))
    results.append(check_write_access(paths))
    results.extend(check_worktree(paths))
    results.extend(check_version(paths))
    results.extend(check_path_hygiene(paths))

    if package_check:
        vnx_home = Path(paths["VNX_HOME"])
        pkg_script = vnx_home / "scripts" / "vnx_package_check.sh"
        if pkg_script.exists():
            try:
                subprocess.run(["bash", str(pkg_script)],
                               capture_output=True, timeout=10, check=True)
                results.append(CheckResult("package", PASS,
                                           "No runtime directories in dist"))
            except subprocess.CalledProcessError:
                results.append(CheckResult("package", FAIL,
                                           "Runtime artifacts found in dist"))

    if runtime or runtime_verbose or runtime_json or runtime_preflight:
        results.extend(check_runtime(
            paths, verbose=runtime_verbose,
            json_output=runtime_json, preflight=runtime_preflight,
        ))

    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="VNX Doctor — installation and runtime health")
    parser.add_argument("--package-check", action="store_true",
                        help="Include package hygiene check")
    parser.add_argument("--runtime", action="store_true",
                        help="Include runtime health checks")
    parser.add_argument("--runtime-verbose", action="store_true",
                        help="Verbose runtime checks")
    parser.add_argument("--runtime-json", action="store_true",
                        help="Runtime checks as JSON")
    parser.add_argument("--preflight", action="store_true",
                        help="Runtime preflight only")
    parser.add_argument("--json", action="store_true",
                        help="Output all results as JSON")
    args = parser.parse_args()

    paths = ensure_env()

    results = run_doctor(
        paths,
        package_check=args.package_check,
        runtime=args.runtime or args.runtime_verbose or args.runtime_json or args.preflight,
        runtime_verbose=args.runtime_verbose,
        runtime_json=args.runtime_json,
        runtime_preflight=args.preflight,
    )

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        # Group by category
        print(f"\n{BOLD}VNX Doctor{RESET}")
        print(f"{'─' * 50}")
        for r in results:
            _log(r)

        fails = sum(1 for r in results if r.status == FAIL)
        warns = sum(1 for r in results if r.status == WARN)
        passes = sum(1 for r in results if r.status == PASS)

        print(f"\n{'─' * 50}")
        if fails:
            print(f"{RED}FAILED{RESET} — {passes} passed, {warns} warnings, {fails} failures")
        elif warns:
            print(f"{YELLOW}PASSED with warnings{RESET} — {passes} passed, {warns} warnings")
        else:
            print(f"{GREEN}PASSED{RESET} — {passes} checks OK")

    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
