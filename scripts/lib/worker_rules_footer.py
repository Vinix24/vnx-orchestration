"""worker_rules_footer — worker report-discipline footer block."""

from __future__ import annotations

SENTINEL = "<!-- VNX-WORKER-RULES-FOOTER -->"


def build(role: "str | None", dispatch_id: str, *, permission_enforcement: str = "soft") -> str:
    """Return the worker rules footer block starting with the sentinel.

    The sentinel serves as an idempotent guard: callers check for its presence
    before appending (see dispatch_prepare.prepare()).
    """
    role_label = role or "(none)"
    return (
        f"{SENTINEL}\n\n"
        "## Worker Report Discipline\n\n"
        f"Dispatch-ID: `{dispatch_id}`  |  Role: {role_label}  |  "
        f"Permission enforcement: {permission_enforcement}\n\n"
        "**Required order:**\n\n"
        "1. Complete all implementation work.\n"
        "2. Write your completion report to `.vnx-data/unified_reports/` **FIRST**.\n"
        "3. Emit the receipt (via `append_receipt.py`) **LAST** — only after the report is written.\n\n"
        "**Prohibited:**\n\n"
        "- Do NOT say \"tests passed\" without naming the exact command and pass/fail count.\n"
        "- Do NOT say \"done\" if you left TODO comments, partial features, or unresolved items.\n"
        "- Do NOT emit a receipt before the report is written.\n"
    )
