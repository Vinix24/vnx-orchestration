"""Persistence interface (production stub). Multi-tenant; idempotent per day.

Every write is stamped with tenant_id. The (tenant_id, domain, day) triple is the
idempotency key for daily runs: writing the same triple twice must upsert, never
duplicate. The agent engine persists its findings/reports through this interface.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol


class Storage(Protocol):
    def upsert_run(self, tenant_id: str, domain: str, day: str, payload: dict) -> str:
        """Idempotent per (tenant_id, domain, day). Returns run_id."""
        ...

    def save_finding(self, tenant_id: str, run_id: str, finding: dict) -> None:
        ...

    def latest_report(self, tenant_id: str, domain: str) -> Optional[dict[str, Any]]:
        ...

    def write_report(self, tenant_id: str, domain: str, day: str, report: dict) -> str:
        """Idempotent per (tenant_id, domain, day). Returns report_id."""
        ...
