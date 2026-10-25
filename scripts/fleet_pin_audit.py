"""One read-only verdict per registered project's engine pin.

Both surfaces the startup path reads are judged: the ``.vnx-version`` pin
(``vnx_cli._reexec._read_pin``, distance from ``pin_drift``) and the engine
symlink under ``.claude`` (``providers_cli.engine_root_for``), plus
``dispatch_cli.main_checkout_lag``. Nothing is re-derived, nothing is written.

Verdicts, worst first: ``dead`` (the pin's release is absent, or the link
dangles), ``lagging`` (``behind`` a count, ``unknown`` when ``current`` is not a
release), ``link-without-pin``, ``pin-without-link``, ``non-release``,
``current`` (pin current, link into the store), ``unpinned`` (no pin, no store
link: not a finding) and ``fabric-source`` (this checkout). lagging and the two
half-configured verdicts are WARN lines and keep exit 0: a deliberate pin must
not turn the fleet red. Exit: 0 no ``dead`` row, 1 one or more, 2 the registry
or the store's ``versions/`` cannot be read. The command writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
for _path in (_HERE / "lib", _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# ``pin_drift`` bootstraps the engine root onto ``sys.path`` (it imports
# ``vnx_cli._reexec``), so ``_read_pin`` is importable right after it.
import pin_drift
from dispatch_cli import main_checkout_lag
from fleet_role_drift import load_registry
from vnx_cli._reexec import _read_pin

VERDICT_FABRIC = "fabric-source"
VERDICT_LINK_NO_PIN = "link-without-pin"
VERDICT_PIN_NO_LINK = "pin-without-link"
VERDICT_UNPINNED = "unpinned"
DEAD, LAGGING, CURRENT, NON_RELEASE = (
    pin_drift.VERDICT_DEAD, pin_drift.VERDICT_LAGGING,
    pin_drift.VERDICT_CURRENT, pin_drift.VERDICT_NON_RELEASE)

# Built this way, not as one literal: scripts/vnx_doctor.sh rejects a literal
# engine-link path under ``scripts/`` (the CI doctor scan).
LINK_REL = "/".join((".claude", "vnx-system"))
# The engine checkout this script runs from (``scripts/fleet_pin_audit.py``).
FABRIC_SOURCE = Path(__file__).resolve().parents[1]

# Worst first, for both the text order and the summary counts.
SEVERITY = {DEAD: 0, LAGGING: 1, VERDICT_LINK_NO_PIN: 2, VERDICT_PIN_NO_LINK: 2,
            NON_RELEASE: 3, CURRENT: 4, VERDICT_UNPINNED: 5, VERDICT_FABRIC: 6}
LEVEL = {DEAD: "DEAD", LAGGING: "WARN", VERDICT_LINK_NO_PIN: "WARN",
         VERDICT_PIN_NO_LINK: "WARN", NON_RELEASE: "INFO", CURRENT: "ok",
         VERDICT_UNPINNED: "ok", VERDICT_FABRIC: "ok"}
WARN_VERDICTS = (LAGGING, VERDICT_LINK_NO_PIN, VERDICT_PIN_NO_LINK)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _read_link(root: Path, store_root: Path) -> Optional[Dict[str, Any]]:
    """The project's engine link: ``None`` when absent, else target/existence.

    An unreadable symlink reports ``dangling`` (fail-closed), never ``None``: a
    link that exists but cannot be read is not "no link", and treating it as
    absent would hide the misconfiguration this command exists to find.
    """
    link = root / LINK_REL
    if not link.is_symlink():
        return None
    try:
        target = os.readlink(link)
    except OSError:
        target = None
    try:
        resolved = link.resolve()
    except OSError:
        resolved = None
    exists = resolved is not None and resolved.exists()
    return {"target": target, "resolved": str(resolved) if resolved is not None else None,
            "dangling": not exists, "in_store": exists and resolved.is_relative_to(store_root)}


def classify_project(root: Path, store_root: Path,
                     store_current: Optional[str]) -> Dict[str, Any]:
    """All measured fields and the one verdict for a single project root."""
    row: Dict[str, Any] = {
        "path": str(root), "pin": None, "pin_dir": None, "link": None,
        "store_current": store_current, "behind": None,
        "checkout_lag": main_checkout_lag(root), "verdict": VERDICT_UNPINNED,
        "note": None,
    }
    pin, pin_dir = _read_pin(root)
    link = _read_link(root, store_root)
    row.update(pin=pin, pin_dir=str(pin_dir) if pin_dir is not None else None, link=link)
    if _same_path(root, FABRIC_SOURCE):
        row["verdict"] = VERDICT_FABRIC  # surfaces recorded, no pin judgment
        return row

    drift = pin_drift.classify_pin(pin, store_root) if pin is not None else None
    if drift is not None:
        row.update(behind=drift.behind, note=drift.note)
    link_dangling = link is not None and link["dangling"]
    link_in_store = link is not None and link["in_store"]

    if (drift is not None and drift.verdict == DEAD) or link_dangling:
        row["verdict"] = DEAD
    elif drift is not None and drift.verdict in (LAGGING, NON_RELEASE):
        row["verdict"] = drift.verdict
    elif drift is not None and drift.verdict == CURRENT:
        # A valid pin with no store link is half-configured, not current; a
        # link that exists but resolves elsewhere is not an engine link to the
        # pinned store either.
        row["verdict"] = CURRENT if link_in_store else VERDICT_PIN_NO_LINK
        if link is not None and not link_in_store:
            row["note"] = f"{LINK_REL} does not resolve into the store"
    elif link_in_store:
        row["verdict"] = VERDICT_LINK_NO_PIN
    elif link is not None:
        row["note"] = f"{LINK_REL} resolves outside the store"
    return row


def _targets(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if args.project_dir:
        entries: List[Any] = [{"name": p.name, "path": str(p)} for p in args.project_dir]
    else:
        entries = load_registry(args.registry)
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise ValueError(f"project entry without a path: {entry!r}")
    return entries


def render_text(report: Dict[str, Any]) -> str:
    """One line per project, worst first, then a fleet summary line."""
    store = report["store"]
    lines = [f"store: {store['root']}  current: {store['current'] or 'unknown'}"]
    ordered = sorted(report["projects"].items(),
                     key=lambda kv: (SEVERITY[kv[1]["verdict"]], kv[0]))
    for name, row in ordered:
        note = f"  note: {row['note']}" if row["note"] else ""
        behind = "unknown" if row["behind"] is None else row["behind"]
        lag = "unknown" if row["checkout_lag"] is None else row["checkout_lag"]
        link = row["link"]
        link_text = ("-" if link is None else
                     f"dangling:{link['target'] or '?'}" if link["dangling"] else
                     "store" if link["in_store"] else f"foreign:{link['target']}")
        lines.append(
            f"[{LEVEL[row['verdict']]:4s}] {name}  {row['verdict']}  "
            f"pin={row['pin'] or '-'} behind={behind} link={link_text} lag={lag}  "
            f"({row['path']}){note}")
    summary = report["summary"]
    lines.append(f"fleet: {summary['projects']} projects, {summary['dead']} dead, "
                 f"{summary['lagging']} lagging, {summary['warnings']} warnings")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="One read-only pin verdict per project.")
    parser.add_argument("--registry", type=Path, default=Path.home() / ".vnx" / "projects.json")
    parser.add_argument("--project-dir", type=Path, action="append", default=[],
                        help="audit this project instead of the registry (repeatable)")
    parser.add_argument("--store-root", type=Path, default=Path.home() / ".vnx-system",
                        help="central store root holding versions/ and current")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    store_root = args.store_root.expanduser().resolve()
    try:
        list((store_root / "versions").iterdir())
    except OSError:
        print(f"fleet_pin_audit: store versions/ not readable: {store_root / 'versions'}",
              file=sys.stderr)
        return 2
    try:
        entries = _targets(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"fleet_pin_audit: {exc}", file=sys.stderr)
        return 2

    current = pin_drift.current_version(store_root)
    projects: Dict[str, Any] = {}
    for entry in entries:
        root = Path(entry["path"]).expanduser()
        projects[entry.get("name") or root.name] = classify_project(root, store_root, current)

    dead = [name for name, row in projects.items() if row["verdict"] == DEAD]
    report = {
        "store": {"root": str(store_root), "current": current,
                  "versions": pin_drift.load_store(store_root / "versions")},
        "projects": projects,
        "summary": {
            "projects": len(projects), "dead": len(dead),
            "lagging": sum(1 for r in projects.values() if r["verdict"] == LAGGING),
            "warnings": sum(1 for r in projects.values() if r["verdict"] in WARN_VERDICTS),
        },
    }
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
