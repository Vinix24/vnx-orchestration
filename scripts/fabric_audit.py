#!/usr/bin/env python3
"""fabric_audit.py — Phase-0 fabric hardening audit (ADR-028 §6 phase 0).

Answers one question before a T0 session trusts the governance fabric:
is the audit trail whole, per-project, and un-forked?

Checks (each GREEN / RED / SKIP):

  A. Legacy shared state at the data-home root.
     A bare `<data_home>/state/` (a dir literally named "state", not a
     project-id) holding *.db files is the split-brain signature: state that
     should live under `<data_home>/<project-id>/state/` was written to a
     shared, project-agnostic location instead. RED when found, with the
     newest *.db mtime so an operator can tell a stale relic (safe to retire)
     from a live writer (must be traced first).

  B. Per-project stores are canonical.
     Every registered project should own `<data_home>/<project-id>/state/`.
     A project missing its central store is either unmigrated or resolving
     somewhere else. RED when a registered project's central state is absent.

  C. Receipt hash-chain integrity (ADR-023).
     verify_chain() on each project's `state/t0_receipts.ndjson`. "unchained"
     (chaining not yet enabled) is reported OK — ADR-023 is PARTIAL by design.
     "verified" is GREEN; "broken" (tamper / partial chain) is RED.

Exit 0 when no RED finding, 1 otherwise. `--json` for scripting.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Reuse the canonical chain verifier rather than reimplementing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
try:
    from ndjson_hash_chain import verify_chain  # type: ignore
except Exception:  # pragma: no cover - import guard
    verify_chain = None  # type: ignore

# Directory names at the data-home root that are shared/legacy by construction,
# never a project-id. `state` holding *.db is the hard split-brain signal.
SHARED_ROOT_DIRS = ("state", "events", "locks")

# A bare shared store written within this many days is an ACTIVE fork (RED);
# older than this it is a stale relic = cleanup debt (WARN, does not block).
ACTIVE_FORK_STALE_DAYS = 7


@dataclass
class CheckResult:
    key: str
    title: str
    status: str  # "GREEN" | "RED" | "SKIP"
    detail: str
    findings: list = field(default_factory=list)


def _newest_db_mtime(directory: Path) -> Optional[float]:
    """Newest mtime among *.db files directly in `directory` (non-recursive)."""
    newest: Optional[float] = None
    try:
        for entry in directory.iterdir():
            if entry.suffix == ".db" and entry.is_file():
                m = entry.stat().st_mtime
                if newest is None or m > newest:
                    newest = m
    except OSError:
        return None
    return newest


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "n/a"
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _load_project_ids(registry_file: Path) -> list[tuple[str, str]]:
    """Return [(project_id, project_path)] for registered projects.

    project_id comes from `<path>/.vnx-project-id` (first line) when readable,
    else falls back to the registry name — so the audit still names the project.
    """
    out: list[tuple[str, str]] = []
    try:
        data = json.loads(registry_file.read_text(encoding="utf-8"))
    except Exception:
        return out
    for p in data.get("projects", []):
        path = p.get("path", "") or ""
        name = p.get("name") or p.get("id") or ""
        pid = name
        if path:
            id_file = Path(path) / ".vnx-project-id"
            try:
                first = id_file.read_text(encoding="utf-8").splitlines()[0].strip()
                if first:
                    pid = first
            except Exception:
                pass
        if pid:
            out.append((pid, path))
    return out


def check_shared_root_state(data_home: Path) -> CheckResult:
    findings = []
    bare_state = data_home / "state"
    if bare_state.is_dir():
        newest = _newest_db_mtime(bare_state)
        db_count = sum(1 for e in bare_state.iterdir() if e.suffix == ".db") if bare_state.exists() else 0
        if db_count > 0:
            findings.append({
                "path": str(bare_state),
                "db_count": db_count,
                "newest_db_mtime": _fmt_ts(newest),
            })
    other_shared = []
    for name in SHARED_ROOT_DIRS:
        if name == "state":
            continue
        d = data_home / name
        if d.is_dir() and any(d.iterdir()):
            other_shared.append(name)
    if findings:
        import time

        newest = _newest_db_mtime(bare_state)
        age_days = (time.time() - newest) / 86400 if newest else None
        active = age_days is not None and age_days < ACTIVE_FORK_STALE_DAYS
        status = "RED" if active else "WARN"
        kind = (
            "ACTIVE fork — a live writer is still resolving to the shared store"
            if active
            else f"stale relic ({int(age_days)}d old) — cleanup debt, retire when convenient"
        )
        detail = (
            f"legacy shared store {findings[0]['path']} "
            f"({findings[0]['db_count']} *.db, newest write {findings[0]['newest_db_mtime']}) — {kind}; "
            f"retire under <data_home>/<project-id>/state after confirming no live writer"
        )
        if other_shared:
            detail += f"; also shared root dirs: {', '.join(other_shared)}"
        return CheckResult("A", "Legacy shared state at data root", status, detail, findings)
    if other_shared:
        return CheckResult(
            "A", "Legacy shared state at data root", "GREEN",
            f"no bare state/*.db; shared root dirs present but empty-ish: {', '.join(other_shared)}",
        )
    return CheckResult("A", "Legacy shared state at data root", "GREEN", "no bare shared state store")


def check_per_project_stores(data_home: Path, projects: list[tuple[str, str]]) -> CheckResult:
    if not projects:
        return CheckResult("B", "Per-project stores canonical", "SKIP", "no registered projects")
    missing = []
    ok = 0
    for pid, _path in projects:
        state_dir = data_home / pid / "state"
        if state_dir.is_dir():
            ok += 1
        else:
            missing.append(pid)
    if missing:
        return CheckResult(
            "B", "Per-project stores canonical", "RED",
            f"{ok}/{len(projects)} projects have central state; missing: {', '.join(missing)}",
            [{"missing_project": m} for m in missing],
        )
    return CheckResult(
        "B", "Per-project stores canonical", "GREEN",
        f"{ok}/{len(projects)} registered projects have <data_home>/<pid>/state",
    )


def check_hash_chains(data_home: Path, projects: list[tuple[str, str]]) -> CheckResult:
    if verify_chain is None:
        return CheckResult("C", "Receipt hash-chain integrity", "SKIP", "verify_chain unavailable")
    if not projects:
        return CheckResult("C", "Receipt hash-chain integrity", "SKIP", "no registered projects")
    broken = []
    verified = 0
    unchained = 0
    checked = 0
    for pid, _path in projects:
        ledger = data_home / pid / "state" / "t0_receipts.ndjson"
        if not ledger.exists():
            continue
        checked += 1
        try:
            is_valid, violations, status = verify_chain(ledger)
        except Exception as exc:
            broken.append({"project": pid, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if status == "verified":
            verified += 1
        elif status == "unchained":
            unchained += 1
        else:  # broken
            broken.append({"project": pid, "violations": len(violations), "status": status})
    if broken:
        names = ", ".join(b["project"] for b in broken)
        return CheckResult(
            "C", "Receipt hash-chain integrity", "RED",
            f"broken chain in: {names} (verified={verified}, unchained={unchained})",
            broken,
        )
    if checked == 0:
        return CheckResult("C", "Receipt hash-chain integrity", "SKIP", "no t0_receipts.ndjson found")
    return CheckResult(
        "C", "Receipt hash-chain integrity", "GREEN",
        f"{checked} ledgers ok (verified={verified}, unchained={unchained} — ADR-023 partial, not an error)",
    )


def run_audit(data_home: Path, registry_file: Path) -> list[CheckResult]:
    projects = _load_project_ids(registry_file)
    return [
        check_shared_root_state(data_home),
        check_per_project_stores(data_home, projects),
        check_hash_chains(data_home, projects),
    ]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="VNX fabric-audit — phase-0 fabric hardening check")
    parser.add_argument(
        "--data-home",
        default=os.environ.get("VNX_DATA_HOME") or str(Path.home() / ".vnx-data"),
        help="Central data-home root (default: $VNX_DATA_HOME or ~/.vnx-data)",
    )
    parser.add_argument(
        "--registry",
        default=str(Path.home() / ".vnx" / "projects.json"),
        help="Project registry JSON (default: ~/.vnx/projects.json)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    data_home = Path(args.data_home).expanduser()
    registry_file = Path(args.registry).expanduser()
    results = run_audit(data_home, registry_file)

    red = [r for r in results if r.status == "RED"]
    warn = [r for r in results if r.status == "WARN"]
    overall = "RED" if red else ("GREEN-WITH-WARN" if warn else "GREEN")

    if args.json:
        print(json.dumps({
            "overall": overall,
            "data_home": str(data_home),
            "checks": [
                {"key": r.key, "title": r.title, "status": r.status,
                 "detail": r.detail, "findings": r.findings}
                for r in results
            ],
        }, indent=2))
    else:
        print(f"VNX fabric-audit — data-home {data_home}")
        for r in results:
            dot = {"GREEN": "[ok]  ", "RED": "[RED] ", "WARN": "[warn]", "SKIP": "[--]  "}[r.status]
            print(f"  {dot} [{r.key}] {r.title}: {r.detail}")
        summary = f"OVERALL: {overall}"
        if red:
            summary += f" ({len(red)} blocking finding{'s' if len(red) != 1 else ''})"
        elif warn:
            summary += f" ({len(warn)} warning{'s' if len(warn) != 1 else ''} — non-blocking cleanup debt)"
        print(summary)

    # WARN is surfaced but does not block a T0 session; only RED fails.
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
