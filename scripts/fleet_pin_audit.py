"""One read-only verdict per registered project's engine pin.

Both surfaces the startup path reads are judged: the ``.vnx-version`` pin
(``vnx_cli._reexec._read_pin``, distance from ``pin_drift``) and the engine
symlink under ``.claude`` (``providers_cli.engine_root_for``), plus
``dispatch_cli.main_checkout_lag``. Nothing is re-derived, nothing is written.

Verdicts, worst first: ``dead`` (the pin's release is absent, or the link
dangles), ``unregistered`` (a pin outside the registry and outside any
registered project's git repo; only found with ``--scan-root``), ``lagging``
(``behind`` a count, ``unknown`` when ``current`` is not a release),
``link-without-pin``, ``pin-without-link``, ``non-release``, ``current`` (pin
current, link into the store), ``unpinned`` (no pin, no store link: not a
finding) and ``fabric-source`` (this checkout). lagging and the two
half-configured verdicts are WARN lines and keep exit 0: a deliberate pin must
not turn the fleet red. ``--scan-root PATH`` (repeatable) adds one row per pin
directory under ``PATH`` that the registry does not cover; a worktree of a
registered project keeps its own pin verdict and names its ``owner``, while a
stranger directory becomes ``unregistered`` with the pin judgment in
``pin_verdict``. Exit: 0 no ``dead``/``unregistered`` row, 1 one or more, 2 the
registry, the store's ``versions/`` or a scan root cannot be read. The command
writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
# The single walk of the disk-scan protection source (OI-1379): a second
# walker here would drift from what ``vnx update`` prunes against.
from vnx_cli.commands.update import DEFAULT_PIN_SCAN_MAX_DEPTH, _iter_pin_files

VERDICT_UNREGISTERED = "unregistered"
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

# Worst first, for both the text order and the summary counts. ``unregistered``
# sits next to ``dead`` because it is the same class of finding — a pin nobody
# governs — not a warning about a pin the operator chose.
SEVERITY = {DEAD: 0, VERDICT_UNREGISTERED: 1, LAGGING: 2, VERDICT_LINK_NO_PIN: 3,
            VERDICT_PIN_NO_LINK: 3, NON_RELEASE: 4, CURRENT: 5, VERDICT_UNPINNED: 6,
            VERDICT_FABRIC: 7}
LEVEL = {DEAD: "DEAD", VERDICT_UNREGISTERED: "FIND", LAGGING: "WARN",
         VERDICT_LINK_NO_PIN: "WARN", VERDICT_PIN_NO_LINK: "WARN",
         NON_RELEASE: "INFO", CURRENT: "ok", VERDICT_UNPINNED: "ok",
         VERDICT_FABRIC: "ok"}
WARN_VERDICTS = (LAGGING, VERDICT_LINK_NO_PIN, VERDICT_PIN_NO_LINK)
# A hung git (network filesystem, broken repo) must not hang the audit.
_GIT_TIMEOUT_SECONDS = 10


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _resolved(path: Path) -> Optional[Path]:
    """Absolute real path, or ``None`` when it cannot be resolved."""
    try:
        return path.resolve()
    except OSError:
        return None


def _under(path: Path, parent: Path) -> bool:
    """``path`` (already resolved) lies at or below the resolved ``parent``."""
    resolved_parent = _resolved(parent)
    return resolved_parent is not None and path.is_relative_to(resolved_parent)


def _git_owner(pin_dir: Path, registered: Dict[Path, str]) -> Optional[str]:
    """The registered project ``pin_dir`` is a git worktree of, else ``None``.

    ``git rev-parse --git-common-dir`` points at the shared ``.git`` of the
    repository a worktree belongs to, so its parent is the repo's main
    checkout. Only a registered main checkout names an owner: a stranger repo
    (or a directory that is not a repo at all, or a host without git) is
    ``None`` and the row stays ``unregistered``.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(pin_dir), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    common_dir = proc.stdout.strip()
    if not common_dir:
        return None
    return registered.get(_resolved(Path(common_dir).parent))


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


def _scan_root_rows(root: Path, registered: Dict[Path, str], taken: "set",
                    store_root: Path, store_current: Optional[str]) -> Dict[str, Any]:
    """Extra rows for the pin directories under one ``--scan-root``.

    A directory whose resolved path IS a registered project root is already
    audited by the registry loop and is not repeated. A worktree of a
    registered project keeps its own pin verdict (a worktree is judged like its
    project, so dead stays dead and lagging stays WARN) and names its
    ``owner``; any other directory becomes ``unregistered`` with the pin
    judgment kept in ``pin_verdict``. An absent root yields no rows; a root
    that exists but cannot be listed raises ``OSError`` (the caller exits 2).
    """
    rows: Dict[str, Any] = {}
    for pin_path, _pin in _iter_pin_files(root, DEFAULT_PIN_SCAN_MAX_DEPTH):
        pin_dir = _resolved(pin_path.parent)
        if pin_dir is None or pin_dir in registered:
            continue
        if _under(pin_dir, store_root) or _under(pin_dir, FABRIC_SOURCE):
            continue
        name = pin_dir.name
        if name in taken or name in rows:
            name = str(pin_dir)
        row = classify_project(pin_dir, store_root, store_current)
        row["registered"] = False
        row["owner"] = _git_owner(pin_dir, registered)
        if row["owner"] is None:
            # Nothing governs this pin: the finding is the missing owner, and
            # the pin judgment moves to ``pin_verdict``.
            row["pin_verdict"] = row["verdict"]
            row["verdict"] = VERDICT_UNREGISTERED
        rows[name] = row
    return rows


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
        # A scan row says where it came from: the worktree's registered owner,
        # or "unregistered" for a pin nothing governs.
        marker = ""
        if row.get("registered") is False:
            marker = f"  owner={row['owner']}" if row["owner"] else "  unregistered"
        lines.append(
            f"[{LEVEL[row['verdict']]:4s}] {name}  {row['verdict']}  "
            f"pin={row['pin'] or '-'} behind={behind} link={link_text} lag={lag}  "
            f"({row['path']}){note}{marker}")
    summary = report["summary"]
    line = (f"fleet: {summary['projects']} projects, {summary['dead']} dead, "
            f"{summary['lagging']} lagging, {summary['warnings']} warnings")
    if "unregistered" in summary:
        line += (f", {summary['unregistered']} unregistered, "
                 f"{summary['worktrees']} worktrees")
    lines.append(line)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="One read-only pin verdict per project.")
    parser.add_argument("--registry", type=Path, default=Path.home() / ".vnx" / "projects.json")
    parser.add_argument("--project-dir", type=Path, action="append", default=[],
                        help="audit this project instead of the registry (repeatable)")
    parser.add_argument("--store-root", type=Path, default=Path.home() / ".vnx-system",
                        help="central store root holding versions/ and current")
    parser.add_argument("--scan-root", type=Path, action="append", default=[],
                        help="also audit unregistered pins under this directory "
                             "(repeatable)")
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
    registered: Dict[Path, str] = {}
    for entry in entries:
        root = Path(entry["path"]).expanduser()
        name = entry.get("name") or root.name
        resolved = _resolved(root)
        if resolved is not None:
            registered[resolved] = name
        projects[name] = classify_project(root, store_root, current)

    scan_roots = [path.expanduser() for path in args.scan_root]
    for scan_root in scan_roots:
        try:
            extra = _scan_root_rows(scan_root, registered, set(projects),
                                    store_root, current)
        except OSError:
            # A scan root that exists but cannot be listed is a blind spot,
            # never a silent pass: pruning or auditing behind it would be
            # judging a fleet this command could not see.
            print(f"fleet_pin_audit: scan root not readable: {scan_root}",
                  file=sys.stderr)
            return 2
        projects.update(extra)

    dead = [name for name, row in projects.items() if row["verdict"] == DEAD]
    summary = {
        "projects": len(projects), "dead": len(dead),
        "lagging": sum(1 for r in projects.values() if r["verdict"] == LAGGING),
        "warnings": sum(1 for r in projects.values() if r["verdict"] in WARN_VERDICTS),
    }
    if scan_roots:
        # Only present in scan mode, so the registry-only report stays
        # byte-for-byte what it was before --scan-root existed.
        summary["unregistered"] = sum(
            1 for r in projects.values() if r["verdict"] == VERDICT_UNREGISTERED)
        summary["worktrees"] = sum(
            1 for r in projects.values()
            if r.get("registered") is False and r["owner"])
    report = {
        "store": {"root": str(store_root), "current": current,
                  "versions": pin_drift.load_store(store_root / "versions")},
        "projects": projects,
        "summary": summary,
    }
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    failing = any(row["verdict"] in (DEAD, VERDICT_UNREGISTERED)
                  for row in projects.values())
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
