#!/usr/bin/env python3
"""Pre-flight skill-coverage scanner for central VNX rollout."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Set

import yaml

logger = logging.getLogger(__name__)


def _norm(name: str) -> str:
    return name.lower().strip().lstrip("@").replace("_", "-")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("check_skill_coverage: cannot read %s: %s", path, e)
        return None


def _discover_skills_dir(project_root: Path) -> Path | None:
    for c in (project_root / ".vnx" / "skills", project_root / ".claude" / "skills", project_root / "skills"):
        if c.is_dir():
            return c
    return None


def _scan_dispatches(project_root: Path) -> Set[str]:
    refs: Set[str] = set()
    dispatches = project_root / ".vnx-data" / "dispatches"
    if not dispatches.is_dir():
        return refs
    for path in dispatches.rglob("*"):
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        for m in re.finditer(r"^Role:\s*(.+)$", text, re.MULTILINE):
            refs.add(_norm(m.group(1)))
    return refs


def _scan_code_roles(project_root: Path) -> Set[str]:
    refs: Set[str] = set()
    vnx = project_root / ".vnx"
    if vnx.is_dir():
        for path in vnx.rglob("*.yaml"):
            text = _read_text(path)
            if text:
                for m in re.finditer(r"^\s*-?\s*role:\s*([\w\-@]+)$", text, re.MULTILINE):
                    refs.add(_norm(m.group(1)))
    for path in project_root.rglob("*.py"):
        if not path.is_file() or ".vnx-system" in path.parts or ".vnx-overrides" in path.parts:
            continue
        text = _read_text(path)
        if text:
            for m in re.finditer(r'skill_name\s*=\s*["\']([\w\-@]+)["\']', text):
                refs.add(_norm(m.group(1)))
    for ext in (".py", ".yaml", ".yml", ".json"):
        for path in project_root.rglob(f"*{ext}"):
            if not path.is_file() or ".vnx-system" in path.parts or ".vnx-overrides" in path.parts:
                continue
            text = _read_text(path)
            if text:
                for m in re.finditer(r'["\']skill["\']\s*:\s*["\']@?([\w\-]+)["\']', text):
                    refs.add(_norm(m.group(1)))
    return refs


def _is_opted_out(skill_dir: Path) -> bool:
    """Project-local-only skills may carry .vnx-skip-sync to avoid propagation."""
    return (skill_dir / ".vnx-skip-sync").is_file()


def _scan_local_skills_dir(project_root: Path) -> tuple[Set[str], List[dict]]:
    refs: Set[str] = set()
    skipped: List[dict] = []
    skills_dir = _discover_skills_dir(project_root)
    if skills_dir is None:
        return refs, skipped
    skills_yaml = skills_dir / "skills.yaml"
    if skills_yaml.is_file():
        try:
            data = yaml.safe_load(_read_text(skills_yaml)) or {}
            for key in data.get("skills", {}):
                refs.add(_norm(key))
        except (yaml.YAMLError, OSError) as e:
            logger.warning("skill_coverage: skipped %s due to %s: %s", skills_yaml, type(e).__name__, e)
            skipped.append({"path": str(skills_yaml), "error": f"{type(e).__name__}: {e}"})
    for child in skills_dir.iterdir():
        if child.is_dir() and not child.name.startswith(".") and not _is_opted_out(child):
            refs.add(_norm(child.name))
    return refs, skipped


def scan_skill_references(project_root: Path) -> tuple[Set[str], List[dict]]:
    local_refs, skipped = _scan_local_skills_dir(project_root)
    return _scan_dispatches(project_root) | _scan_code_roles(project_root) | local_refs, skipped


def _resolve_central_skills(central: Path | None) -> Path | None:
    if central is not None:
        return central if central.is_dir() else None
    vnx_home = os.environ.get("VNX_HOME")
    if vnx_home:
        p = Path(vnx_home).expanduser().resolve()
        for cand in (p / "skills", p / "current" / "skills"):
            if cand.is_dir():
                return cand
    default = Path.home() / ".vnx-system" / "current" / "skills"
    return default if default.is_dir() else None


def _list_skills_in_dir(skills_dir: Path) -> tuple[Dict[str, Path], List[dict]]:
    available: Dict[str, Path] = {}
    skipped: List[dict] = []
    if not skills_dir.is_dir():
        return available, skipped
    skills_yaml = skills_dir / "skills.yaml"
    if skills_yaml.is_file():
        try:
            data = yaml.safe_load(_read_text(skills_yaml)) or {}
            for key, meta in data.get("skills", {}).items():
                available[_norm(key)] = skills_dir / meta.get("file", key)
        except (yaml.YAMLError, OSError) as e:
            logger.warning("skill_coverage: skipped %s due to %s: %s", skills_yaml, type(e).__name__, e)
            skipped.append({"path": str(skills_yaml), "error": f"{type(e).__name__}: {e}"})
    for child in skills_dir.iterdir():
        if child.is_dir() and not child.name.startswith(".") and not _is_opted_out(child):
            name = _norm(child.name)
            if name not in available:
                available[name] = child
    return available, skipped


def list_available_skills(central: Path | None, overrides: Path | None) -> tuple[Dict[str, Path], List[dict]]:
    available: Dict[str, Path] = {}
    skipped: List[dict] = []
    central_dir = _resolve_central_skills(central)
    if central_dir is not None:
        avail, sk = _list_skills_in_dir(central_dir)
        available |= avail
        skipped += sk
    if overrides is not None and overrides.is_dir():
        avail, sk = _list_skills_in_dir(overrides)
        available |= avail
        skipped += sk
    return available, skipped


def compute_missing(refs: Set[str], available: Dict[str, Path]) -> Set[str]:
    return {r for r in refs if r and _norm(r) not in available}


def format_report(
    refs: Set[str],
    available: Dict[str, Path],
    missing: Set[str],
    json_mode: bool,
    skipped: List[dict] | None = None,
) -> str:
    if skipped is None:
        skipped = []
    if json_mode:
        return json.dumps(
            {
                "referenced": sorted(refs),
                "referenced_count": len(refs),
                "available_count": len(available),
                "missing": sorted(missing),
                "missing_count": len(missing),
                "covered": len(missing) == 0,
                "skipped": skipped,
            },
            indent=2,
        )
    lines = [f"skills referenced: {len(refs)}", f"skills available: {len(available)}"]
    lines.append(f"MISSING: {', '.join(sorted(missing))}" if missing else "All referenced skills are covered.")
    if skipped:
        lines.append(f"SKIPPED (errors): {len(skipped)}")
        for entry in skipped:
            lines.append(f"  - {entry['path']}: {entry['error']}")
    return "\n".join(lines)


def _copy_to_overrides(missing: Set[str], project_root: Path) -> None:
    overrides_dir = project_root / ".vnx-overrides" / "skills"
    for skill in sorted(missing):
        answer = input(f"Copy skill '{skill}' to {overrides_dir}? [y/N] ")
        if answer.strip().lower() == "y":
            overrides_dir.mkdir(parents=True, exist_ok=True)
            dest = overrides_dir / skill
            dest.mkdir(exist_ok=True)
            (dest / "SKILL.md").write_text(f"# {skill}\n\nOverride placeholder.\n")
            print(f"  Created {dest}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-flight skill-coverage scanner for VNX central rollout.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help="Project root to scan (default: cwd)")
    parser.add_argument("--central-skills", type=Path, default=None, help="Central skills directory (default: auto-detect)")
    parser.add_argument("--overrides", type=Path, default=None, help="Override skills directory (default: ./.vnx-overrides/skills/)")
    parser.add_argument("--add-to-overrides", action="store_true", help="Prompt to copy each missing skill into overrides dir")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args(argv)

    project_root = args.project_root.expanduser().resolve()
    overrides = args.overrides.expanduser().resolve() if args.overrides else project_root / ".vnx-overrides" / "skills"

    refs, refs_skipped = scan_skill_references(project_root)
    available, avail_skipped = list_available_skills(args.central_skills, overrides)
    skipped = refs_skipped + avail_skipped
    missing = compute_missing(refs, available)

    print(format_report(refs, available, missing, args.json, skipped))

    if args.add_to_overrides and missing:
        _copy_to_overrides(missing, project_root)
        available, avail_skipped2 = list_available_skills(args.central_skills, overrides)
        skipped += avail_skipped2
        missing = compute_missing(refs, available)
        if missing:
            print(f"Still missing after overrides: {', '.join(sorted(missing))}")

    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
