#!/usr/bin/env python3
"""vnx migrate — bootstrap or repair the runtime DB schema.

Applies the full migration chain to runtime_coordination.db and
quality_intelligence.db: v1-v10 base schema + runners 0017/0019/0020/0022/0024/0026.

Idempotent: migrations already at the target version are skipped.
Safe to run on existing installs — no data is deleted.

Use when:
  - After a fresh `vnx init` (handled automatically since vnx 1.0.5+)
  - After upgrading VNX to a version that adds new migration runners
  - To repair a partially-initialized project
"""

from __future__ import annotations

import sys
from pathlib import Path

from vnx_cli import _engine
from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs


def vnx_migrate(args) -> int:
    """Run the full migration chain against the project's runtime DBs."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # Resolve data root via canonical chain (explicit > VNX_DATA_HOME > XDG).
    # ensure_engine_on_path is called inside resolve_data_root.
    try:
        data_root = _engine.resolve_data_root(project_dir)
    except Exception as exc:
        print(f"  error: cannot resolve data root: {exc}", file=sys.stderr)
        return 1

    # Derive project_id so the bootstrap seeds the correct pool_config row.
    project_id = _engine.derive_project_id(project_dir)

    print(f"Migrating VNX runtime databases at: {data_root}")

    try:
        _bootstrap_runtime_dbs(data_root, project_id=project_id)
    except Exception as exc:
        print(f"\n  error: migration failed: {exc}", file=sys.stderr)
        return 1

    print()
    print("Migration complete.")
    return 0
