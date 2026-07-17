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
     from a live writer (must be traced first). The active/stale decision also
     weighs `.db-wal`/`.db-shm` sidecar mtimes: a fresh sidecar on an old .db
     means a connection opened the store recently (uncheckpointed), so it is
     treated as a possible live writer (RED, verify with `lsof`) rather than a
     safe stale relic.

  B. Per-project stores are canonical.
     Every registered project should own `<data_home>/<project-id>/state/`.
     A project missing its central store is either unmigrated or resolving
     somewhere else. RED when a registered project's central state is absent.

  C. Receipt hash-chain integrity (ADR-023).
     verify_chain() on each project's `state/t0_receipts.ndjson`. "unchained"
     (chaining not yet enabled) is reported OK — ADR-023 is PARTIAL by design.
     "verified" is GREEN; "broken" (tamper / partial chain) is RED.

  D. Chain-origin anchor provenance (ADR-034).
     The anchor-aware verify_chain() (scripts/lib/chain_origin_anchor.py) on
     each project's `state/t0_receipts.ndjson`, run against that project's OWN
     repo (the registry's `path`) as `project_root`. Additive to check C, not
     a replacement — ADR-034 §6 step 3 (flipping check C's OWN verify_chain
     call to the anchor-aware, fail-closed contract) is a separate, later
     migration gated on step 2b's per-project branch-protection precondition.
     This section surfaces AnchorProvenance (ref, anchor_commit_sha,
     remote_url) for every checked ledger and goes RED on "broken" — which
     includes the reverse-direction case (a git anchor exists for a ledger
     that is now missing/empty/unchained), catching a deleted-then-reset
     ledger that check C alone would still read as a clean "unchained".

Exit 0 when no RED finding, 1 otherwise. `--json` for scripting.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
try:
    from chain_origin_anchor import verify_chain as anchor_verify_chain  # type: ignore
except Exception:  # pragma: no cover - import guard
    anchor_verify_chain = None  # type: ignore

# Directory names at the data-home root that are shared/legacy by construction,
# never a project-id. `state` holding *.db is the hard split-brain signal.
SHARED_ROOT_DIRS = ("state", "events", "locks")

# A resolved project-id is used as a path component under <data_home>; reject
# anything that could traverse or is otherwise not a plain id.
SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

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


def _newest_sidecar_mtime(directory: Path) -> Optional[float]:
    """Newest mtime among SQLite sidecar files (``*.db-wal`` / ``*.db-shm``) directly
    in ``directory``.

    A fresh sidecar means a connection opened the store recently even when the
    ``.db`` mtime (its last checkpoint) stayed old. Check A used to read only
    ``.db`` mtimes, so a Jun-20 ``.db`` with a same-day ``.db-wal`` reported as a
    safe 17-day stale relic while a connection had in fact just touched it. mtime
    alone cannot tell a leftover sidecar from a live handle, so a recent sidecar
    must escalate the finding (verify with ``lsof`` before retiring).
    """
    newest: Optional[float] = None
    try:
        for entry in directory.iterdir():
            if entry.is_file() and (entry.name.endswith(".db-wal") or entry.name.endswith(".db-shm")):
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


def _load_project_ids(registry_file: Path) -> tuple[list[tuple[str, str]], Optional[str]]:
    """Return ``([(project_id, project_path)], registry_error)``.

    ``registry_error`` is a message when the registry EXISTS but is unreadable
    or malformed — the caller turns that into a RED finding so the audit never
    reports clean while it was actually blind to the project set. A registry
    that simply does not exist yields ``([], None)`` (legitimate).

    project_id comes from ``<path>/.vnx-project-id`` (first line) when readable,
    else falls back to the registry name. A resolved id that is not a safe path
    component is skipped with a warning (traversal guard).
    """
    out: list[tuple[str, str]] = []
    if not registry_file.exists():
        return out, None
    try:
        data = json.loads(registry_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return out, f"cannot read project registry {registry_file}: {exc}"
    for p in data.get("projects", []):
        path = p.get("path", "") or ""
        name = p.get("name") or p.get("id") or ""
        pid = name
        if path:
            id_file = Path(path) / ".vnx-project-id"
            if id_file.exists():
                try:
                    first = id_file.read_text(encoding="utf-8").splitlines()[0].strip()
                    if first:
                        pid = first
                except (OSError, IndexError) as exc:
                    print(
                        f"fabric-audit: WARNING — cannot read {id_file}: {exc}; "
                        f"falling back to registry name {name!r}",
                        file=sys.stderr,
                    )
        if not pid:
            continue
        if "/" in pid or ".." in pid or not SAFE_PROJECT_ID.match(pid):
            print(
                f"fabric-audit: WARNING — skipping project with unsafe id {pid!r} "
                f"(from {path or 'registry'}) — not used as a path component",
                file=sys.stderr,
            )
            continue
        out.append((pid, path))
    return out, None


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

        newest_db = _newest_db_mtime(bare_state)
        newest_sidecar = _newest_sidecar_mtime(bare_state)
        findings[0]["newest_sidecar_mtime"] = _fmt_ts(newest_sidecar)
        # A recent -wal/-shm touch means a connection opened the store recently even
        # when the .db was never checkpointed (its mtime stays old). Drive the
        # active/stale decision off the newest of the two so an old .db can never
        # read as a safe stale relic while a sidecar was just written.
        activity = max((m for m in (newest_db, newest_sidecar) if m is not None), default=None)
        age_days = (time.time() - activity) / 86400 if activity else None
        active = age_days is not None and age_days < ACTIVE_FORK_STALE_DAYS
        status = "RED" if active else "WARN"
        sidecar_recent = newest_sidecar is not None and (
            newest_db is None or newest_sidecar > newest_db
        )
        if active:
            kind = "ACTIVE fork — a live writer may still be resolving to the shared store"
            if sidecar_recent:
                kind += (
                    f" (a -wal/-shm connection touched it at {_fmt_ts(newest_sidecar)}, "
                    f"newer than the last .db checkpoint — verify with `lsof` before retiring)"
                )
        else:
            kind = f"stale relic ({int(age_days)}d old) — cleanup debt, retire when convenient"
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


def check_per_project_stores(
    data_home: Path, projects: list[tuple[str, str]], registry_error: Optional[str] = None
) -> CheckResult:
    if registry_error:
        # A registry we could not read must never read as clean — the audit
        # was blind to the project set, so it cannot vouch for the stores.
        return CheckResult(
            "B", "Per-project stores canonical", "RED",
            f"project registry unreadable — cannot verify per-project stores: {registry_error}",
            [{"registry_error": registry_error}],
        )
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
        # The integrity verifier failing to import is itself a problem — the
        # audit cannot vouch for chain integrity, so surface it (not a silent SKIP).
        return CheckResult(
            "C", "Receipt hash-chain integrity", "WARN",
            "integrity verifier (ndjson_hash_chain.verify_chain) could not be imported — chains NOT checked",
        )
    if not projects:
        return CheckResult("C", "Receipt hash-chain integrity", "SKIP", "no registered projects")
    broken = []
    verified = 0
    segmented = 0
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
        elif status == "verified-segmented":
            # ADR-029: an unchained epoch-0 prefix + intact chained epoch(s) is
            # a healthy sealed ledger mid-adoption, not a break.
            segmented += 1
        elif status == "unchained":
            unchained += 1
        else:  # broken
            broken.append({"project": pid, "violations": len(violations), "status": status})
    if broken:
        names = ", ".join(b["project"] for b in broken)
        return CheckResult(
            "C", "Receipt hash-chain integrity", "RED",
            f"broken chain in: {names} (verified={verified}, segmented={segmented}, unchained={unchained})",
            broken,
        )
    if checked == 0:
        return CheckResult("C", "Receipt hash-chain integrity", "SKIP", "no t0_receipts.ndjson found")
    return CheckResult(
        "C", "Receipt hash-chain integrity", "GREEN",
        f"{checked} ledgers ok (verified={verified}, segmented={segmented}, unchained={unchained} — ADR-023/029)",
    )


def check_anchor_provenance(data_home: Path, projects: list[tuple[str, str]]) -> CheckResult:
    if anchor_verify_chain is None:
        # Finding 4 (ADR-034 fix-r1): RED, not WARN. WARN is non-blocking
        # (main() only fails on a RED finding) — an anchor verifier that
        # can't even import means the anchor-provenance audit did NOT run at
        # all, which must never let the overall audit exit 0. Unlike check
        # C's underlying chain-integrity verifier (a long-standing, always-
        # available import), this is the anchor check's OWN verifier — its
        # absence is exactly the failure this specific check exists to catch.
        return CheckResult(
            "D", "Chain-origin anchor provenance (ADR-034)", "RED",
            "anchor verifier (chain_origin_anchor.verify_chain) could not be imported — anchors NOT checked",
        )
    if not projects:
        return CheckResult("D", "Chain-origin anchor provenance (ADR-034)", "SKIP", "no registered projects")

    findings = []
    broken_projects = []
    verified = 0
    unchained = 0
    checked = 0
    for pid, path in projects:
        ledger = data_home / pid / "state" / "t0_receipts.ndjson"
        project_root = Path(path) if path else None
        if project_root is None or not project_root.is_dir() or not (project_root / ".git").exists():
            # No resolvable GIT repo for this project — nothing to
            # anchor-check against (verify_chain requires a real git
            # checkout, ADR §2). Checking for `.git` specifically (not just
            # "is a directory") matters now that a missing ledger file no
            # longer short-circuits this loop (Finding 3 below) — a
            # registered project path that exists but was never actually
            # cloned/initialized as a git repo must still SKIP, not fail
            # anchor resolution and read as broken/RED.
            continue
        # Finding 3 (ADR-034 fix-r1): do NOT skip on a missing ledger file
        # here. A project whose t0_receipts.ndjson was deleted/reset but
        # which has a committed anchor on origin for its identity must
        # report "broken" (the reverse-direction case anchor_verify_chain
        # already covers) — skipping before ever calling the anchor-aware
        # verifier let that case fall through to SKIP/GREEN instead. A
        # genuinely missing ledger with no anchor anywhere still resolves to
        # "unchained" inside anchor_verify_chain itself (base verify_chain's
        # own missing-file handling), so this is additive, not a false RED.
        checked += 1
        try:
            _is_valid, violations, status, provenance = anchor_verify_chain(
                ledger,
                project_root=project_root,
                project_id=pid,
                project_data_dir=data_home / pid,
            )
        except Exception as exc:
            broken_projects.append(pid)
            findings.append({"project": pid, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            continue
        finding = {
            "project": pid,
            "status": status,
            "anchor_ref": provenance.ref if provenance else None,
            "anchor_commit_sha": provenance.anchor_commit_sha if provenance else None,
            "remote_url": provenance.remote_url if provenance else None,
        }
        if status in ("verified", "verified-segmented"):
            verified += 1
        elif status == "unchained":
            unchained += 1
        else:  # broken
            broken_projects.append(pid)
            finding["violations"] = len(violations)
        findings.append(finding)

    if broken_projects:
        names = ", ".join(broken_projects)
        return CheckResult(
            "D", "Chain-origin anchor provenance (ADR-034)", "RED",
            f"broken anchor verification in: {names} (verified={verified}, unchained={unchained})",
            findings,
        )
    if checked == 0:
        return CheckResult(
            "D", "Chain-origin anchor provenance (ADR-034)", "SKIP",
            "no registered project has a resolvable repo to anchor-check against",
        )
    return CheckResult(
        "D", "Chain-origin anchor provenance (ADR-034)", "GREEN",
        f"{checked} ledgers ok (verified={verified}, unchained={unchained} — ADR-034)",
        findings,
    )


def run_audit(data_home: Path, registry_file: Path) -> list[CheckResult]:
    projects, registry_error = _load_project_ids(registry_file)
    return [
        check_shared_root_state(data_home),
        check_per_project_stores(data_home, projects, registry_error),
        check_hash_chains(data_home, projects),
        check_anchor_provenance(data_home, projects),
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
