#!/usr/bin/env python3
"""VNX Init — Python-led init/bootstrap orchestrator.

Unifies init, bootstrap-skills, bootstrap-terminals, bootstrap-hooks,
init-db, and intelligence-import under a single deterministic Python
entrypoint. Replaces the bash cmd_init() chain with structured output
and explicit error reporting.

Design:
  - Idempotent: safe to re-run at any time.
  - Each step reports PASS/SKIP/FAIL with actionable detail.
  - Path resolution uses vnx_paths.py (canonical Python resolver).
  - Shell sub-commands are called only where Python cannot replace them
    (e.g., regen-settings merge that sources bash libs).

Governance: G-R2 (receipts and runtime state even in simplified flows).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

PASS = "pass"
SKIP = "skip"
FAIL = "fail"


@dataclass
class StepResult:
    name: str
    status: str  # pass | skip | fail
    message: str
    details: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
BOLD = "\033[1m"

STATUS_ICON = {
    PASS: f"{GREEN}OK{RESET}",
    SKIP: f"{YELLOW}SKIP{RESET}",
    FAIL: f"{RED}FAIL{RESET}",
}


def _log(step: StepResult) -> None:
    icon = STATUS_ICON.get(step.status, step.status)
    print(f"[init] [{icon}] {step.name}: {step.message}")
    for d in step.details:
        print(f"       {d}")


# ---------------------------------------------------------------------------
# Step: runtime layout
# ---------------------------------------------------------------------------

def ensure_runtime_layout(paths: Dict[str, str]) -> StepResult:
    """Create .vnx-data directory tree."""
    data_dir = Path(paths["VNX_DATA_DIR"])
    dispatch_dir = Path(paths["VNX_DISPATCH_DIR"])

    dirs = [
        data_dir,
        Path(paths["VNX_STATE_DIR"]),
        Path(paths["VNX_LOGS_DIR"]),
        Path(paths["VNX_PIDS_DIR"]),
        Path(paths["VNX_LOCKS_DIR"]),
        dispatch_dir / "pending",
        dispatch_dir / "active",
        dispatch_dir / "completed",
        dispatch_dir / "rejected",
        dispatch_dir / "failed",
        Path(paths["VNX_REPORTS_DIR"]),
        Path(paths["VNX_DB_DIR"]),
        data_dir / "receipts",
        data_dir / "profiles",
        data_dir / "startup_presets",
    ]

    created = []
    for d in dirs:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))

    if created:
        return StepResult("runtime-layout", PASS, f"Created {len(created)} directories",
                          [f"  + {c}" for c in created[:5]])
    return StepResult("runtime-layout", SKIP, "All directories already exist")


# ---------------------------------------------------------------------------
# Step: write profiles
# ---------------------------------------------------------------------------

PROFILES = {
    "claude-only.env": (
        "# VNX Profile: claude-only\n"
        "# All worker terminals use Claude Code (claude).\n"
        "VNX_T1_PROVIDER=claude_code\n"
        "VNX_T2_PROVIDER=claude_code\n"
    ),
    "claude-codex.env": (
        "# VNX Profile: claude-codex\n"
        "# T1 uses Codex CLI, T2 uses Claude Code.\n"
        "VNX_T1_PROVIDER=codex_cli\n"
        "VNX_T2_PROVIDER=claude_code\n"
    ),
    "claude-gemini.env": (
        "# VNX Profile: claude-gemini\n"
        "# T1 uses Gemini CLI, T2 uses Claude Code.\n"
        "VNX_T1_PROVIDER=gemini_cli\n"
        "VNX_T2_PROVIDER=claude_code\n"
    ),
    "full-multi.env": (
        "# VNX Profile: full-multi\n"
        "# T1 uses Codex CLI, T2 uses Gemini CLI.\n"
        "VNX_T1_PROVIDER=codex_cli\n"
        "VNX_T2_PROVIDER=gemini_cli\n"
    ),
}


def write_profiles(paths: Dict[str, str]) -> StepResult:
    """Write default provider profiles if missing."""
    profiles_dir = Path(paths["VNX_DATA_DIR"]) / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for name, content in PROFILES.items():
        target = profiles_dir / name
        if not target.exists():
            target.write_text(content)
            written.append(name)

    if written:
        return StepResult("profiles", PASS, f"Wrote {len(written)} provider profiles")
    return StepResult("profiles", SKIP, "All profiles already exist")


# ---------------------------------------------------------------------------
# Step: config
# ---------------------------------------------------------------------------

def write_config(paths: Dict[str, str]) -> StepResult:
    """Write .vnx/config.yml if missing."""
    config_dir = Path(paths["PROJECT_ROOT"]) / ".vnx"
    config_file = config_dir / "config.yml"

    if config_file.exists():
        return StepResult("config", SKIP, f"Keeping existing: {config_file}")

    config_dir.mkdir(parents=True, exist_ok=True)
    vnx_home = paths["VNX_HOME"]
    templates_dir = str(Path(vnx_home) / "templates" / "terminals")

    config_file.write_text(
        f'# Generated by vnx init\n'
        f'project_root: "{paths["PROJECT_ROOT"]}"\n'
        f'vnx_home: "{vnx_home}"\n'
        f'vnx_data_dir: "{paths["VNX_DATA_DIR"]}"\n'
        f'terminals_template_dir: "{templates_dir}"\n'
    )
    return StepResult("config", PASS, f"Wrote: {config_file}")


# ---------------------------------------------------------------------------
# Step: bootstrap skills
# ---------------------------------------------------------------------------

def bootstrap_skills(paths: Dict[str, str]) -> StepResult:
    """Copy shipped skills to .claude/skills/ (and multi-provider dirs)."""
    project_root = Path(paths["PROJECT_ROOT"])
    vnx_home = Path(paths["VNX_HOME"])
    shipped = vnx_home / "skills"
    target = project_root / ".claude" / "skills"

    if not shipped.is_dir():
        return StepResult("skills", FAIL, f"Missing shipped skills: {shipped}")

    # Remove stale symlink
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        return StepResult("skills", SKIP, "Keeping existing skills dir")

    target.parent.mkdir(parents=True, exist_ok=True)

    if shutil.which("rsync"):
        subprocess.run(["rsync", "-a", f"{shipped}/", f"{target}/"],
                       check=True, capture_output=True)
    else:
        shutil.copytree(str(shipped), str(target), dirs_exist_ok=True)

    details = []

    # Multi-provider sync
    for cli, skill_dir in [
        ("codex", project_root / ".agents" / "skills"),
        ("gemini", project_root / ".gemini" / "skills"),
    ]:
        if shutil.which(cli):
            skill_dir.mkdir(parents=True, exist_ok=True)
            if shutil.which("rsync"):
                subprocess.run(["rsync", "-a", f"{shipped}/", f"{skill_dir}/"],
                               check=True, capture_output=True)
            else:
                shutil.copytree(str(shipped), str(skill_dir), dirs_exist_ok=True)
            details.append(f"Synced to {cli}: {skill_dir}")

    return StepResult("skills", PASS, f"Copied to {target}", details)


# ---------------------------------------------------------------------------
# Step: bootstrap terminals
# ---------------------------------------------------------------------------

def bootstrap_terminals(paths: Dict[str, str], force: bool = False,
                        terminal_ids: Optional[List[str]] = None) -> StepResult:
    """Create .claude/terminals/{T0..T3}/ with CLAUDE.md and .mcp.json."""
    project_root = Path(paths["PROJECT_ROOT"])
    vnx_home = Path(paths["VNX_HOME"])
    templates_dir = vnx_home / "templates" / "terminals"
    terminals_dir = project_root / ".claude" / "terminals"
    terminals_dir.mkdir(parents=True, exist_ok=True)

    if terminal_ids is None:
        terminal_ids = ["T0", "T1", "T2", "T3"]

    written = []
    skipped = []

    for tid in terminal_ids:
        target_dir = terminals_dir / tid
        target_file = target_dir / "CLAUDE.md"
        template_file = templates_dir / f"{tid}.md"

        target_dir.mkdir(parents=True, exist_ok=True)

        if not template_file.exists():
            return StepResult("terminals", FAIL, f"Missing template: {template_file}")

        if target_file.exists() and not force:
            skipped.append(tid)
            continue

        shutil.copy2(str(template_file), str(target_file))
        written.append(tid)

    # Generate .mcp.json per terminal (disable global MCPs)
    _generate_mcp_configs(terminals_dir, terminal_ids, force)

    # Pre-trust for Gemini CLI
    _pretrust_gemini(project_root)

    details = []
    if written:
        details.append(f"Wrote CLAUDE.md: {', '.join(written)}")
    if skipped:
        details.append(f"Kept existing: {', '.join(skipped)}")

    if written:
        return StepResult("terminals", PASS, f"Bootstrapped {len(written)} terminals", details)
    return StepResult("terminals", SKIP, "All terminals already exist", details)


def _generate_mcp_configs(terminals_dir: Path, terminal_ids: List[str], force: bool) -> None:
    """Write .mcp.json per terminal, disabling global MCPs."""
    global_claude = Path.home() / ".claude.json"
    global_mcps: Dict = {}

    if global_claude.exists():
        try:
            with open(global_claude) as f:
                global_mcps = json.load(f).get("mcpServers", {})
        except (json.JSONDecodeError, OSError):
            pass

    if global_mcps:
        disable_all = {}
        for name, cfg in global_mcps.items():
            entry = dict(cfg)
            entry["disabled"] = True
            disable_all[name] = entry
        mcp_config = {"mcpServers": disable_all}
    else:
        mcp_config = {"mcpServers": {}}

    for tid in terminal_ids:
        target = terminals_dir / tid / ".mcp.json"
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w") as f:
            json.dump(mcp_config, f, indent=2)
            f.write("\n")


def _pretrust_gemini(project_root: Path) -> None:
    """Pre-populate Gemini trust file to avoid first-launch crash."""
    if not shutil.which("gemini"):
        return

    trust_file = Path.home() / ".gemini" / "trustedFolders.json"
    trust_file.parent.mkdir(parents=True, exist_ok=True)

    trust: Dict = {}
    if trust_file.exists():
        try:
            with open(trust_file) as f:
                trust = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    entries = {
        str(project_root): "TRUST_FOLDER",
        str(project_root / ".claude" / "terminals"): "TRUST_FOLDER",
    }

    changed = False
    for path, level in entries.items():
        if path not in trust:
            trust[path] = level
            changed = True

    if changed:
        with open(trust_file, "w") as f:
            json.dump(trust, f, indent=2)
            f.write("\n")


# ---------------------------------------------------------------------------
# Step: bootstrap hooks
# ---------------------------------------------------------------------------

def bootstrap_hooks(paths: Dict[str, str]) -> StepResult:
    """Deploy SessionStart hook and trigger settings merge."""
    project_root = Path(paths["PROJECT_ROOT"])
    vnx_home = Path(paths["VNX_HOME"])
    shipped_hook = vnx_home / "hooks" / "sessionstart.sh"

    if not shipped_hook.exists():
        return StepResult("hooks", FAIL, f"Missing shipped hook: {shipped_hook}")

    hooks_dir = project_root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    target_hook = hooks_dir / "sessionstart.sh"
    shutil.copy2(str(shipped_hook), str(target_hook))
    os.chmod(str(target_hook), 0o755)

    # Trigger settings merge via shell (regen-settings uses bash libs)
    vnx_bin = Path(paths["VNX_HOME"]) / "bin" / "vnx"
    if vnx_bin.exists():
        try:
            subprocess.run(
                [str(vnx_bin), "regen-settings", "--merge", "--no-backup"],
                capture_output=True, timeout=15,
                env={**os.environ, "PROJECT_ROOT": paths["PROJECT_ROOT"],
                     "VNX_HOME": paths["VNX_HOME"]},
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return StepResult("hooks", PASS, f"Deployed: {target_hook}")


# ---------------------------------------------------------------------------
# Step: init-db
# ---------------------------------------------------------------------------

def init_db(paths: Dict[str, str]) -> StepResult:
    """Initialize quality intelligence database from schema."""
    vnx_home = Path(paths["VNX_HOME"])
    state_dir = Path(paths["VNX_STATE_DIR"])
    schema_file = vnx_home / "schemas" / "quality_intelligence.sql"
    db_path = state_dir / "quality_intelligence.db"
    db_init_script = vnx_home / "scripts" / "quality_db_init.py"

    if not schema_file.exists():
        return StepResult("init-db", SKIP, "Schema file not found (skipping)")

    state_dir.mkdir(parents=True, exist_ok=True)

    if db_init_script.exists():
        try:
            subprocess.run(
                [sys.executable, str(db_init_script)],
                capture_output=True, timeout=30, check=True,
                env={**os.environ, **{k: v for k, v in paths.items()}},
            )
            return StepResult("init-db", PASS, "Quality intelligence database ready")
        except subprocess.CalledProcessError as e:
            return StepResult("init-db", FAIL,
                              f"quality_db_init.py failed: {e.stderr.decode()[:200]}")

    # Fallback: apply schema directly
    try:
        conn = sqlite3.connect(str(db_path))
        conn.executescript(schema_file.read_text())
        conn.close()
        return StepResult("init-db", PASS, f"Database initialized via sqlite3: {db_path}")
    except sqlite3.Error as e:
        return StepResult("init-db", FAIL, f"SQLite error: {e}")


# ---------------------------------------------------------------------------
# Step: patch agent files
# ---------------------------------------------------------------------------

def patch_agent_files(paths: Dict[str, str]) -> StepResult:
    """Insert/update VNX marked block in CLAUDE.md / AGENTS.md."""
    vnx_bin = Path(paths["VNX_HOME"]) / "bin" / "vnx"
    if not vnx_bin.exists():
        return StepResult("agent-files", SKIP, "vnx binary not found")

    try:
        subprocess.run(
            [str(vnx_bin), "patch-agent-files"],
            capture_output=True, timeout=15,
            env={**os.environ, "PROJECT_ROOT": paths["PROJECT_ROOT"],
                 "VNX_HOME": paths["VNX_HOME"]},
        )
        return StepResult("agent-files", PASS, "Agent files patched")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return StepResult("agent-files", SKIP, "Could not run patch-agent-files")


# ---------------------------------------------------------------------------
# Step: intelligence import
# ---------------------------------------------------------------------------

def intelligence_import(paths: Dict[str, str]) -> StepResult:
    """Import git-tracked intelligence into SQLite."""
    intel_dir = Path(paths.get("VNX_INTELLIGENCE_DIR",
                               Path(paths["PROJECT_ROOT"]) / ".vnx-intelligence"))
    export_dir = intel_dir / "db_export"

    if not export_dir.is_dir():
        return StepResult("intelligence-import", SKIP, "No .vnx-intelligence/db_export found")

    import_script = Path(paths["VNX_HOME"]) / "scripts" / "intelligence_import.py"
    if not import_script.exists():
        return StepResult("intelligence-import", SKIP, "Import script not found")

    try:
        subprocess.run(
            [sys.executable, str(import_script)],
            capture_output=True, timeout=60, check=True,
            env={**os.environ, **{k: v for k, v in paths.items()}},
        )
        return StepResult("intelligence-import", PASS, "Intelligence imported into SQLite")
    except subprocess.CalledProcessError as e:
        return StepResult("intelligence-import", FAIL,
                          f"Import failed: {e.stderr.decode()[:200]}")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_init(paths: Dict[str, str], skip_hooks: bool = False) -> List[StepResult]:
    """Execute the full init sequence, returning structured results."""
    results: List[StepResult] = []

    results.append(ensure_runtime_layout(paths))
    results.append(write_profiles(paths))
    results.append(write_config(paths))
    results.append(bootstrap_skills(paths))
    results.append(bootstrap_terminals(paths))

    if not skip_hooks:
        results.append(bootstrap_hooks(paths))

    results.append(patch_agent_files(paths))
    results.append(init_db(paths))
    results.append(intelligence_import(paths))

    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="VNX Init — unified bootstrap orchestrator")
    parser.add_argument("--skip-hooks", action="store_true",
                        help="Skip hooks deployment (useful in CI)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    parser.add_argument("--step", choices=[
        "layout", "profiles", "config", "skills", "terminals",
        "hooks", "agent-files", "init-db", "intelligence-import",
    ], help="Run only a specific step")
    args = parser.parse_args()

    paths = ensure_env()

    if args.step:
        step_map = {
            "layout": lambda: ensure_runtime_layout(paths),
            "profiles": lambda: write_profiles(paths),
            "config": lambda: write_config(paths),
            "skills": lambda: bootstrap_skills(paths),
            "terminals": lambda: bootstrap_terminals(paths),
            "hooks": lambda: bootstrap_hooks(paths),
            "agent-files": lambda: patch_agent_files(paths),
            "init-db": lambda: init_db(paths),
            "intelligence-import": lambda: intelligence_import(paths),
        }
        results = [step_map[args.step]()]
    else:
        results = run_init(paths, skip_hooks=args.skip_hooks)

    if args.json:
        out = [{"name": r.name, "status": r.status, "message": r.message,
                "details": r.details} for r in results]
        print(json.dumps(out, indent=2))
    else:
        for r in results:
            _log(r)

        failures = [r for r in results if r.status == FAIL]
        if failures:
            print(f"\n{RED}[init] {len(failures)} step(s) failed.{RESET}")
        else:
            print(f"\n{GREEN}[init] Done. Runtime root: {paths['VNX_DATA_DIR']}{RESET}")
            print(f"[init] Next: vnx doctor")

    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
