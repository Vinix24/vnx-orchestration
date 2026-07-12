"""governance_effectiveness_probe — read-only health probe for the
governance-enforcement-stack (framework-status-audit-and-cockpit PR-7).

Reads the REAL hash-chained attestation ledger, ``.vnx-attest/plan-gates.ndjson``
(``.vnx-attest/governed.ndjson`` does NOT exist — kimi finding, verified against
the tree) and verifies its integrity via ``ndjson_hash_chain.verify_chain()``.

Vocabulary (glm/deepseek finding): an ``unchained`` ledger (no entry carries
``prev_hash``) is the EXPECTED PARK state — hash-chaining is off by default
(``VNX_HASH_CHAIN_REQUIRED`` default "0") — and reports ``ok``, not
``produces_crap``. Only a ``broken`` chain (a ``prev_hash`` present but failing
verification — tamper) reports ``produces_crap`` (beacon ``fail``, with
``tamper: True`` in the detail); ``corrupt`` stays owned by the beacon layer for
unreadable JSON (see ``effectiveness_probe.py`` module docstring).

Known detection limitation: this probe inherits ``verify_chain``'s open #1086
prefix-strip weakness — it cannot detect a prefix-strip forgery until #1086's
origin-pinning lands. Recorded verbatim in every result's ``detail``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import project_root  # noqa: E402
from effectiveness_probe import EffectivenessProbe, register_probe  # noqa: E402
from ndjson_hash_chain import verify_chain, walk_chain  # noqa: E402

LEDGER_RELPATH = ".vnx-attest/plan-gates.ndjson"

DETECTION_LIMITATION = (
    "inherits verify_chain's open #1086 prefix-strip weakness: cannot detect a "
    "prefix-strip forgery until #1086's origin-pinning lands"
)


@register_probe("governance-enforcement-stack")
class GovernanceEffectivenessProbe(EffectivenessProbe):
    """Read-only over ``.vnx-attest/plan-gates.ndjson``. No new central-DB table
    (ADR-007 scope statement, PR-5)."""

    subsystem = "governance-enforcement-stack"

    def __init__(self, repo_root: Optional[Path] = None) -> None:
        self._repo_root = Path(repo_root) if repo_root else project_root.resolve_project_root(__file__)

    def _ledger_path(self) -> Path:
        return self._repo_root / LEDGER_RELPATH

    def probe(self) -> Dict[str, Any]:
        path = self._ledger_path()
        if not path.exists():
            return {
                "ledger_exists": False,
                "entry_count": 0,
                "chain_status": "unchained",
                "chain_valid": True,
                "violation_count": 0,
                "detection_limitation": DETECTION_LIMITATION,
            }
        entry_count = sum(1 for _ in walk_chain(path))
        is_valid, violations, status = verify_chain(path)
        return {
            "ledger_exists": True,
            "entry_count": entry_count,
            "chain_status": status,
            "chain_valid": is_valid,
            "violation_count": len(violations),
            "tamper": status == "broken",
            "detection_limitation": DETECTION_LIMITATION,
        }

    def signal(self, raw: Dict[str, Any]) -> str:
        if not raw["ledger_exists"] or raw["entry_count"] == 0:
            return "no plan-gates.ndjson attestations yet"
        base = f"{raw['entry_count']} attestation(s); chain={raw['chain_status']}"
        if raw["chain_status"] == "broken":
            return f"{base} — TAMPER: {raw['violation_count']} violation(s)"
        return base

    def health(self, raw: Dict[str, Any]) -> str:
        if not raw["ledger_exists"] or raw["entry_count"] == 0:
            return "unknown"
        if raw["chain_status"] == "broken":
            return "produces_crap"
        # "unchained" (expected PARK state), "verified", "verified-segmented" all ok.
        return "ok"


__all__ = ["GovernanceEffectivenessProbe", "LEDGER_RELPATH", "DETECTION_LIMITATION"]
