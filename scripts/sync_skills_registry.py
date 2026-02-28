#!/usr/bin/env python3
"""
sync_skills_registry.py — Auto-sync skill directories to skills.yaml registry.

Scans .claude/skills/ for subdirectories containing SKILL.md, reads their
frontmatter, and appends missing entries to skills.yaml.

Special entries preserved (never overwritten or removed):
  - vnx-manager  (has terminal: T-MANAGER)
  - t0-orchestrator  (has terminal: T0)

Usage:
    python3 sync_skills_registry.py                      # Apply sync
    python3 sync_skills_registry.py --dry-run            # Show what would be added
    python3 sync_skills_registry.py --skills-dir /path   # Custom skills dir
    python3 sync_skills_registry.py --registry /path     # Custom registry path
"""

import argparse
import re
import sys
from pathlib import Path


# ─── Type inference ────────────────────────────────────────────────

TYPE_KEYWORDS = {
    "implementation": ["implement", "develop", "code", "build"],
    "validation":     ["test", "quality", "qa", "validate"],
    "analysis":       ["analyze", "investigate", "audit"],
    "design":         ["design", "architect", "structure"],
    "security":       ["security", "vulnerability", "hardening"],
    "optimization":   ["performance", "optimize", "profile"],
    "marketing":      ["copy", "content", "marketing", "seo", "campaign"],
    "reporting":      ["report", "excel", "spreadsheet"],
}

DEFAULT_TYPE = "implementation"


def infer_type(skill_name: str, skill_content: str) -> str:
    """Infer skill type from name and SKILL.md content keywords."""
    text = (skill_name + " " + skill_content).lower()
    for skill_type, keywords in TYPE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return skill_type
    return DEFAULT_TYPE


# ─── YAML frontmatter parsing ──────────────────────────────────────

def parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter fields (name, description) from SKILL.md."""
    result = {}
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return result

    block = match.group(1)

    # name:
    m = re.search(r"^name:\s*(.+)$", block, re.MULTILINE)
    if m:
        result["name"] = m.group(1).strip().strip('"').strip("'")

    # description: (may be multiline with '>')
    m = re.search(r"^description:\s*[>|]?\s*\n?(.*?)(?=\n\w|\Z)", block, re.DOTALL | re.MULTILINE)
    if m:
        raw = m.group(1)
        # Collapse indented continuation lines
        lines = [l.strip() for l in raw.strip().splitlines()]
        result["description"] = " ".join(lines)
    else:
        m = re.search(r"^description:\s*(.+)$", block, re.MULTILINE)
        if m:
            result["description"] = m.group(1).strip().strip('"').strip("'")

    return result


# ─── skills.yaml parsing (minimal, preserves file verbatim) ────────

def read_registry(registry_path: Path) -> tuple[str, set]:
    """Return raw file content and set of registered skill keys."""
    content = registry_path.read_text(encoding="utf-8")

    # Extract existing skill keys under `skills:` block
    registered = set()
    in_skills_block = False
    for line in content.splitlines():
        if re.match(r"^skills:\s*$", line):
            in_skills_block = True
            continue
        if in_skills_block:
            # Top-level key under skills (2-space indent, word chars + hyphens)
            m = re.match(r"^  ([\w-]+):\s*$", line)
            if m:
                registered.add(m.group(1))
            # End of skills block: another top-level key at col 0
            elif line and not line.startswith(" ") and not line.startswith("#"):
                in_skills_block = False

    return content, registered


def build_entry_yaml(key: str, name: str, description: str, skill_type: str) -> str:
    """Build a YAML block for a new skill entry (2-space indent)."""
    # Escape description if it contains special chars
    safe_desc = description.replace('"', '\\"') if description else f"@{key} skill"
    lines = [
        f"  {key}:",
        f'    name: "@{key}"',
        f'    file: "{key}/SKILL.md"',
        f"    type: {skill_type}",
    ]
    if safe_desc:
        # Add description as a comment for human readability (not standard YAML field)
        # We omit description from YAML to keep registry lean — only name/file/type matter
        pass
    return "\n".join(lines)


def find_skills_block_end(content: str) -> int:
    """Return the character index of the blank line after the skills: block."""
    lines = content.splitlines(keepends=True)
    in_skills_block = False
    pos = 0
    last_skill_end = -1

    for line in lines:
        stripped = line.rstrip()
        if re.match(r"^skills:\s*$", stripped):
            in_skills_block = True
            pos += len(line)
            continue

        if in_skills_block:
            if stripped and not stripped.startswith(" ") and not stripped.startswith("#"):
                # Left the skills block — insert before this line
                return pos
            last_skill_end = pos + len(line)

        pos += len(line)

    return last_skill_end if last_skill_end != -1 else len(content)


# ─── Main logic ────────────────────────────────────────────────────

PROTECTED_SKILLS = {"vnx-manager", "t0-orchestrator"}


def sync(skills_dir: Path, registry_path: Path, dry_run: bool) -> int:
    """Perform the sync. Returns exit code (0 = success)."""
    if not skills_dir.is_dir():
        print(f"ERROR: skills-dir not found: {skills_dir}", file=sys.stderr)
        return 1

    if not registry_path.is_file():
        print(f"ERROR: registry not found: {registry_path}", file=sys.stderr)
        return 1

    registry_content, registered = read_registry(registry_path)

    added = []
    skipped_registered = []
    skipped_no_skill_md = []

    # Scan for skill subdirectories
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_key = skill_dir.name

        # Skip non-skill dirs
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            skipped_no_skill_md.append(skill_key)
            continue

        if skill_key in registered:
            skipped_registered.append(skill_key)
            continue

        # Parse frontmatter
        content = skill_md.read_text(encoding="utf-8")
        meta = parse_frontmatter(content)
        name = meta.get("name", f"@{skill_key}")
        description = meta.get("description", "")
        skill_type = infer_type(skill_key, content)

        added.append({
            "key": skill_key,
            "name": name,
            "description": description,
            "type": skill_type,
        })

    # Print summary
    print(f"Sync summary:")
    print(f"  Would add:   {len(added)}")
    print(f"  Already registered: {len(skipped_registered)}")
    print(f"  No SKILL.md: {len(skipped_no_skill_md)}")

    if skipped_no_skill_md:
        print(f"\n  Skipped (no SKILL.md): {', '.join(skipped_no_skill_md)}")

    if not added:
        print("\nNothing to add. Registry is up to date.")
        return 0

    print(f"\nNew entries to add:")
    for entry in added:
        print(f"  + {entry['key']} (type: {entry['type']})")
        if entry['description']:
            print(f"      desc: {entry['description'][:80]}")

    if dry_run:
        print("\n[dry-run] No changes written.")
        return 0

    # Build new entries block
    new_entries_lines = ["\n  # --- Auto-synced skills ---\n"]
    for entry in added:
        new_entries_lines.append(build_entry_yaml(
            entry["key"], entry["name"], entry["description"], entry["type"]
        ))
        new_entries_lines.append("")  # blank line between entries

    new_entries_text = "\n".join(new_entries_lines)

    # Insert before the end of the skills: block
    insert_pos = find_skills_block_end(registry_content)
    updated_content = (
        registry_content[:insert_pos]
        + new_entries_text
        + "\n"
        + registry_content[insert_pos:]
    )

    registry_path.write_text(updated_content, encoding="utf-8")
    print(f"\nWrote {len(added)} new entries to {registry_path}")
    return 0


# ─── CLI ───────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync skill directories to skills.yaml registry."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be added without writing changes."
    )
    parser.add_argument(
        "--skills-dir",
        help="Path to .claude/skills/ directory (auto-detected if omitted).",
    )
    parser.add_argument(
        "--registry",
        help="Path to skills.yaml registry (auto-detected if omitted).",
    )
    args = parser.parse_args()

    # Auto-detect paths relative to this script's location
    script_dir = Path(__file__).resolve().parent
    vnx_system_dir = script_dir.parent  # .claude/vnx-system/
    claude_dir = vnx_system_dir.parent  # .claude/

    skills_dir = Path(args.skills_dir) if args.skills_dir else claude_dir / "skills"
    registry_path = Path(args.registry) if args.registry else vnx_system_dir / "skills" / "skills.yaml"

    return sync(skills_dir, registry_path, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
