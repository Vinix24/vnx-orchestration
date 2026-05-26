#!/usr/bin/env python3
"""VNX Worker Permissions Merge Engine

Manages worker_permissions.yaml with overlay semantics that mirror the
settings.json merge pattern (vnx_settings_merge.py):

  _vnx_meta.managed_keys documents what VNX owns vs what the project owns.
  On every regen/cutover the merge engine reads the shipped template (VNX_HOME),
  reads the project file (PROJECT_ROOT/.vnx/worker_permissions.yaml), and produces
  a merged output that preserves project overrides.

Merge semantics per key:
  version              VNX-managed — always taken from template
  _vnx_meta            VNX-managed — always taken from template
  profiles.<role>      Baseline role entries: VNX-managed (overwritten by template).
                       Project-added roles that do NOT exist in the template are
                       preserved unchanged.
                       Within a role that exists in BOTH template and project:
                         allowed_tools      union (template baseline + project extras)
                         denied_tools       union (template baseline + project extras)
                         bash_allow_patterns union
                         bash_deny_patterns  union
                         file_write_scope   union — THIS is where project paths live
  terminal_assignments VNX-managed baseline; project overrides are preserved
                       (project value wins for terminals that exist in both)

Usage:
  python3 vnx_worker_permissions_merge.py --merge  --project-root /path/to/project --vnx-home /path/to/vnx
  python3 vnx_worker_permissions_merge.py --full   --project-root /path/to/project --vnx-home /path/to/vnx
  python3 vnx_worker_permissions_merge.py --validate --project-root /path/to/project --vnx-home /path/to/vnx
  python3 vnx_worker_permissions_merge.py --merge  --project-root /path/to/project --dry-run
"""

from __future__ import annotations

import argparse
import copy
import os
import shutil
import sys
from datetime import datetime, timezone
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# VNX-managed keys documentation (mirrored in _vnx_meta.managed_keys)
# ---------------------------------------------------------------------------

VNX_MANAGED_PROFILE_KEYS = {
    "allowed_tools",
    "denied_tools",
    "bash_allow_patterns",
    "bash_deny_patterns",
}
# file_write_scope is intentionally NOT in this set — it is project-owned.
# VNX provides defaults; projects extend with their own source paths.

VNX_MANAGED_TOP_KEYS = {"version", "_vnx_meta"}
# terminal_assignments: VNX baseline, project overrides win for collisions.


# ---------------------------------------------------------------------------
# Template location
# ---------------------------------------------------------------------------

def find_template(vnx_home: str) -> str:
    """Locate the shipped worker_permissions.yaml template in VNX_HOME."""
    # Primary: shipped template under .vnx/templates/
    tmpl = os.path.join(vnx_home, "templates", "worker_permissions.yaml.tmpl")
    if os.path.isfile(tmpl):
        return tmpl
    # Fallback: the .vnx/ root file itself (dev-mode layout where vnx home IS repo root/.vnx)
    root_file = os.path.join(vnx_home, "worker_permissions.yaml")
    if os.path.isfile(root_file):
        return root_file
    return tmpl  # let caller raise the missing-file error


def load_yaml(path: str) -> dict:
    """Load a YAML file; return empty dict if missing."""
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_template(vnx_home: str) -> dict:
    """Load and return the VNX worker_permissions template."""
    tmpl_path = find_template(vnx_home)
    if not os.path.isfile(tmpl_path):
        print(f"ERROR: worker_permissions template not found at: {tmpl_path}", file=sys.stderr)
        sys.exit(1)
    return load_yaml(tmpl_path)


def project_permissions_path(project_root: str) -> str:
    return os.path.join(project_root, ".vnx", "worker_permissions.yaml")


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def union_list(base: list, overlay: list) -> list:
    """Union two lists, preserving base-first order, deduplicating."""
    seen: set = set()
    result: list = []
    for item in base + overlay:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def merge_role(template_role: dict, project_role: dict) -> dict:
    """Merge a single role definition.

    VNX baseline keys (allowed_tools, denied_tools, bash_*) are unioned so that
    VNX additions (e.g. new deny patterns) reach project installs while project
    extras survive.

    file_write_scope is union-merged: VNX baseline paths + project-specific paths.
    """
    result: dict[str, Any] = {}

    for key in ("allowed_tools", "denied_tools", "bash_allow_patterns", "bash_deny_patterns"):
        base = template_role.get(key) or []
        proj = project_role.get(key) or []
        result[key] = union_list(base, proj)

    # file_write_scope: union — project paths survive rc-cutover
    base_scope = template_role.get("file_write_scope") or []
    proj_scope = project_role.get("file_write_scope") or []
    result["file_write_scope"] = union_list(base_scope, proj_scope)

    # Preserve any unknown project-added keys within the role
    for k, v in project_role.items():
        if k not in result:
            result[k] = v

    return result


def merge_profiles(template_profiles: dict, project_profiles: dict) -> dict:
    """Merge profiles section.

    Roles in template: merged (VNX baseline + project overrides).
    Roles only in project: preserved unchanged (project-added roles).
    """
    result: dict = {}

    # Process template roles first (baseline + project overlay)
    for role, tmpl_role in template_profiles.items():
        if role in project_profiles:
            result[role] = merge_role(tmpl_role, project_profiles[role])
        else:
            # Template-only role: use template as-is
            result[role] = copy.deepcopy(tmpl_role)

    # Preserve project-only roles (not in template)
    for role, proj_role in project_profiles.items():
        if role not in result:
            result[role] = copy.deepcopy(proj_role)

    return result


def merge_terminal_assignments(template_ta: dict, project_ta: dict) -> dict:
    """Merge terminal_assignments.

    Template provides baseline (T1=backend-developer etc).
    Project overrides win for terminals that appear in both — projects may
    remap terminals to project-specific roles.
    Project-only terminals are preserved.
    """
    result = dict(template_ta)
    for terminal, role in project_ta.items():
        result[terminal] = role  # project wins for any collision
    return result


def merge_permissions(existing: dict, template: dict) -> dict:
    """Merge existing project file with VNX template."""
    result: dict = {}

    # VNX-managed top-level keys: always from template
    result["version"] = template.get("version", 1)
    result["_vnx_meta"] = template.get("_vnx_meta", _default_vnx_meta())

    # profiles: merge logic above
    result["profiles"] = merge_profiles(
        template.get("profiles", {}),
        existing.get("profiles", {}),
    )

    # terminal_assignments: merge with project-wins-on-collision
    result["terminal_assignments"] = merge_terminal_assignments(
        template.get("terminal_assignments", {}),
        existing.get("terminal_assignments", {}),
    )

    # Preserve any unknown top-level project keys
    for k, v in existing.items():
        if k not in result:
            result[k] = v

    return result


def _default_vnx_meta() -> dict:
    return {
        "managed_keys": [
            "version",
            "profiles.<role>.allowed_tools (vnx_baseline)",
            "profiles.<role>.denied_tools (vnx_baseline)",
            "profiles.<role>.bash_allow_patterns (vnx_baseline)",
            "profiles.<role>.bash_deny_patterns (vnx_baseline)",
            "terminal_assignments (vnx_baseline; project overrides win)",
        ],
        "project_owned_keys": [
            "profiles.<role>.file_write_scope",
            "profiles.<project-role>  (roles not in template)",
        ],
        "description": (
            "VNX worker permission profiles. "
            "Merge via 'vnx regen-worker-permissions --merge'. "
            "file_write_scope and project-added roles survive cutover."
        ),
    }


def generate_full_permissions(template: dict) -> dict:
    """Generate a project worker_permissions.yaml from the VNX template (first-time)."""
    result = copy.deepcopy(template)
    if "_vnx_meta" not in result:
        result["_vnx_meta"] = _default_vnx_meta()
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_permissions(data: dict) -> list[str]:
    """Validate the structure of worker_permissions.yaml.  Returns list of issues."""
    issues: list[str] = []

    if not isinstance(data, dict):
        issues.append("root must be a YAML mapping")
        return issues

    profiles = data.get("profiles")
    if profiles is None:
        issues.append("missing 'profiles' section")
    elif not isinstance(profiles, dict):
        issues.append("'profiles' must be a mapping")
    else:
        for role, body in profiles.items():
            if not isinstance(body, dict):
                issues.append(f"profiles.{role}: must be a mapping")
                continue
            for list_key in ("allowed_tools", "denied_tools", "bash_allow_patterns",
                             "bash_deny_patterns", "file_write_scope"):
                v = body.get(list_key)
                if v is not None and not isinstance(v, list):
                    issues.append(f"profiles.{role}.{list_key}: must be a list")

    ta = data.get("terminal_assignments")
    if ta is not None and not isinstance(ta, dict):
        issues.append("'terminal_assignments' must be a mapping")

    return issues


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def backup_file(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = f"{path}.bak.{ts}"
    shutil.copy2(path, bak)
    return bak


def write_yaml(path: str, data: dict) -> None:
    """Atomically write a YAML file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def diff_summary(existing: dict | None, merged: dict) -> list[str]:
    """Generate a human-readable diff summary for the merge."""
    lines: list[str] = []

    if existing is None:
        lines.append("[regen-worker-permissions] Creating new worker_permissions.yaml")
        return lines

    old_profiles = set(existing.get("profiles", {}).keys())
    new_profiles = set(merged.get("profiles", {}).keys())
    added = new_profiles - old_profiles
    removed = old_profiles - new_profiles
    if added:
        lines.append(f"  profiles: +{sorted(added)}")
    if removed:
        lines.append(f"  profiles: -{sorted(removed)}")

    for role in new_profiles & old_profiles:
        old_scope = set(existing.get("profiles", {}).get(role, {}).get("file_write_scope") or [])
        new_scope = set(merged.get("profiles", {}).get(role, {}).get("file_write_scope") or [])
        gained = new_scope - old_scope
        lost = old_scope - new_scope
        if gained:
            lines.append(f"  profiles.{role}.file_write_scope: +{sorted(gained)}")
        if lost:
            lines.append(f"  profiles.{role}.file_write_scope: -{sorted(lost)}")

    old_ta = existing.get("terminal_assignments", {})
    new_ta = merged.get("terminal_assignments", {})
    for t, role in new_ta.items():
        if old_ta.get(t) != role:
            lines.append(f"  terminal_assignments.{t}: {old_ta.get(t)!r} -> {role!r}")

    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="VNX Worker Permissions Merge Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--merge", action="store_true",
                      help="Merge VNX template into existing worker_permissions.yaml")
    mode.add_argument("--full", action="store_true",
                      help="Generate worker_permissions.yaml from VNX template (first-time init)")
    mode.add_argument("--validate", action="store_true",
                      help="Validate existing worker_permissions.yaml structure")

    parser.add_argument("--project-root", required=True, help="Project root directory")
    parser.add_argument("--vnx-home", default=None,
                        help="VNX home directory (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip backup of existing file")
    parser.add_argument("--json", action="store_true",
                        help="Output result as JSON (for scripting)")

    args = parser.parse_args()

    project_root = os.path.realpath(args.project_root)
    target_path = project_permissions_path(project_root)

    # Auto-detect vnx_home when not provided
    if args.vnx_home:
        vnx_home = args.vnx_home
    else:
        vnx_home = _detect_vnx_home(project_root)

    if args.validate:
        data = load_yaml(target_path)
        if not data:
            print(f"ERROR: {target_path} not found or empty", file=sys.stderr)
            return 1
        issues = validate_permissions(data)
        if issues:
            for i in issues:
                print(f"  FAIL: {i}", file=sys.stderr)
            return 1
        print(f"[regen-worker-permissions] {target_path}: valid")
        return 0

    template = load_template(vnx_home)

    if args.full:
        existing = load_yaml(target_path) if os.path.isfile(target_path) else None
        result = generate_full_permissions(template)

        issues = validate_permissions(result)
        if issues:
            print("ERROR: generated permissions failed validation:", file=sys.stderr)
            for i in issues:
                print(f"  {i}", file=sys.stderr)
            return 1

        if args.dry_run:
            if args.json:
                import json
                print(json.dumps(result, indent=2))
            else:
                print("[regen-worker-permissions] --full --dry-run: would create worker_permissions.yaml")
                if existing:
                    print("[regen-worker-permissions] WARNING: would overwrite existing file")
            return 0

        if existing and not args.no_backup:
            bak = backup_file(target_path)
            print(f"[regen-worker-permissions] Backup: {bak}")

        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        write_yaml(target_path, result)
        print(f"[regen-worker-permissions] Created: {target_path} (full)")
        return 0

    # --merge mode
    existing = load_yaml(target_path) if os.path.isfile(target_path) else None

    if existing is None:
        print("[regen-worker-permissions] No existing file; generating full from template")
        result = generate_full_permissions(template)
    else:
        result = merge_permissions(existing, template)

    issues = validate_permissions(result)
    if issues:
        print("ERROR: merged permissions failed validation:", file=sys.stderr)
        for i in issues:
            print(f"  {i}", file=sys.stderr)
        return 1

    summary = diff_summary(existing, result)

    if args.dry_run:
        if args.json:
            import json
            print(json.dumps(result, indent=2))
        else:
            print("[regen-worker-permissions] --merge --dry-run:")
            if summary:
                for line in summary:
                    print(line)
            else:
                print("  (no changes)")
        return 0

    if existing and not args.no_backup:
        bak = backup_file(target_path)
        print(f"[regen-worker-permissions] Backup: {bak}")

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    write_yaml(target_path, result)

    if summary:
        for line in summary:
            print(line)
    print(f"[regen-worker-permissions] Updated: {target_path} (merge)")
    return 0


def _detect_vnx_home(project_root: str) -> str:
    """Auto-detect VNX_HOME from the environment or standard locations."""
    vnx_home_env = os.environ.get("VNX_HOME")
    if vnx_home_env and os.path.isdir(vnx_home_env):
        return vnx_home_env

    # Embedded layout: project/.vnx/
    embedded = os.path.join(project_root, ".vnx")
    if os.path.isdir(embedded):
        return embedded

    return embedded  # best guess


if __name__ == "__main__":
    sys.exit(main())
