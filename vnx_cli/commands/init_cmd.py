#!/usr/bin/env python3
"""vnx init — scaffold a new VNX project directory structure.

PR-PIP-2 (clean init footprint): the in-project tree is now tracked config
only — ``.vnx/`` (governance profiles + config.yml), a ``.vnx-project-id``
marker, and an optional ``agents/`` scaffold. The runtime state tree
(dispatches, receipts, logs, state) is created under the *resolved* state root
(a user-data-dir for pip installs), never inside the project map. This keeps
the committed footprint tiny (< 10 KB) and stops a pip-installed VNX from
writing runtime state into the package or the repo.
"""

import os
import sys
from pathlib import Path

from vnx_cli import _engine

GOVERNANCE_PROFILES_YAML = """\
# VNX Governance Profiles
# Each profile defines approval requirements and gate thresholds.

profiles:
  default:
    description: Standard governance — human gate at every dispatch
    approval_required: true
    gates:
      codex: true
      review: true
      ci: true

  lightweight:
    description: Reduced gates for rapid prototyping
    approval_required: true
    gates:
      codex: false
      review: true
      ci: true

  strict:
    description: Regulated environments — all gates mandatory
    approval_required: true
    gates:
      codex: true
      review: true
      ci: true
    extra:
      require_two_reviewers: true
      audit_trail: true
"""

AGENTS_README = """\
# agents/

Place one subdirectory per agent here.

Each agent directory should contain:
- `CLAUDE.md`  — role-specific instructions for that terminal
- (optional) `skills/` — agent-local skill overrides

Example layout:
    agents/
      T1/CLAUDE.md
      T2/CLAUDE.md
      T3/CLAUDE.md
"""

CLAUDE_MD_TEMPLATE = """\
# Agent Instructions

## Role
Define the role for this agent terminal.

## Capabilities
List the tools and capabilities available.

## Workflow
1. Read the dispatch instruction
2. Implement changes
3. Write a completion report to the runtime reports directory

## Rules
- No TODO comments — complete all implementations
- Follow established project patterns
"""

# Runtime subdirs — created under the RESOLVED state root, not the project map.
VNX_DATA_SUBDIRS = [
    "dispatches/pending",
    "dispatches/active",
    "dispatches/completed",
    "dispatches/rejected",
    "dispatches/failed",
    "receipts",
    "unified_reports",
    "logs",
    "state",
    "pids",
    "locks",
]


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a tmp file + os.replace (atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _write_project_id_marker(project_dir: Path, project_id: str) -> bool:
    """Write/refresh ``.vnx-project-id`` first line to ``project_id``.

    Preserves any orchestrator/agent ids already on lines 2-3. Returns True if
    the file was created/changed, False if it already carried this id.
    """
    marker = project_dir / _engine.PROJECT_FILE_NAME
    existing_lines: list[str] = []
    if marker.is_file():
        try:
            existing_lines = marker.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing_lines = []
    if existing_lines and existing_lines[0].strip() == project_id:
        return False
    rest = existing_lines[1:] if len(existing_lines) > 1 else []
    _atomic_write(marker, "\n".join([project_id, *rest]) + "\n")
    return True


def _is_within(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def vnx_init(args) -> int:
    project_dir = Path(args.project_dir).resolve()

    print(f"Initialising VNX project at: {project_dir}")

    try:
        project_id = _engine.derive_project_id(
            project_dir, explicit=getattr(args, "project_id", None)
        )
    except ValueError as exc:
        print(f"  error: {exc}", file=sys.stderr)
        return 1

    # --- tracked, in-project config (tiny footprint) ----------------------
    vnx_dir = project_dir / ".vnx"
    vnx_dir.mkdir(parents=True, exist_ok=True)

    if _write_project_id_marker(project_dir, project_id):
        print(f"  created {_engine.PROJECT_FILE_NAME} (project_id: {project_id})")
    else:
        print(f"  exists  {_engine.PROJECT_FILE_NAME} (project_id: {project_id})")

    profiles_path = vnx_dir / "governance_profiles.yaml"
    if not profiles_path.exists():
        profiles_path.write_text(GOVERNANCE_PROFILES_YAML)
        print(f"  created {profiles_path.relative_to(project_dir)}")
    else:
        print(f"  exists  {profiles_path.relative_to(project_dir)}")

    # --- resolved runtime root (OUTSIDE the project map for fresh installs)
    data_root = _engine.resolve_data_root(project_dir)

    config_path = vnx_dir / "config.yml"
    if not config_path.exists():
        _atomic_write(
            config_path,
            "# Generated by vnx init\n"
            f'project_root: "{project_dir}"\n'
            f'project_id: "{project_id}"\n'
            f'vnx_data_dir: "{data_root}"\n',
        )
        print(f"  created {config_path.relative_to(project_dir)}")
    else:
        print(f"  exists  {config_path.relative_to(project_dir)}")

    # --- optional agents/ scaffold (tracked, small) -----------------------
    agents_dir = project_dir / "agents"
    agents_dir.mkdir(exist_ok=True)
    readme_path = agents_dir / "README.md"
    if not readme_path.exists():
        readme_path.write_text(AGENTS_README)
        print(f"  created {readme_path.relative_to(project_dir)}")
    else:
        print(f"  exists  {readme_path.relative_to(project_dir)}")

    claude_md = agents_dir / "CLAUDE.md.template"
    if not claude_md.exists():
        claude_md.write_text(CLAUDE_MD_TEMPLATE)
        print(f"  created {claude_md.relative_to(project_dir)}")
    else:
        print(f"  exists  {claude_md.relative_to(project_dir)}")

    # --- runtime layout under the resolved state root ---------------------
    for subdir in VNX_DATA_SUBDIRS:
        (data_root / subdir).mkdir(parents=True, exist_ok=True)

    inside_project = _is_within(data_root, project_dir)
    print()
    print(f"Runtime state: {data_root}")
    if inside_project:
        print("  (legacy project-local layout — pre-existing .vnx-data preserved)")
    else:
        print("  (outside the project map — nothing runtime is committed)")

    print()
    print("VNX project initialised.")
    print()
    print("Next steps:")
    print("  1. Review .vnx/governance_profiles.yaml and adjust gates")
    print("  2. Add agent instructions in agents/<terminal>/CLAUDE.md")
    print("  3. Run `vnx doctor` to validate your setup")
    if inside_project:
        print("  4. Add .vnx-data/ to .gitignore (runtime state — do not commit)")

    return 0
