"""vnx init — scaffold a new VNX governance project."""

import argparse
import os
import textwrap


_GOVERNANCE_PROFILES_YAML = textwrap.dedent("""\
    # VNX Governance Profiles
    # Defines quality gates and dispatch routing per environment.

    profiles:
      default:
        gates:
          - codex
          - review
          - ci
        merge_strategy: squash
        pr_size_target: 300

      strict:
        gates:
          - codex
          - review
          - ci
          - security
        merge_strategy: squash
        pr_size_target: 150

      relaxed:
        gates:
          - ci
        merge_strategy: merge
        pr_size_target: 500
""")

_AGENTS_README = textwrap.dedent("""\
    # Agents

    Place one directory per agent terminal here, e.g. `T1/`, `T2/`, `T3/`.

    Each agent directory may contain:
    - `CLAUDE.md` — role-specific instructions for the agent
    - Additional context files as needed
""")

_CLAUDE_MD_TEMPLATE = textwrap.dedent("""\
    # Agent Terminal

    ## Role
    Describe this agent's role here (e.g. T1 — Primary Implementation).

    ## Responsibilities
    - Implement features as directed by T0 dispatches
    - Write tests for all changes
    - Commit with conventional commit format

    ## Rules
    - No TODO comments — complete all implementations
    - Follow the VNX dispatch protocol
    - Write completion reports to .vnx-data/unified_reports/
""")

_VNX_DATA_SUBDIRS = [
    "dispatches/pending",
    "dispatches/staging",
    "dispatches/done",
    "receipts",
    "unified_reports",
    "unified_reports/headless",
    "state/review_gates/results",
    "logs",
]


def vnx_init(args: argparse.Namespace) -> int:
    project_dir = os.path.abspath(args.project_dir)

    print(f"Initializing VNX project in: {project_dir}")

    # .vnx/ config dir
    vnx_dir = os.path.join(project_dir, ".vnx")
    _mkdir(vnx_dir)

    # agents/ dir with README
    agents_dir = os.path.join(project_dir, "agents")
    _mkdir(agents_dir)
    _write_if_missing(os.path.join(agents_dir, "README.md"), _AGENTS_README)

    # Example CLAUDE.md in agents/
    _write_if_missing(os.path.join(agents_dir, "CLAUDE.md"), _CLAUDE_MD_TEMPLATE)

    # .vnx-data/ full structure
    vnx_data_dir = os.path.join(project_dir, ".vnx-data")
    for sub in _VNX_DATA_SUBDIRS:
        _mkdir(os.path.join(vnx_data_dir, sub))

    # governance_profiles.yaml
    profiles_path = os.path.join(vnx_dir, "governance_profiles.yaml")
    _write_if_missing(profiles_path, _GOVERNANCE_PROFILES_YAML)

    print("\n[ok] VNX project scaffolded successfully.\n")
    print("Next steps:")
    print("  1. Review agents/CLAUDE.md and customize for your agent terminals.")
    print("  2. Copy or symlink .vnx/ into your agent terminals.")
    print("  3. Run `vnx doctor` to validate your setup.")
    print("  4. Start dispatching with T0.")
    return 0


def _mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_if_missing(path: str, content: str) -> None:
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(content)
