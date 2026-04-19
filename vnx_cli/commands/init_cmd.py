#!/usr/bin/env python3
"""vnx init — scaffold a new VNX project directory structure."""

import sys
from pathlib import Path

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
3. Write a completion report to .vnx-data/unified_reports/

## Rules
- No TODO comments — complete all implementations
- Follow established project patterns
"""

VNX_DATA_SUBDIRS = [
    "dispatches/pending",
    "dispatches/active",
    "dispatches/completed",
    "receipts",
    "unified_reports",
    "logs",
]


def vnx_init(args) -> int:
    project_dir = Path(args.project_dir).resolve()

    print(f"Initialising VNX project at: {project_dir}")

    # .vnx/ config dir
    vnx_dir = project_dir / ".vnx"
    vnx_dir.mkdir(parents=True, exist_ok=True)

    # governance_profiles.yaml
    profiles_path = vnx_dir / "governance_profiles.yaml"
    if not profiles_path.exists():
        profiles_path.write_text(GOVERNANCE_PROFILES_YAML)
        print(f"  created {profiles_path.relative_to(project_dir)}")
    else:
        print(f"  exists  {profiles_path.relative_to(project_dir)}")

    # agents/ dir
    agents_dir = project_dir / "agents"
    agents_dir.mkdir(exist_ok=True)
    readme_path = agents_dir / "README.md"
    if not readme_path.exists():
        readme_path.write_text(AGENTS_README)
        print(f"  created {readme_path.relative_to(project_dir)}")
    else:
        print(f"  exists  {readme_path.relative_to(project_dir)}")

    # Example CLAUDE.md template in agents/
    claude_md = agents_dir / "CLAUDE.md.template"
    if not claude_md.exists():
        claude_md.write_text(CLAUDE_MD_TEMPLATE)
        print(f"  created {claude_md.relative_to(project_dir)}")
    else:
        print(f"  exists  {claude_md.relative_to(project_dir)}")

    # .vnx-data/ full structure
    vnx_data = project_dir / ".vnx-data"
    for subdir in VNX_DATA_SUBDIRS:
        path = vnx_data / subdir
        path.mkdir(parents=True, exist_ok=True)
    print(f"  created {vnx_data.relative_to(project_dir)}/ (dispatch tree)")

    print()
    print("VNX project initialised.")
    print()
    print("Next steps:")
    print("  1. Review .vnx/governance_profiles.yaml and adjust gates")
    print("  2. Add agent instructions in agents/<terminal>/CLAUDE.md")
    print("  3. Run `vnx doctor` to validate your setup")
    print("  4. Add .vnx-data/ to .gitignore (runtime state — do not commit)")

    return 0
