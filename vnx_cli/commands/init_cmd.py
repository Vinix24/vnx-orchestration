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

# ---------------------------------------------------------------------------
# D5a — attestation trust-root scaffold content
# ---------------------------------------------------------------------------

_ATTEST_ALLOWED_SIGNERS_SCAFFOLD = """\
# VNX attestation trust-root — provisioned by vnx init.
#
# This file lists the SSH public keys that the attestation gate trusts.
# An empty file means NO signatures are accepted — the gate will warn on every
# PR until at least one key is added here.
#
# Format (SSH allowed_signers):
#   <identity> <keytype> <base64-pubkey>
#
# Example:
#   operator@your-repo ssh-ed25519 AAAA...
#
# Operator provisioning step: see docs/governance/KEY_PROVISIONING.md
#   1. Generate a signing key (ssh-keygen -t ed25519)
#   2. Append: <email> <keytype> <pubkey-contents>
#   3. Run: git config gpg.ssh.allowedSignersFile .vnx-attest/allowed_signers
#
# NEVER commit the private key.  This file contains PUBLIC keys only.
"""

_ATTEST_README_CONTENT = """\
# .vnx-attest/

Attestation trust-root for the VNX governance gate.

## Files

- `allowed_signers` — SSH public keys trusted by the attestation gate.
  Provisioned by `vnx init`.  Populated by the operator key-provisioning step.
- `governed.ndjson` — Hash-chained ledger of governed attestation records.
  Written by the signing step at governed build time.
- `adhoc.ndjson` — Ledger of ad-hoc (ungoverned) attestation records.

## Operator provisioning

Before the attestation gate can accept any PR, complete the key-provisioning
step described in:

  docs/governance/KEY_PROVISIONING.md

The private key is NEVER committed here.  This directory contains PUBLIC
keys (in `allowed_signers`) and ledger records only.

## CODEOWNERS protection

`allowed_signers` and the gate workflow (`.github/workflows/attestation-gate.yml`)
are CODEOWNERS-protected — any change requires a maintainer review.  This
prevents a PR from self-authorizing a new signing key.
"""

# Sentinel line used for idempotency detection in CODEOWNERS.
_CODEOWNERS_ATTEST_SENTINEL = ".vnx-attest/allowed_signers"

_CODEOWNERS_ATTEST_BLOCK = """\

# VNX attestation trust-root — provisioned by vnx init.
# Replace @your-github-handle with your actual GitHub username.
.vnx-attest/allowed_signers @your-github-handle
.github/workflows/attestation-gate.yml @your-github-handle
.vnx/governance_enforcement.yaml @your-github-handle
"""


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


def _emit_bootstrap_event(data_root: Path, status: str, error: str = "") -> None:
    """Append an audit event to data_root/events/db_bootstrap.ndjson (ADR-005)."""
    import fcntl
    import json
    from datetime import datetime, timezone

    events_dir = data_root / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    log_path = events_dir / "db_bootstrap.ndjson"
    record: dict = {
        "event_type": "db_bootstrap",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
    }
    if error:
        record["error"] = error
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(log_path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(line)


def _seed_default_pool_config(db_path: Path, project_id: str) -> None:
    """Seed default pool_config + worker_pools rows for project_id. Idempotent.

    ADR-007: UNIQUE(project_id, pool_id) is the natural guard.  Migration 0020
    seeds a ('vnx-dev', 'default') row; this function upgrades that legacy row
    to ``project_id`` instead of inserting a second row.  This prevents the W1
    Phase-2 vnx-dev->pid restamp from colliding on the composite UNIQUE.

    pool_config must be updated/inserted before worker_pools (FK dependency).
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        # FKs are suspended during the seed upgrade: migration 0020 creates a
        # (pool_config='vnx-dev', worker_pools='vnx-dev') pair linked by FK.  When
        # we upgrade both rows to project_id we must avoid a transient FK violation
        # between the two UPDATEs.  The final state is consistent, so re-enabling
        # FKs after COMMIT is safe.
        conn.execute("PRAGMA foreign_keys = OFF")

        # pool_config: ensure exactly one default row for project_id.
        existing = conn.execute(
            "SELECT project_id FROM pool_config WHERE pool_id = ?",
            ("default",),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO pool_config
                    (project_id, pool_id, min_workers, max_workers, target_workers,
                     role_mix_json, provider_mix_json, scale_policy, cooldown_seconds)
                VALUES (?, 'default', 0, 4, 1,
                        '["backend-developer"]',
                        '["claude"]',
                        'queue_depth_v1', 120)
                """,
                (project_id,),
            )
        elif existing[0] == "vnx-dev" and project_id != "vnx-dev":
            conn.execute(
                "UPDATE pool_config SET project_id = ? WHERE pool_id = ? AND project_id = ?",
                (project_id, "default", "vnx-dev"),
            )
        # else: already seeded for project_id (or vnx-dev -> vnx-dev); leave untouched.

        # worker_pools: mirror the same idempotent upgrade so the FK target and
        # the runtime state row stay one-to-one with pool_config.
        existing_wp = conn.execute(
            "SELECT project_id FROM worker_pools WHERE pool_id = ?",
            ("default",),
        ).fetchone()
        if existing_wp is None:
            conn.execute(
                """
                INSERT INTO worker_pools
                    (project_id, pool_id, state, current_size, target_size)
                VALUES (?, 'default', 'idle', 0, 0)
                """,
                (project_id,),
            )
        elif existing_wp[0] == "vnx-dev" and project_id != "vnx-dev":
            conn.execute(
                "UPDATE worker_pools SET project_id = ? WHERE pool_id = ? AND project_id = ?",
                (project_id, "default", "vnx-dev"),
            )

        conn.commit()
        # Re-enable FK enforcement now the consistent (project_id, default) pair
        # is in place.
        conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()


def _bootstrap_runtime_dbs(data_root: Path, project_id: str | None = None) -> None:
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

    When project_id is provided, inserts a default pool_config + worker_pools row
    for that project so vnx pool status works immediately after migrate.

    Raises RuntimeError (or the original exception) if any core step fails.
    quality_intelligence.db is advisory and warns instead of raising.
    """
    print()
    print("Bootstrapping runtime databases...")

    state_dir = data_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # runtime_coordination.db — CORE: must succeed, any failure raises.
    from coordination_db import init_schema, db_path_from_state_dir  # type: ignore
    init_schema(state_dir)
    db_path = db_path_from_state_dir(state_dir)

    # Step 2: add project_id columns (migration 0010) before numbered runners.
    # init_schema v1-v10 creates dispatches via CREATE TABLE IF NOT EXISTS, so
    # the column is absent on a fresh DB. run_runtime_coordination_migration
    # adds it idempotently; auto_apply 0022 then safely SELECTs project_id.
    from project_id_migration import run_runtime_coordination_migration  # type: ignore
    run_runtime_coordination_migration(db_path)

    # Step 3: numbered migration runners 0017+ (tracks, pool, dream, claim) — CORE.
    from migrations.auto_apply import auto_apply  # type: ignore
    auto_apply(db_path)

    # Safety: ensure runtime_schema_version table exists and has at least version 10
    # stamped. The migration chain creates and stamps it, but if init_schema ran
    # against a packaged wheel where the schema SQL path resolves differently, the
    # table may be absent — CREATE TABLE IF NOT EXISTS ensures the INSERT OR IGNORE
    # below never hits "no such table: runtime_schema_version" (swallowed in caller).
    import sqlite3 as _sqlite3
    with _sqlite3.connect(str(db_path)) as _conn:
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS runtime_schema_version ("
            "version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            "description TEXT NOT NULL)"
        )
        _conn.execute(
            "INSERT OR IGNORE INTO runtime_schema_version (version, description) "
            "VALUES (10, 'runtime_coordination bootstrap')"
        )

    # Seed default pool_config + worker_pools row for this project so that
    # vnx pool status works immediately after vnx init / vnx migrate.
    if project_id:
        _seed_default_pool_config(db_path, project_id)

    print("  bootstrapped runtime_coordination.db")

    # ADR-005: emit ledger event so bootstrap is auditable.
    try:
        _emit_bootstrap_event(data_root, status="success")
    except Exception:
        pass  # advisory — never block bootstrap for an audit event

    # quality_intelligence.db — dream_cycles, code_snippets, etc. (advisory).
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


def _provision_attest_trust_root(project_dir: Path, force: bool) -> None:
    """Create .vnx-attest/ scaffold + idempotently add CODEOWNERS trust-root entries.

    D5a: provisions the structure (allowed_signers scaffold + README).
    NEVER generates a signing key — that is an operator Touch-ID step.
    """
    attest_dir = project_dir / ".vnx-attest"
    attest_dir.mkdir(parents=True, exist_ok=True)

    signers_path = attest_dir / "allowed_signers"
    if not signers_path.exists() or force:
        _atomic_write(signers_path, _ATTEST_ALLOWED_SIGNERS_SCAFFOLD)
        print(f"  created .vnx-attest/allowed_signers (scaffold — no key generated)")
    else:
        print(f"  exists  .vnx-attest/allowed_signers")

    readme_path = attest_dir / "README.md"
    if not readme_path.exists() or force:
        _atomic_write(readme_path, _ATTEST_README_CONTENT)
        print(f"  created .vnx-attest/README.md")
    else:
        print(f"  exists  .vnx-attest/README.md")

    # CODEOWNERS — idempotent: append only when the sentinel line is absent.
    codeowners_path = project_dir / "CODEOWNERS"
    existing = ""
    if codeowners_path.exists():
        try:
            existing = codeowners_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    if _CODEOWNERS_ATTEST_SENTINEL in existing:
        print(f"  exists  CODEOWNERS (attest entries already present)")
    else:
        new_content = existing.rstrip() + _CODEOWNERS_ATTEST_BLOCK
        _atomic_write(codeowners_path, new_content)
        print(f"  {'updated' if existing else 'created'} CODEOWNERS (added attest trust-root entries)")


def _provision_github_workflow(project_dir: Path) -> None:
    """Copy attestation-gate.yml from the engine template into .github/workflows/.

    D5b: idempotent — skips if the file already exists.  Never clobbers an
    existing workflow.  If the template is absent (non-standard install), warns
    and skips so the rest of init is not affected.
    """
    # Template is shipped in the engine's templates/init/ tree.
    template_path = _engine.engine_root() / "templates" / "init" / "attestation-gate.yml"
    if not template_path.exists():
        print(
            "  skipped .github/workflows/attestation-gate.yml "
            "(template not found in engine root — provision manually)",
            file=sys.stderr,
        )
        return

    dest = project_dir / ".github" / "workflows" / "attestation-gate.yml"
    if dest.exists():
        print(f"  exists  .github/workflows/attestation-gate.yml")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(dest, template_path.read_text(encoding="utf-8"))
    print(f"  created .github/workflows/attestation-gate.yml")


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
    try:
        _bootstrap_runtime_dbs(data_root, project_id=project_id)
    except Exception as exc:
        print(f"\n  error: DB bootstrap failed: {exc}", file=sys.stderr)
        print(
            "  Runtime databases could not be initialized. Fix the error above and"
            " run `vnx migrate` to retry.",
            file=sys.stderr,
        )
        return 1

    inside_project = _is_within(data_root, project_dir)

    # --- A-11: .claude/ skeleton, local .vnx-data/, version pin, CLAUDE.md -
    print()
    tmpl_root = _templates_root(template)
    ctx = {
        "project_name": project_dir.name,
        "project_id": project_id,
        "vnx_version": __version__,
        "engine_root": str(_engine.engine_root()),
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

    # --- D5a: attestation trust-root (.vnx-attest/ + CODEOWNERS entries) ----
    print()
    print("Provisioning attestation trust-root...")
    _provision_attest_trust_root(project_dir, force)

    # --- D5b: gate workflow (.github/workflows/attestation-gate.yml) ---------
    print()
    print("Provisioning attestation gate workflow...")
    _provision_github_workflow(project_dir)

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
    print(f"  5. Provision a signing key — see docs/governance/KEY_PROVISIONING.md")

    return 0
