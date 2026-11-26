from pathlib import Path

from project_scope import resolve_stamp_project_id
from vnx_paths import resolve_central_data_dir


def operator_data_dir(project_id: str | None = None) -> Path:
    """Return the central store; unresolved identity raises TenantUnresolved."""
    # The resolver only inspects the anchor's layout and ancestor markers;
    # no database is needed, and a legacy database must not select the tenant.
    return resolve_central_data_dir(resolve_stamp_project_id(
        explicit=project_id, db_path=Path.cwd() / ".vnx-project-id"))
