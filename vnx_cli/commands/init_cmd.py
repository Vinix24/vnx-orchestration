#!/usr/bin/env python3
"""vnx init — scaffold a new VNX project directory structure.

PR-PIP-2 (clean init footprint): the in-project tree is now tracked config
only — ``.vnx/`` (governance profiles + config.yml), a ``.vnx-project-id``
marker, and an optional ``agents/`` scaffold. The runtime state tree
(dispatches, receipts, logs, state) is created under the *resolved* state root
(a user-data-dir for pip installs), never inside the project map. This keeps
the committed footprint tiny (< 10 KB) and stops a pip-installed VNX from
writing runtime state into the package or the repo.

A-11 extension: adds .claude/ skeleton, local .vnx-data/ layout, .vnx-version
pin, root CLAUDE.md and FEATURE_PLAN.md via Jinja2 templates (default/minimal).
"""

import os
import sys
from pathlib import Path

from vnx_cli import _engine, __version__

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

# Local .vnx-data/ subdirs written by the init scaffold (always project-local).
VNX_DATA_INIT_SUBDIRS = [
    "state",
    "dispatches/pending",
    "dispatches/active",
    "dispatches/completed",
    "events",
    "unified_reports",
]

_VALID_TEMPLATES = {"default", "minimal"}


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


def _templates_root(template: str) -> Path:
    """Return the path to the init template set (default or minimal)."""
    return _engine.engine_root() / "templates" / "init" / template


def _render_template(tmpl_path: Path, ctx: dict) -> str:
    """Render a Jinja2 .j2 template file with ``ctx`` as the template context."""
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
    env = Environment(
        loader=FileSystemLoader(str(tmpl_path.parent)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    return env.get_template(tmpl_path.name).render(**ctx)


def _scaffold_claude_dir(project_dir: Path, tmpl_root: Path, ctx: dict, force: bool) -> None:
    """Create the .claude/ skeleton from templates."""
    claude_dir = project_dir / ".claude"

    t0_md = claude_dir / "terminals" / "T0" / "CLAUDE.md"
    t0_md.parent.mkdir(parents=True, exist_ok=True)
    if not t0_md.exists() or force:
        _atomic_write(t0_md, _render_template(tmpl_root / "terminals" / "T0_claude_md.j2", ctx))
        print(f"  created {t0_md.relative_to(project_dir)}")
    else:
        print(f"  exists  {t0_md.relative_to(project_dir)}")

    skills_dir = claude_dir / "skills"
    if not skills_dir.exists():
        skills_dir.mkdir(parents=True, exist_ok=True)
        print(f"  created {skills_dir.relative_to(project_dir)}/")
    else:
        print(f"  exists  {skills_dir.relative_to(project_dir)}/")

    settings_path = claude_dir / "settings.json"
    if not settings_path.exists() or force:
        _atomic_write(settings_path, _render_template(tmpl_root / "settings.json.j2", ctx))
        print(f"  created {settings_path.relative_to(project_dir)}")
    else:
        print(f"  exists  {settings_path.relative_to(project_dir)}")


def _scaffold_vnx_data_local(project_dir: Path) -> None:
    """Create the project-local .vnx-data/ skeleton."""
    vnx_data = project_dir / ".vnx-data"
    for subdir in VNX_DATA_INIT_SUBDIRS:
        (vnx_data / subdir).mkdir(parents=True, exist_ok=True)
    print(f"  created .vnx-data/ (local scaffold)")


def _write_vnx_version(project_dir: Path, version: str, force: bool) -> bool:
    """Write .vnx-version to ``project_dir``. Returns True if written."""
    path = project_dir / ".vnx-version"
    if path.exists() and not force:
        return False
    _atomic_write(path, version + "\n")
    return True


def _write_root_claude_md(project_dir: Path, tmpl_root: Path, ctx: dict, force: bool) -> None:
    path = project_dir / "CLAUDE.md"
    if path.exists() and not force:
        print("  exists  CLAUDE.md")
        return
    _atomic_write(path, _render_template(tmpl_root / "claude_md.j2", ctx))
    print("  created CLAUDE.md")


def _write_feature_plan(project_dir: Path, force: bool) -> None:
    path = project_dir / "FEATURE_PLAN.md"
    if path.exists() and not force:
        print("  exists  FEATURE_PLAN.md")
        return
    _atomic_write(
        path,
        "# Feature Plan\n\n"
        "<!-- Start a new track: vnx track new <id>"
        " --project-id <proj-id> --title '...' --goal '...' -->\n",
    )
    print("  created FEATURE_PLAN.md")


def _update_gitignore(project_dir: Path) -> None:
    """Append .vnx-data/ to .gitignore if not already present."""
    gi_path = project_dir / ".gitignore"
    marker = ".vnx-data/"
    if gi_path.exists():
        content = gi_path.read_text(encoding="utf-8")
        if marker in content:
            print(f"  exists  .gitignore (already has {marker})")
            return
        new_content = content.rstrip() + "\n\n# VNX runtime state\n" + marker + "\n"
    else:
        new_content = "# VNX runtime state\n" + marker + "\n"
    _atomic_write(gi_path, new_content)
    print(f"  updated .gitignore (added {marker})")


def _bootstrap_runtime_dbs(data_root: Path) -> None:
    """Bootstrap runtime_coordination.db and quality_intelligence.db. Idempotent.

    Runs after vnx init creates the directory scaffold. Applies the full
    migration chain (v1-v10 base schema + project_id columns + 0017/0019/0020/
    0022/0024/0026 runners) so that vnx track list, vnx pool status, and
    vnx dream status return empty results instead of "no such table".

    Step order matters:
      1. init_schema: applies base schema v1 through v10 (dispatches table
         created WITHOUT project_id — CREATE TABLE IF NOT EXISTS is a no-op).
      2. run_runtime_coordination_migration: idempotently adds project_id column
         to dispatches, terminal_leases, etc. (migration 0010). This must run
         BEFORE auto_apply so that migration 0022 (which SELECTs project_id from
         the old dispatches table) does not fail with "no such column: project_id".
      3. auto_apply: applies numbered migration runners 0017+ (tracks, pool,
         dispatches rebuild with CHECK, dream, dispatch claim).
    """
    print()
    print("Bootstrapping runtime databases...")

    state_dir = data_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # runtime_coordination.db — tracks, pool, dispatches, terminal_leases, etc.
    try:
        from coordination_db import init_schema, db_path_from_state_dir  # type: ignore
        init_schema(state_dir)
        db_path = db_path_from_state_dir(state_dir)
        # Step 2: add project_id columns (migration 0010) before numbered runners.
        # init_schema v1-v10 creates dispatches via CREATE TABLE IF NOT EXISTS, so
        # the column is absent on a fresh DB. run_runtime_coordination_migration
        # adds it idempotently; auto_apply 0022 then safely SELECTs project_id.
        try:
            from project_id_migration import run_runtime_coordination_migration  # type: ignore
            run_runtime_coordination_migration(db_path)
        except Exception as exc:
            print(f"  warning: project_id migration skipped: {exc}", file=sys.stderr)
        try:
            from migrations.auto_apply import auto_apply  # type: ignore
            auto_apply(db_path)
        except Exception as exc:
            print(f"  warning: migration auto_apply skipped: {exc}", file=sys.stderr)
        print("  bootstrapped runtime_coordination.db")
    except Exception as exc:
        print(f"  warning: runtime_coordination.db bootstrap failed: {exc}", file=sys.stderr)

    # quality_intelligence.db — dream_cycles, code_snippets, etc.
    try:
        engine_root = _engine.engine_root()
        schema_file = engine_root / "schemas" / "quality_intelligence.sql"
        if schema_file.exists():
            import contextlib
            import io
            from quality_db_init import bootstrap_qi_db  # type: ignore
            qi_db = state_dir / "quality_intelligence.db"
            with contextlib.redirect_stdout(io.StringIO()):
                bootstrap_qi_db(qi_db, schema_file)
            print("  bootstrapped quality_intelligence.db")
        else:
            print("  skipped quality_intelligence.db (schema file not found)", file=sys.stderr)
    except Exception as exc:
        print(f"  warning: quality_intelligence.db bootstrap failed: {exc}", file=sys.stderr)


def vnx_init(args) -> int:
    raw_dir = getattr(args, "project_path", None) or args.project_dir
    project_dir = Path(raw_dir).resolve()
    template = getattr(args, "template", "default") or "default"
    force = getattr(args, "force", False)

    if template not in _VALID_TEMPLATES:
        print(f"  error: unknown template {template!r}. Choose: {', '.join(sorted(_VALID_TEMPLATES))}", file=sys.stderr)
        return 1

    print(f"Initialising VNX project at: {project_dir}")
    print(f"  template: {template}")

    # Safety gate: abort if already initialised and --force not set.
    version_pin = project_dir / ".vnx-version"
    if version_pin.exists() and not force:
        print(
            f"\n  error: .vnx-version already exists ({version_pin.read_text().strip()}).\n"
            "  Use --force to reinitialise.",
            file=sys.stderr,
        )
        return 1

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

    # --- bootstrap runtime DBs so track/pool/dream work immediately ----------
    _bootstrap_runtime_dbs(data_root)

    inside_project = _is_within(data_root, project_dir)

    # --- A-11: .claude/ skeleton, local .vnx-data/, version pin, CLAUDE.md -
    print()
    tmpl_root = _templates_root(template)
    ctx = {
        "project_name": project_dir.name,
        "project_id": project_id,
        "vnx_version": __version__,
    }
    _scaffold_claude_dir(project_dir, tmpl_root, ctx, force)
    # Only create the project-local .vnx-data/ scaffold when the resolved
    # data root is already inside the project directory. For fresh
    # XDG/external installs the runtime dirs are under data_root (created
    # above); silently creating a second local .vnx-data/ would cause the
    # next resolver call to prefer it (step-4 "existing dev checkout" branch),
    # contradicting the config and the init output (PR-PIP-2 clean-footprint).
    if inside_project:
        _scaffold_vnx_data_local(project_dir)

    written = _write_vnx_version(project_dir, __version__, force)
    print(f"  {'created' if written else 'exists '} .vnx-version ({__version__})")

    _write_root_claude_md(project_dir, tmpl_root, ctx, force)
    _write_feature_plan(project_dir, force)
    _update_gitignore(project_dir)

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
    print("  2. Edit .claude/terminals/T0/CLAUDE.md for project-specific T0 instructions")
    print("  3. Run `vnx doctor` to validate your setup")
    print(f"  4. `vnx track new <id> --project-id {project_id} --title '...' --goal '...'`")

    return 0
