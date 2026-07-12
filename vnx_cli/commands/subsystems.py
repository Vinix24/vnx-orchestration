#!/usr/bin/env python3
"""vnx subsystems — render the live subsystem cockpit SSOT (MAP + ON/OFF + HEALTH).

Rowset is the union of three sources (framework-status-audit-and-cockpit PR-3):
  (a) ``config_registry.CONFIG_REGISTRY`` + ``all_effective()`` — flag-backed
      subsystems. Several flags can share one subsystem (e.g. every
      intelligence-tuning flag maps to ``intelligence-self-learning-loop``);
      ``_ROW_ORDER`` below pins exactly one canonical flag per subsystem row
      (the master on/off switch), matching ``docs/core/SUBSYSTEMS.md``.
  (b) ``config_registry.CONFIG_REGISTRY_SUBSYSTEMS`` — flag-less kernel
      subsystems (``phantom_guard``, ``dispatch-plan``, ...).
  (c) ``health_beacon.all_beacons(data_dir)`` — live HEALTH only. The beacon
      root is ``VNX_DATA_DIR`` (health_beacon writes/reads under
      ``VNX_DATA_DIR/health``), NOT ``VNX_STATE_DIR``.

``--md`` emits the exact ``docs/core/SUBSYSTEMS.md`` ledger table. The
deterministic columns (subsystem/what/flag/status) are always regenerated from
the registry; ``.github/workflows/subsystems-drift.yml`` diffs exactly those
columns against the committed file — the dynamic ``health`` column is excluded
so a live probe never forces a ledger recommit. Before a beacon exists for a
subsystem, health falls back to the value committed in the seed table (parsed
from the file itself), so the round-trip is byte-identical before PR-5..7 land.

``--probe`` is this PR's owned flag surface (PR-5 supplies the aggregator, via
a guarded import, with no edit back to this file). A subsystem with no probe
registered in ``EFFECTIVENESS_PROBES`` reports ``unknown`` / ``"no probe
registered"``; if the aggregator module itself is absent, every row does.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from vnx_cli import _engine

_engine.ensure_engine_on_path()

# Long-form ledger status text (docs/core/SUBSYSTEMS.md legend) vs the short
# registry code (config_registry.ALLOWED_STATUSES). Pure display mapping —
# no runtime behaviour hinges on it.
_STATUS_DISPLAY: Dict[str, str] = {
    "LIVE": "LIVE",
    "PARK": "PARK-with-trigger",
    "CUT": "CUT",
    "ACTIVATE": "ACTIVATE-and-measure",
    "SCOPE": "SCOPE",
    "COCKPIT": "COCKPIT",
}

# Subsystem-level "what" narrative for flag-backed CONFIG_REGISTRY subsystems.
# ConfigEntry.description is per-FLAG (e.g. VNX_MIGRATION_SYSTEM's own text);
# the ledger's "what" column is per-SUBSYSTEM. Text is verbatim from the PR-1
# seed for the 10 subsystems that already had a ledger row; the 5 new ones
# (registered pre-PR-1 but never added to the hand-seeded ledger) get concise
# text authored here.
SUBSYSTEM_DESCRIPTIONS: Dict[str, str] = {
    "migration-mechanisms": (
        "Schema-evolution surfaces (42 SQL files + 6 appliers). Consolidation "
        "PARKed pending inventory-lock."
    ),
    "governance-enforcement-stack": (
        "Receipt hash-chain + signed attestation + evidence-bound merge gate. "
        "SURFACED here; enforcement wiring deferred."
    ),
    "receipt-hash-chain": "Tamper-evident NDJSON hash-chain (ADR-029).",
    "signed-attestation": "SSH-signed PR attestation manifests (ADR-027).",
    "evidence-bound-gate": "D3 evidence-bound merge gate.",
    "intelligence-self-learning-loop": (
        "Daily pattern learning, skill refinements, confidence updates."
    ),
    "dream-consolidation": "Nightly memory consolidation + pending review dispatch.",
    "injection-effectiveness-eval-loop": (
        "Instrument WHY patterns are ignored before tuning generation."
    ),
    "plan-gate-panel": "5-model deliberation panel for plan-first enforcement.",
    "plan-gate-task-class-scope": (
        "Restrict panel to complex features; skip trivial tracks. Enforcement "
        "deferred to review-floor-enforcer."
    ),
    "cheap-recon-scout": "Cheap-model scout recon pre-pass in the dispatch door (fail-open).",
    "horizon-planning": "Autonomous roadmap auto-next loading (starts work unattended).",
    "headless-dispatch-routing": "Headless dispatch routing mode selector.",
    "central-db-routing": (
        "Central-DB read mode for the runtime coordination store "
        "(per-project vs central vs shadow)."
    ),
    "cross-project-federation": "Cross-project intelligence federation (not yet implemented).",
}

# Ledger row order (framework-status-audit-and-cockpit PR-3). Grouped by
# status, matching docs/core/SUBSYSTEMS.md. A subsystem discovered in the
# registry but absent here is a build error (see _order_key) — the ledger
# stays a deliberately curated MAP, not an alphabetical dump.
_ROW_ORDER: List[str] = [
    # LIVE
    "provider-routing",
    "git-grounded-reconcile",
    "phantom_guard",
    "tmux-operational-scar",
    "zero-llm-injection",
    "dispatch-plan",
    "test-suite",
    "cheap-recon-scout",
    "horizon-planning",
    "headless-dispatch-routing",
    "central-db-routing",
    # PARK-with-trigger / CUT
    "migration-mechanisms",
    "within-db-tenancy",
    "docs-bloat",
    "governance-enforcement-stack",
    "receipt-hash-chain",
    "signed-attestation",
    "evidence-bound-gate",
    # ACTIVATE-and-measure
    "intelligence-self-learning-loop",
    "dream-consolidation",
    "injection-effectiveness-eval-loop",
    "cross-project-federation",
    # SCOPE
    "plan-gate-panel",
    "plan-gate-task-class-scope",
    # COCKPIT
    "subsystem-cockpit",
    "effectiveness-probe-framework",
]


def _order_key(subsystem: str) -> int:
    try:
        return _ROW_ORDER.index(subsystem)
    except ValueError:
        raise ValueError(
            f"subsystem {subsystem!r} discovered in config_registry but not "
            "listed in vnx_cli/commands/subsystems.py:_ROW_ORDER — add it to "
            "the cockpit ledger order (and SUBSYSTEM_DESCRIPTIONS if it has a "
            "flag) before it can render."
        ) from None


def _canonical_flags(cr) -> Dict[str, str]:
    """subsystem -> the one flag key shown as the ledger row's ``flag``.

    CONFIG_REGISTRY is insertion-ordered; when several flags share a
    subsystem the LAST-inserted one wins (PR-2's net-new master-switch flags
    are appended after the pre-existing tuning flags they group).
    """
    canonical: Dict[str, str] = {}
    for key, entry in cr.CONFIG_REGISTRY.items():
        if entry.subsystem:
            canonical[entry.subsystem] = key
    return canonical


def build_rows(project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Rowset = (a) CONFIG_REGISTRY (canonical flag per subsystem) union (b)
    CONFIG_REGISTRY_SUBSYSTEMS (flag-less). Health is NOT populated here —
    see _attach_health."""
    import config_registry as cr

    rows: List[Dict[str, Any]] = []

    for subsystem, meta in cr.CONFIG_REGISTRY_SUBSYSTEMS.items():
        status = meta.get("status", "COCKPIT")
        rows.append({
            "subsystem": subsystem,
            "what": meta.get("description", ""),
            "flag": None,
            "status": status,
            "status_display": _STATUS_DISPLAY.get(status, status),
            "effective_value": None,
            "provenance": "config_registry_subsystems",
        })

    for subsystem, flag_key in _canonical_flags(cr).items():
        entry = cr.CONFIG_REGISTRY[flag_key]
        status = entry.status or "COCKPIT"
        rows.append({
            "subsystem": subsystem,
            "what": SUBSYSTEM_DESCRIPTIONS.get(subsystem, entry.description),
            "flag": flag_key,
            "status": status,
            "status_display": _STATUS_DISPLAY.get(status, status),
            "effective_value": cr.get(flag_key, project_id),
            "provenance": "config_registry",
        })

    rows.sort(key=lambda r: _order_key(r["subsystem"]))
    return rows


def _parse_seed_health(engine_root: Path) -> Dict[str, str]:
    """Parse the health cell out of the committed docs/core/SUBSYSTEMS.md
    ledger table — the fallback used until a live beacon exists for a
    subsystem (PR-5..7). Returns {} if the file is missing/unparseable."""
    path = engine_root / "docs" / "core" / "SUBSYSTEMS.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 5:
            continue
        subsystem, _what, _flag, _status, health = cells
        if subsystem in ("subsystem", "") or set(subsystem) <= {"-"}:
            continue
        out[subsystem] = health
    return out


def _run_registered_probes(data_dir: Path) -> Optional[Dict[str, Any]]:
    """Guarded import of scripts/lib/subsystem_health.py (PR-5). Returns None
    when the module does not exist yet — the caller then reports 'unknown' /
    'no probe registered' for every subsystem. PR-3 owns this flag surface;
    PR-5 supplies the module with no edit back to this file.

    Beacons are written under the same resolved ``data_dir`` (VNX_DATA_DIR)
    this CLI already uses for reading them, rather than letting the
    aggregator re-resolve it independently."""
    try:
        from subsystem_health import aggregate  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return aggregate(state_dir=data_dir)
    except Exception as exc:  # probe internals are owned by PR-5+; never crash the CLI
        return {"__error__": str(exc)}


def _attach_health(
    rows: List[Dict[str, Any]],
    data_dir: Path,
    seed_health: Dict[str, str],
    use_probe: bool,
) -> None:
    from health_beacon import all_beacons

    if use_probe:
        probe_results = _run_registered_probes(data_dir)
        for row in rows:
            result = probe_results.get(row["subsystem"]) if probe_results else None
            if isinstance(result, dict) and "status" in result:
                status = result.get("status", "unknown")
                signal = result.get("signal", "")
                row["health"] = f"{status} — {signal}" if signal else status
                row["last_signal"] = signal
            else:
                row["health"] = "unknown"
                row["last_signal"] = "no probe registered"
        return

    beacons = all_beacons(data_dir)  # data_dir = VNX_DATA_DIR, not VNX_STATE_DIR
    for row in rows:
        subsystem = row["subsystem"]
        beacon = beacons.get(subsystem)
        if beacon:
            health = beacon.get("health", "unknown")
            detail = beacon.get("details") or {}
            signal = detail.get("signal") or beacon.get("status", "")
            row["health"] = f"{health} — {signal}" if signal else health
            row["last_signal"] = beacon.get("last_run_iso", "")
        else:
            row["health"] = seed_health.get(subsystem, "unknown — no probe yet")
            row["last_signal"] = ""


def _escape_cell(text: str) -> str:
    """Escape literal ``|`` so free-text cells never fracture the GFM table."""
    return text.replace("|", "\\|")


def _render_md(rows: List[Dict[str, Any]]) -> str:
    lines = ["| subsystem | what | flag | status | health |",
             "|-----------|------|------|--------|--------|"]
    for row in rows:
        flag = f"`{row['flag']}`" if row["flag"] else "—"
        lines.append(
            f"| {row['subsystem']} | {_escape_cell(row['what'])} | {flag} | "
            f"{row['status_display']} | {_escape_cell(row['health'])} |"
        )
    return "\n".join(lines)


def _render_table(rows: List[Dict[str, Any]]) -> str:
    header = f"  {'SUBSYSTEM':<34} {'STATUS':<10} {'FLAG':<26} {'EFFECTIVE':<10} {'HEALTH'}"
    lines = [header, "  " + "-" * len(header.strip())]
    for row in rows:
        flag = row["flag"] or "—"
        effective = row["effective_value"] if row["effective_value"] is not None else "—"
        health = (row.get("health") or "unknown")[:60]
        lines.append(
            f"  {row['subsystem']:<34} {row['status']:<10} {flag:<26} "
            f"{str(effective):<10} {health}"
        )
    return "\n".join(lines)


def vnx_subsystems(args) -> int:
    project_id = getattr(args, "project_id", None)
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    emit_json = getattr(args, "json", False)
    emit_md = getattr(args, "md", False)
    use_probe = getattr(args, "probe", False)

    engine_root = _engine.engine_root()
    data_dir = _engine.resolve_data_root(project_dir)

    rows = build_rows(project_id)
    seed_health = _parse_seed_health(engine_root)
    _attach_health(rows, data_dir, seed_health, use_probe)

    if emit_md:
        print(_render_md(rows))
        return 0

    if emit_json:
        print(json.dumps({"project_id": project_id, "subsystems": rows}, indent=2))
        return 0

    print(_render_table(rows))
    return 0
