#!/usr/bin/env python3
"""VNX Settings Merge Engine

Manages settings.json with patch-based semantics:
- VNX owns: hooks, VNX_* env vars, baseline permissions
- Project owns: everything else (custom env, additionalDirectories, ask rules, extra permissions)

Merge semantics:
  env:                      VNX_* keys replaced, project keys preserved
  permissions.allow:        union (VNX baseline + project entries, deduplicated)
  permissions.deny:         union (VNX baseline + project entries, deduplicated)
  permissions.ask:          preserved (project-owned)
  permissions.additionalDirectories: preserved (project-owned)
  hooks:                    replaced (VNX-owned)
  _vnx_meta:                replaced (VNX-owned)
  other top-level keys:     preserved (project-owned)

Deny-over-allow: if a pattern exists in both deny and allow after merge,
it is removed from allow and kept in deny.

Usage:
  python3 vnx_settings_merge.py --merge --project-root /path/to/project
  python3 vnx_settings_merge.py --full  --project-root /path/to/project
  python3 vnx_settings_merge.py --merge --project-root /path/to/project --dry-run
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

# Deferred (merge-time) template placeholder token for the VNX_HOME slot in
# settings_vnx_keys.json.tmpl. Built by concatenation on purpose, NOT written
# out as a literal "{{VNX_HOME}}" string: install.sh's own INSTALL-TIME
# placeholder substitution (_substitute_install_templates) scans every
# shipped .py file for that exact 12-character spelling and sed-replaces it
# with the current install's path (see install.sh's INSTALL_TIME_PLACEHOLDERS
# vocabulary). A literal occurrence here collides with that vocabulary and
# gets permanently clobbered the moment this file ships via `install.sh`,
# silently turning this search pattern into a no-op against the (correctly
# un-substituted, .tmpl-protected) placeholder in the real template — see
# OI-1139. PROJECT_ROOT does not collide (install.sh's token is
# VNX_PROJECT_ROOT, a different spelling) so it is left as a normal literal.
_VNX_HOME_TOKEN = "{{" + "VNX_HOME" + "}}"


def find_vnx_home(project_root: str) -> str:
    """Resolve VNX_HOME: prefer .vnx/ layout, fall back to legacy layout."""
    vnx_primary = os.path.join(project_root, ".vnx")
    vnx_legacy = os.path.join(project_root, ".claude", "vnx-system")

    if os.path.isdir(vnx_primary):
        return vnx_primary
    if os.path.isdir(vnx_legacy):
        return vnx_legacy

    # Check if we're in VNX dist root (bin/vnx exists at project root level)
    if os.path.isfile(os.path.join(project_root, "bin", "vnx")):
        return project_root

    return vnx_primary  # default expectation


def _relative_suffixes(raw: str, token: str) -> list[str]:
    """Return the literal text following each ``token`` occurrence in ``raw``,
    up to the next ``"`` (the JSON string terminator). Used to know what a
    correctly-rendered value must be followed by, so the render can be
    verified structurally instead of just hoped for.
    """
    suffixes = []
    start = 0
    while True:
        idx = raw.find(token, start)
        if idx == -1:
            break
        after = idx + len(token)
        end = raw.find('"', after)
        suffixes.append(raw[after:end] if end != -1 else raw[after:after + 80])
        start = after
    return suffixes


def _assert_rendered_cleanly(raw: str, rendered: str, vnx_home: str, project_root: str,
                              template_path: str) -> None:
    """Guard against OI-1139: a render that silently drops the install root.

    A rendered hook command must never contain an unresolved ``{{...}}``
    placeholder, and every VNX_HOME/PROJECT_ROOT-relative path the raw
    template declares must actually appear prefixed by the real (non-empty)
    value in the output — not as a bare path rooted at ``/``. Checking the
    real value is present (rather than just "no placeholder left") also
    catches a corrupted search pattern that turns the substitution into a
    silent no-op, which is exactly how this regression manifested: the
    template's own placeholder was untouched, but the *code* rendering it had
    been mangled to search for the wrong string.
    """
    if "{{" in rendered:
        print(
            f"ERROR: {template_path} rendered with an unresolved template "
            "placeholder still present in the output — refusing to write dead "
            "hook pins",
            file=sys.stderr,
        )
        sys.exit(1)

    for label, token, value in (
        ("VNX_HOME", _VNX_HOME_TOKEN, vnx_home),
        ("PROJECT_ROOT", "{{PROJECT_ROOT}}", project_root),
    ):
        for suffix in _relative_suffixes(raw, token):
            if suffix and (value + suffix) not in rendered:
                print(
                    f"ERROR: {template_path} — {label} substitution did not "
                    f"produce the expected path (value + {suffix!r}) in the "
                    "rendered output; a hook would resolve to a path rooted "
                    "at '/' instead of the install root — refusing to write "
                    "dead hook pins",
                    file=sys.stderr,
                )
                sys.exit(1)


def load_template(vnx_home: str, project_root: str) -> dict:
    """Load and render the VNX settings template."""
    if not vnx_home:
        print(
            "ERROR: vnx_home is empty — refusing to render hook commands "
            "rooted at '/' instead of the install root",
            file=sys.stderr,
        )
        sys.exit(1)
    if not project_root:
        print(
            "ERROR: project_root is empty — refusing to render hook commands "
            "rooted at '/' instead of the project root",
            file=sys.stderr,
        )
        sys.exit(1)

    template_path = os.path.join(vnx_home, "templates", "settings_vnx_keys.json.tmpl")
    if not os.path.isfile(template_path):
        print(f"ERROR: VNX settings template not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    with open(template_path, "r") as f:
        raw = f.read()

    # Substitute template variables. VNX_HOME is where the shared scripts/
    # tree actually lives (embedded: a hidden dir under the project's .claude/,
    # central: the shared per-machine install, dev checkout: the repo itself)
    # — distinct from PROJECT_ROOT, which only equals VNX_HOME in the
    # dev-checkout case. A hardcoded VNX_HOME-relative hook command breaks in
    # embedded and central installs. See #1023.
    rendered = raw.replace(_VNX_HOME_TOKEN, vnx_home)
    rendered = rendered.replace("{{PROJECT_ROOT}}", project_root)

    _assert_rendered_cleanly(raw, rendered, vnx_home, project_root, template_path)

    try:
        return json.loads(rendered)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in template after rendering: {e}", file=sys.stderr)
        sys.exit(1)


def load_existing_settings(settings_path: str) -> dict | None:
    """Load existing settings.json, return None if missing."""
    if not os.path.isfile(settings_path):
        return None
    try:
        with open(settings_path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Existing settings.json is invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)


def union_lists(base: list, overlay: list) -> list:
    """Union two lists, preserving order (base first), deduplicating."""
    seen = set()
    result = []
    for item in base + overlay:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def is_vnx_env_key(key: str) -> bool:
    """Check if an env key is VNX-managed (VNX_ prefix)."""
    return key.startswith("VNX_")


def merge_env(existing_env: dict, vnx_env: dict) -> dict:
    """Merge env: VNX_* keys from template, project keys preserved."""
    result = {}

    # Start with existing (project) keys
    for key, val in existing_env.items():
        result[key] = val

    # Overlay VNX-managed keys (overwrite if exists)
    for key, val in vnx_env.items():
        if is_vnx_env_key(key):
            result[key] = val
        else:
            # Template shouldn't have non-VNX keys, but if it does, only add if missing
            if key not in result:
                result[key] = val

    return result


def merge_permissions(existing_perms: dict, vnx_perms: dict) -> dict:
    """Merge permissions with union semantics and deny-over-allow precedence."""
    result = {}

    # Preserve project-owned keys
    for key in ("additionalDirectories", "ask"):
        if key in existing_perms:
            result[key] = existing_perms[key]

    # Union allow lists
    existing_allow = existing_perms.get("allow", [])
    vnx_allow = vnx_perms.get("allow", [])
    merged_allow = union_lists(existing_allow, vnx_allow)

    # Union deny lists
    existing_deny = existing_perms.get("deny", [])
    vnx_deny = vnx_perms.get("deny", [])
    merged_deny = union_lists(existing_deny, vnx_deny)

    # Deny-over-allow: remove from allow anything that appears in deny
    deny_set = set(merged_deny)
    merged_allow = [p for p in merged_allow if p not in deny_set]

    result["allow"] = merged_allow
    result["deny"] = merged_deny

    # Preserve any other project permission keys we don't know about
    for key in existing_perms:
        if key not in result:
            result[key] = existing_perms[key]

    return result


def merge_settings(existing: dict, vnx_template: dict) -> dict:
    """Merge VNX template keys into existing settings with patch semantics."""
    result = {}

    # Start with all existing keys
    for key in existing:
        if key == "_vnx_meta":
            continue  # Will be replaced
        result[key] = existing[key]

    # Merge env
    existing_env = existing.get("env", {})
    vnx_env = vnx_template.get("env", {})
    result["env"] = merge_env(existing_env, vnx_env)

    # Merge permissions
    existing_perms = existing.get("permissions", {})
    vnx_perms = vnx_template.get("permissions", {})
    result["permissions"] = merge_permissions(existing_perms, vnx_perms)

    # Replace hooks (VNX-owned entirely)
    if "hooks" in vnx_template:
        result["hooks"] = vnx_template["hooks"]

    # Set VNX meta
    result["_vnx_meta"] = vnx_template.get("_vnx_meta", {})

    return result


def generate_full_settings(vnx_template: dict) -> dict:
    """Generate a complete settings.json from VNX template (first-time init)."""
    result = {
        "env": vnx_template.get("env", {}),
        "permissions": {
            "allow": vnx_template.get("permissions", {}).get("allow", []),
            "deny": vnx_template.get("permissions", {}).get("deny", []),
        },
        "hooks": vnx_template.get("hooks", {}),
        "_vnx_meta": vnx_template.get("_vnx_meta", {}),
    }
    return result


def backup_settings(settings_path: str) -> str | None:
    """Create a timestamped backup of settings.json. Returns backup path."""
    if not os.path.isfile(settings_path):
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{settings_path}.bak.{ts}"
    shutil.copy2(settings_path, backup_path)
    return backup_path


def write_settings(settings_path: str, data: dict) -> None:
    """Atomically write settings.json."""
    tmp_path = f"{settings_path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, settings_path)


def validate_settings(data: dict) -> list[str]:
    """Validate settings structure. Returns list of issues."""
    issues = []

    if not isinstance(data, dict):
        issues.append("settings.json root must be a JSON object")
        return issues

    # Check required sections
    if "hooks" not in data:
        issues.append("missing 'hooks' section")
    elif not isinstance(data["hooks"], dict):
        issues.append("'hooks' must be an object")

    if "permissions" not in data:
        issues.append("missing 'permissions' section")
    elif not isinstance(data["permissions"], dict):
        issues.append("'permissions' must be an object")
    else:
        perms = data["permissions"]
        if "allow" not in perms and "deny" not in perms:
            issues.append("'permissions' should have 'allow' or 'deny' arrays")
        for key in ("allow", "deny"):
            if key in perms and not isinstance(perms[key], list):
                issues.append(f"'permissions.{key}' must be an array")

    if "env" in data and not isinstance(data["env"], dict):
        issues.append("'env' must be an object")

    # Check hooks structure
    if "hooks" in data and isinstance(data["hooks"], dict):
        for event_name, matchers in data["hooks"].items():
            if not isinstance(matchers, list):
                issues.append(f"hooks.{event_name} must be an array")
                continue
            for i, matcher_block in enumerate(matchers):
                if not isinstance(matcher_block, dict):
                    issues.append(f"hooks.{event_name}[{i}] must be an object")
                    continue
                if "hooks" not in matcher_block:
                    issues.append(f"hooks.{event_name}[{i}] missing 'hooks' array")

    return issues


def diff_summary(existing: dict | None, merged: dict) -> list[str]:
    """Generate a human-readable diff summary."""
    lines = []

    if existing is None:
        lines.append("[regen-settings] Creating new settings.json")
        return lines

    # Env changes
    old_env = existing.get("env", {})
    new_env = merged.get("env", {})
    for key in sorted(set(list(old_env.keys()) + list(new_env.keys()))):
        if key not in old_env:
            lines.append(f"  env: +{key}={new_env[key]}")
        elif key not in new_env:
            lines.append(f"  env: -{key}")
        elif old_env[key] != new_env[key]:
            lines.append(f"  env: ~{key}: {old_env[key]} -> {new_env[key]}")

    # Permission changes
    old_perms = existing.get("permissions", {})
    new_perms = merged.get("permissions", {})
    for ptype in ("allow", "deny"):
        old_set = set(old_perms.get(ptype, []))
        new_set = set(new_perms.get(ptype, []))
        added = new_set - old_set
        removed = old_set - new_set
        if added:
            lines.append(f"  permissions.{ptype}: +{len(added)} entries")
        if removed:
            lines.append(f"  permissions.{ptype}: -{len(removed)} entries")

    # Hooks
    old_hooks = existing.get("hooks", {})
    new_hooks = merged.get("hooks", {})
    if old_hooks != new_hooks:
        old_events = set(old_hooks.keys())
        new_events = set(new_hooks.keys())
        lines.append(f"  hooks: replaced ({len(new_events)} event types)")

    return lines


def main():
    parser = argparse.ArgumentParser(description="VNX Settings Merge Engine")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--merge", action="store_true",
                      help="Merge VNX keys into existing settings.json")
    mode.add_argument("--full", action="store_true",
                      help="Generate complete settings.json from VNX template")
    mode.add_argument("--validate", action="store_true",
                      help="Validate existing settings.json structure")

    parser.add_argument("--project-root", required=True,
                        help="Project root directory")
    parser.add_argument("--vnx-home", default=None,
                        help="VNX home directory (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip backup of existing settings.json")
    parser.add_argument("--json", action="store_true",
                        help="Output result as JSON (for scripting)")

    args = parser.parse_args()

    project_root = os.path.realpath(args.project_root)
    vnx_home = args.vnx_home or find_vnx_home(project_root)
    settings_path = os.path.join(project_root, ".claude", "settings.json")

    # Validate mode
    if args.validate:
        existing = load_existing_settings(settings_path)
        if existing is None:
            print("ERROR: settings.json not found", file=sys.stderr)
            sys.exit(1)
        issues = validate_settings(existing)
        if issues:
            for issue in issues:
                print(f"  FAIL: {issue}", file=sys.stderr)
            sys.exit(1)
        else:
            print("[regen-settings] settings.json structure is valid")
            sys.exit(0)

    # Load template
    vnx_template = load_template(vnx_home, project_root)

    if args.full:
        # Full mode: generate from scratch
        existing = load_existing_settings(settings_path)
        result = generate_full_settings(vnx_template)

        issues = validate_settings(result)
        if issues:
            print("ERROR: Generated settings failed validation:", file=sys.stderr)
            for issue in issues:
                print(f"  {issue}", file=sys.stderr)
            sys.exit(1)

        if args.dry_run:
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print("[regen-settings] --full --dry-run: would create settings.json")
                if existing:
                    print("[regen-settings] WARNING: would overwrite existing settings.json")
                print(json.dumps(result, indent=2))
            sys.exit(0)

        # Backup existing if present
        if existing and not args.no_backup:
            bak = backup_settings(settings_path)
            print(f"[regen-settings] Backup: {bak}")

        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        write_settings(settings_path, result)
        print(f"[regen-settings] Created: {settings_path} (full)")

    elif args.merge:
        # Merge mode: overlay VNX keys into existing
        existing = load_existing_settings(settings_path)

        if existing is None:
            # No existing file — fall back to full generation
            print("[regen-settings] No existing settings.json, generating full")
            result = generate_full_settings(vnx_template)
        else:
            result = merge_settings(existing, vnx_template)

        issues = validate_settings(result)
        if issues:
            print("ERROR: Merged settings failed validation:", file=sys.stderr)
            for issue in issues:
                print(f"  {issue}", file=sys.stderr)
            sys.exit(1)

        summary = diff_summary(existing, result)

        if args.dry_run:
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print("[regen-settings] --merge --dry-run:")
                if summary:
                    for line in summary:
                        print(line)
                else:
                    print("  (no changes)")
            sys.exit(0)

        # Backup existing if present
        if existing and not args.no_backup:
            bak = backup_settings(settings_path)
            print(f"[regen-settings] Backup: {bak}")

        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        write_settings(settings_path, result)

        if summary:
            for line in summary:
                print(line)
        print(f"[regen-settings] Updated: {settings_path} (merge)")


if __name__ == "__main__":
    main()
