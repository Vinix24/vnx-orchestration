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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env
from vnx_skills import is_opted_out, iter_skill_dirs

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
    """Create the runtime data tree under the resolved VNX_DATA_DIR.

    PR-PIP-2: VNX_DATA_DIR is resolved by vnx_paths (explicit override >
    VNX_DATA_HOME > existing ~/.vnx-data/<id> > existing project-local
    .vnx-data > XDG user-data-dir), so a fresh install lands outside the
    project map while existing dev checkouts keep their project-local tree.
    """
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

def _refresh_canon_skills(canon_dirs: List[Path], target: Path) -> "tuple[int, int]":
    """Refresh each canon skill into target/<name>/ in place.

    ``target`` (.claude/skills and its codex/gemini mirrors) is a MIXED dir:
    shipped canon skills co-exist with project-authored skills (ADR
    2026-07-14 skills-in-consumers-ssot §11). Only directory names present in
    ``canon_dirs`` are touched; anything else under target is project-authored
    and is left byte-for-byte untouched. A target skill dir carrying the
    ``.vnx-skip-sync`` marker is preserved even when its name matches canon,
    so a consumer can pin a local customization of a canon skill.

    Returns (canon_refreshed_count, project_preserved_count).
    """
    if target.is_symlink():
        target.unlink()

    target.mkdir(parents=True, exist_ok=True)
    use_rsync = shutil.which("rsync") is not None

    canon_names = {skill_dir.name for skill_dir in canon_dirs}
    refreshed = 0
    for skill_dir in canon_dirs:
        dest = target / skill_dir.name
        if dest.exists() and is_opted_out(dest):
            continue
        if use_rsync:
            subprocess.run(["rsync", "-a", f"{skill_dir}/", f"{dest}/"],
                           check=True, capture_output=True)
        else:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(str(skill_dir), str(dest))
        refreshed += 1

    preserved = sum(
        1 for child in target.iterdir()
        if child.is_dir() and child.name not in canon_names
    )

    return refreshed, preserved


# Top-level (non-directory) files in the shipped skills root that are shipped
# canon config, not project-authored, and must ship alongside the per-skill
# dirs. `skills.yaml` is the registry `vnx doctor` and skill validation read
# from the target root, not from a per-skill subdirectory (OI-624).
SKILLS_REGISTRY_FILES = ("skills.yaml",)


class RegistryCopyError(RuntimeError):
    """Raised when a shipped registry file cannot be safely synced to target."""


def _refresh_registry_files(shipped: Path, target: Path) -> "tuple[int, int]":
    """Seed shipped top-level registry file(s) into target, copy-if-missing only.

    Unlike the per-skill dirs (`_refresh_canon_skills`, never-drift-from-canon),
    `skills.yaml` is a MIXED file by its own contract: consumers extend it with
    project-specific skill registrations (ADR-032). Clobbering it on every run
    would destroy those extensions, so a registry file already present at the
    target is left byte-for-byte untouched — only a MISSING target file is
    seeded from canon.

    Must be called with a `target` directory that already exists (the normal
    call order runs `_refresh_canon_skills(canon_dirs, target)` first, which
    both removes a stale target-level symlink and creates the directory).

    Raises RegistryCopyError (fail-loud, never silently skipped) when:
      - the shipped source file is missing.
      - the target path is itself a symlink (refuses to follow it, since that
        could write outside the project root).
      - the target directory resolves outside itself between mkdir and write
        (defense-in-depth against a swapped-out parent dir).

    Returns (copied_count, preserved_count).
    """
    copied = 0
    preserved = 0
    for name in SKILLS_REGISTRY_FILES:
        src = shipped / name
        if not src.is_file():
            raise RegistryCopyError(f"Missing shipped registry file: {src}")

        dest = target / name
        if dest.is_symlink():
            raise RegistryCopyError(
                f"Refusing to write registry file through symlink: {dest} "
                f"-> {os.path.realpath(str(dest))}"
            )
        if dest.exists():
            preserved += 1
            continue

        resolved_target = os.path.realpath(str(target))
        resolved_dest_dir = os.path.realpath(str(dest.parent))
        if resolved_dest_dir != resolved_target:
            raise RegistryCopyError(
                f"Refusing registry write: {dest} resolves outside target dir {target}"
            )

        fd, tmp_path = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=str(target))
        try:
            with os.fdopen(fd, "wb") as tmp_f, open(src, "rb") as src_f:
                shutil.copyfileobj(src_f, tmp_f)
            shutil.copystat(str(src), tmp_path)
            os.replace(tmp_path, str(dest))
        except OSError as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise RegistryCopyError(f"Failed to copy registry file {src} -> {dest}: {e}") from e
        copied += 1

    return copied, preserved


def bootstrap_skills(paths: Dict[str, str]) -> StepResult:
    """Refresh shipped canon skills into .claude/skills/ (and multi-provider dirs).

    Per-skill refresh, not dir-level copy-once: each shipped skill under
    VNX_HOME/skills is re-synced into the target by name every run, so a
    consumer's canon skills can never freeze/drift from fabric canon. Skill
    dirs whose name isn't in the shipped set are project-authored and are
    never touched.

    The top-level `skills.yaml` registry is different: it is a MIXED file
    (canon entries + a consumer's own project-specific registrations, per
    ADR-032), so it is seeded copy-if-missing rather than refreshed every
    run — see `_refresh_registry_files`.
    """
    project_root = Path(paths["PROJECT_ROOT"])
    vnx_home = Path(paths["VNX_HOME"])
    shipped = vnx_home / "skills"
    target = project_root / ".claude" / "skills"

    if not shipped.is_dir():
        return StepResult("skills", FAIL, f"Missing shipped skills: {shipped}")

    canon_dirs = list(iter_skill_dirs(shipped))
    if not canon_dirs:
        return StepResult("skills", FAIL, f"No canon skill directories found under: {shipped}")

    refreshed, preserved = _refresh_canon_skills(canon_dirs, target)
    try:
        reg_copied, reg_preserved = _refresh_registry_files(shipped, target)
    except RegistryCopyError as e:
        return StepResult("skills", FAIL, f"Registry sync failed for {target}: {e}")

    details = [
        f".claude/skills: refreshed {refreshed} canon skill(s), "
        f"preserved {preserved} project skill(s), "
        f"{reg_copied} registry file(s) seeded, {reg_preserved} preserved (already present)"
    ]

    # Multi-provider sync
    for cli, skill_dir in [
        ("codex", project_root / ".agents" / "skills"),
        ("gemini", project_root / ".gemini" / "skills"),
    ]:
        if shutil.which(cli):
            r, p = _refresh_canon_skills(canon_dirs, skill_dir)
            try:
                reg_c, reg_p = _refresh_registry_files(shipped, skill_dir)
            except RegistryCopyError as e:
                return StepResult("skills", FAIL, f"Registry sync failed for {skill_dir}: {e}")
            details.append(
                f"Synced to {cli} ({skill_dir}): refreshed {r} canon skill(s), "
                f"preserved {p} project skill(s), "
                f"{reg_c} registry file(s) seeded, {reg_p} preserved (already present)"
            )

    return StepResult("skills", PASS,
                      f"Refreshed {refreshed} canon skill(s) in {target}", details)


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
# Step: generate tri-files (AGENTS.md + GEMINI.md mirror of CLAUDE.md)
# ---------------------------------------------------------------------------

_BOOTSTRAP_START = "<!-- VNX:BEGIN BOOTSTRAP -->"
_BOOTSTRAP_END = "<!-- VNX:END BOOTSTRAP -->"


def generate_tri_files(paths: Dict[str, str]) -> StepResult:
    """Generate AGENTS.md + GEMINI.md mirroring CLAUDE.md bootstrap block.

    Extracts the VNX bootstrap block from CLAUDE.md and writes/updates
    AGENTS.md and GEMINI.md so all three provider instruction files stay
    in sync. Idempotent: skips files whose bootstrap block is already current.
    """
    project_root = Path(paths["PROJECT_ROOT"])
    claude_md = project_root / "CLAUDE.md"

    if not claude_md.exists():
        return StepResult("tri-files", SKIP, "CLAUDE.md not found; skipping tri-file generation")

    content = claude_md.read_text(encoding="utf-8")
    start_idx = content.find(_BOOTSTRAP_START)
    end_idx = content.find(_BOOTSTRAP_END)
    if start_idx == -1 or end_idx == -1:
        return StepResult("tri-files", SKIP, "No VNX bootstrap block in CLAUDE.md; skipping")

    bootstrap_block = content[start_idx:end_idx + len(_BOOTSTRAP_END)]

    generated = []
    for filename in ("AGENTS.md", "GEMINI.md"):
        target = project_root / filename
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if bootstrap_block in existing:
                continue
            s = existing.find(_BOOTSTRAP_START)
            e = existing.find(_BOOTSTRAP_END)
            if s != -1 and e != -1:
                updated = existing[:s] + bootstrap_block + existing[e + len(_BOOTSTRAP_END):]
            else:
                updated = bootstrap_block + "\n"
        else:
            updated = bootstrap_block + "\n"

        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, target)
        generated.append(filename)

    if generated:
        return StepResult("tri-files", PASS, f"Generated/updated: {', '.join(generated)}")
    return StepResult("tri-files", SKIP, "AGENTS.md + GEMINI.md already in sync with CLAUDE.md")


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
                               Path(paths.get("VNX_CANONICAL_ROOT") or paths["VNX_HOME"]) / ".vnx-intelligence"))
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

def run_init(paths: Dict[str, str], skip_hooks: bool = False,
             starter: bool = False) -> List[StepResult]:
    """Execute the full init sequence, returning structured results."""
    results: List[StepResult] = []

    results.append(ensure_runtime_layout(paths))

    if starter:
        results.append(StepResult("profiles", SKIP,
                                  "Starter mode: single provider, profiles not needed"))
    else:
        results.append(write_profiles(paths))

    results.append(write_config(paths))
    results.append(bootstrap_skills(paths))

    if starter:
        # Starter mode: only bootstrap T0
        results.append(bootstrap_terminals(paths, terminal_ids=["T0"]))
    else:
        results.append(bootstrap_terminals(paths))

    if not skip_hooks:
        results.append(bootstrap_hooks(paths))

    results.append(generate_tri_files(paths))
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
    parser.add_argument("--starter", action="store_true",
                        help="Initialize in starter mode (single terminal, no tmux)")
    parser.add_argument("--operator", action="store_true",
                        help="Initialize in operator mode (full tmux grid)")
    parser.add_argument("--step", choices=[
        "layout", "profiles", "config", "skills", "terminals",
        "hooks", "tri-files", "agent-files", "init-db", "intelligence-import",
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
            "tri-files": lambda: generate_tri_files(paths),
            "agent-files": lambda: patch_agent_files(paths),
            "init-db": lambda: init_db(paths),
            "intelligence-import": lambda: intelligence_import(paths),
        }
        results = [step_map[args.step]()]
    else:
        results = run_init(paths, skip_hooks=args.skip_hooks,
                           starter=args.starter)

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
