#!/usr/bin/env python3
"""Pre-flight skill-coverage scanner for central VNX rollout."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Set


def _norm(name: str) -> str:
    return name.lower().strip().lstrip("@").replace("_", "-")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
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


def _scan_local_skills_dir(project_root: Path) -> Set[str]:
    refs: Set[str] = set()
    skills_dir = _discover_skills_dir(project_root)
    if skills_dir is None:
        return refs
    skills_yaml = skills_dir / "skills.yaml"
    if skills_yaml.is_file():
        try:
            import yaml

            data = yaml.safe_load(_read_text(skills_yaml)) or {}
            for key in data.get("skills", {}):
                refs.add(_norm(key))
        except Exception:
            pass
    for child in skills_dir.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            refs.add(_norm(child.name))
    return refs


def scan_skill_references(project_root: Path) -> Set[str]:
    return _scan_dispatches(project_root) | _scan_code_roles(project_root) | _scan_local_skills_dir(project_root)


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


def _list_skills_in_dir(skills_dir: Path) -> Dict[str, Path]:
    available: Dict[str, Path] = {}
    if not skills_dir.is_dir():
        return available
    skills_yaml = skills_dir / "skills.yaml"
    if skills_yaml.is_file():
        try:
            import yaml

            data = yaml.safe_load(_read_text(skills_yaml)) or {}
            for key, meta in data.get("skills", {}).items():
                available[_norm(key)] = skills_dir / meta.get("file", key)
        except Exception:
            pass
    for child in skills_dir.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            name = _norm(child.name)
            if name not in available:
                available[name] = child
    return available


def list_available_skills(central: Path | None, overrides: Path | None) -> Dict[str, Path]:
    available: Dict[str, Path] = {}
    central_dir = _resolve_central_skills(central)
    if central_dir is not None:
        available |= _list_skills_in_dir(central_dir)
    if overrides is not None and overrides.is_dir():
        available |= _list_skills_in_dir(overrides)
    return available


def compute_missing(refs: Set[str], available: Dict[str, Path]) -> Set[str]:
    return {r for r in refs if r and _norm(r) not in available}


def format_report(refs: Set[str], available: Dict[str, Path], missing: Set[str], json_mode: bool) -> str:
    if json_mode:
        return json.dumps(
            {
                "referenced": sorted(refs),
                "referenced_count": len(refs),
                "available_count": len(available),
                "missing": sorted(missing),
                "missing_count": len(missing),
                "covered": len(missing) == 0,
            },
            indent=2,
        )
    lines = [f"skills referenced: {len(refs)}", f"skills available: {len(available)}"]
    lines.append(f"MISSING: {', '.join(sorted(missing))}" if missing else "All referenced skills are covered.")
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

    refs = scan_skill_references(project_root)
    available = list_available_skills(args.central_skills, overrides)
    missing = compute_missing(refs, available)

    print(format_report(refs, available, missing, args.json))

    if args.add_to_overrides and missing:
        _copy_to_overrides(missing, project_root)
        available = list_available_skills(args.central_skills, overrides)
        missing = compute_missing(refs, available)
        if missing:
            print(f"Still missing after overrides: {', '.join(sorted(missing))}")

    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
